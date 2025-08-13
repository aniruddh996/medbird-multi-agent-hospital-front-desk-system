import os, sys, json, re, subprocess
from pathlib import Path
from datetime import datetime, timedelta

from ibm_watsonx_ai.foundation_models import ModelInference
from ibm_watsonx_ai.foundation_models.schema import TextChatParameters

MODEL_ID = os.getenv("WX_MODEL_ID", "ibm/granite-3-3-8b-instruct")
WX_URL = os.getenv("WX_URL", "https://us-south.ml.cloud.ibm.com")
WX_API_KEY = os.getenv("WX_API_KEY", "FrTeMV6PrYLAWNt4nsbIFFGhyXuSW3bw0CMtcKUpLwh3")
WX_PROJECT_ID = os.getenv("WX_PROJECT_ID", "3beeec8a-fb9a-4fd0-9acd-8b3479e60625")

BASE = Path(__file__).resolve().parent
INSTRUCTIONS_PATH = BASE / "config" / "instructions to chatbot.txt"
DOCTORS_PATH      = BASE / "config" / "doctors.json"
APPTS_PATH        = BASE / "data"   / "appointments.json"

NOTIFIER = str(BASE / "agents" / "notifier_agent.py")
REMINDER = str(BASE / "agents" / "reminder_agent.py")

(APPTS_PATH.parent).mkdir(parents=True, exist_ok=True)

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default

def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")

# --- weekly schedule parsing + live slots ---
DOW_MAP = {"Mon":0,"Tue":1,"Wed":2,"Thu":3,"Fri":4,"Sat":5,"Sun":6,"M":0,"T":1,"W":2,"Th":3,"F":4,"S":5,"Su":6}

def _parse_weekly(weekly: str):
    if not isinstance(weekly, str) or not weekly.strip():
        return {i:(9*60,17*60) for i in range(0,5)}
    w = weekly.replace("Monday","Mon").replace("Tuesday","Tue").replace("Wednesday","Wed").replace("Thursday","Thu").replace("Friday","Fri").replace("Saturday","Sat").replace("Sunday","Sun")
    w = w.replace("Mon","M").replace("Tue","T").replace("Wed","W").replace("Thu","Th").replace("Fri","F").replace("Sat","S").replace("Sun","Su")
    parts = w.split()
    time_token = next((p for p in reversed(parts) if ":" in p and "-" in p), None)
    if time_token:
        t1,t2 = time_token.split("-"); sh,sm = map(int,t1.split(":")); eh,em = map(int,t2.split(":"))
        start_m,end_m = sh*60+sm, eh*60+em
    else:
        start_m,end_m = 9*60,17*60
    days_str = "".join([p for p in parts if p != time_token])
    result = {}
    for token in days_str.split(","):
        token = token.strip()
        if not token: continue
        if "-" in token:
            a,b = token.split("-")
            order = ["M","T","W","Th","F","S","Su"]
            ia, ib = order.index(a), order.index(b)
            for sym in order[ia:ib+1]:
                result[DOW_MAP[sym]] = (start_m,end_m)
        elif token in DOW_MAP:
            result[DOW_MAP[token]] = (start_m,end_m)
    return result or {i:(start_m,end_m) for i in range(0,5)}

def _gen_slots(days_map, horizon_days=14, step_min=30, daily_cap=8):
    now = datetime.now()
    end = now + timedelta(days=horizon_days)
    cur = datetime(now.year, now.month, now.day, 0, 0)
    while cur <= end:
        wd = cur.weekday()
        if wd in days_map:
            start_m, end_m = days_map[wd]
            day_start = datetime(cur.year, cur.month, cur.day, start_m//60, start_m%60)
            day_end   = datetime(cur.year, cur.month, cur.day, end_m//60, end_m%60)
            ptr = max(day_start, now + timedelta(minutes=1))
            count = 0
            while ptr + timedelta(minutes=step_min) <= day_end and count < daily_cap:
                yield ptr
                ptr += timedelta(minutes=step_min)
                count += 1
        cur += timedelta(days=1)

def _slot(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")

def next_free_slots(doc: dict, booked: set, max_slots=6):
    weekly = doc.get("weekly_schedule") or doc.get("availability") or "Mon-Fri 09:00-17:00"
    days_map = _parse_weekly(weekly)
    out = []
    for dt in _gen_slots(days_map):
        s = _slot(dt)
        if s not in booked:
            out.append(s)
        if len(out) >= max_slots:
            break
    return out

# Build dynamic ROSTER from doctors.json and appointments.json
doctors_map = load_json(DOCTORS_PATH, {})
appts = load_json(APPTS_PATH, [])
booked_by_doc = {}
for a in appts:
    did = a.get("doctor_id"); slot = a.get("slot") or a.get("datetime")
    if did and slot:
        booked_by_doc.setdefault(did, set()).add(slot)

def _roster_line(d: dict) -> str:
    name = d.get("name") or d.get("doctor_name") or ""
    specialty = d.get("specialization") or d.get("specialty") or ""
    conds = d.get("conditions") or []
    conds_txt = ", ".join(conds) if isinstance(conds, list) else str(conds)
    location = d.get("location","")
    slots = next_free_slots(d, booked_by_doc.get(d.get("doctor_id"), set()))
    availability = ", ".join(slots) if slots else "none"
    return f"- id: {d.get('doctor_id')}; name: {name}; specialty: {specialty}; conditions: {conds_txt}; location: {location}; availability: {availability}"

ROSTER = "\n".join(_roster_line(doc) for doc in doctors_map.values())

# Load instructions & inject roster
if not INSTRUCTIONS_PATH.exists():
    raise FileNotFoundError(f"Instructions file not found: {INSTRUCTIONS_PATH}")
instructions_raw = INSTRUCTIONS_PATH.read_text(encoding="utf-8")
system_message = instructions_raw.replace("{{ROSTER}}", ROSTER)

# Model client
model = ModelInference(
    model_id=MODEL_ID,
    credentials={"apikey": WX_API_KEY, "url": WX_URL},
    project_id=WX_PROJECT_ID,
)
params = TextChatParameters(temperature=0.2, max_tokens=250, top_p=0.9)

# Chat state
history = [{"role": "system", "content": system_message}]
BOOKING_RE = re.compile(r"^BOOKING_REQUEST\s*:\s*(\{.*\})\s*$", re.DOTALL | re.MULTILINE)

def _append_appointment(booking: dict):
    cur = load_json(APPTS_PATH, [])
    if "datetime" in booking and "slot" not in booking:
        booking["slot"] = booking["datetime"]
    key = (booking.get("doctor_id"), booking.get("slot"), booking.get("patient_name"))
    for a in cur:
        if (a.get("doctor_id"), a.get("slot") or a.get("datetime"), a.get("patient_name")) == key:
            return
    cur.append(booking)
    save_json(APPTS_PATH, cur)

# >>> added: PID file + helper to keep one reminder loop
PID_FILE = BASE / "data" / "reminder_loop.pid"

def _reminder_loop_running() -> bool:
    try:
        if PID_FILE.exists():
            pid = int(PID_FILE.read_text().strip())
            # os.kill(pid, 0) raises OSError if not running (POSIX/macOS)
            os.kill(pid, 0)
            return True
    except Exception:
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    return False

def _start_reminder_loop():
    if _reminder_loop_running():
        return
    (BASE / "data").mkdir(parents=True, exist_ok=True)
    # start reminder loop in background, checking every 60s
    proc = subprocess.Popen([sys.executable, REMINDER, "--loop", "--interval", "60", "--doctors", str(DOCTORS_PATH)])
    try:
        PID_FILE.write_text(str(proc.pid))
    except Exception:
        pass
    print(f"[CHATBOT] reminder loop started pid={proc.pid}")
# <<< added

def ask(user_text: str):
    history.append({"role": "user", "content": user_text})
    reply_parts = []
    try:
        for chunk in model.chat_stream(messages=history, params=params):
            if not isinstance(chunk, dict): continue
            choices = chunk.get("choices", [])
            if not choices: continue
            msg = choices[0].get("message", {}) or {}
            piece = msg.get("content") or choices[0].get("delta", {}).get("content")
            if piece: reply_parts.append(piece)
    except Exception:
        out = model.chat(messages=history, params=params)
        reply_parts = [out["choices"][0]["message"]["content"]]
    reply = "".join(reply_parts).strip()
    print("\nassistant:", reply)
    history.append({"role": "assistant", "content": reply})

    m = BOOKING_RE.search(reply)
    if not m:
        return
    try:
        booking = json.loads(m.group(1))
    except json.JSONDecodeError:
        print("[WARN] Could not parse BOOKING_REQUEST JSON")
        return

    print("\n[BOOKING_REQUEST detected]")
    print(json.dumps(booking, indent=2))

    # 1) Save appointment
    _append_appointment(booking)

    # 2) Immediate notifications
    try:
        subprocess.call([sys.executable, NOTIFIER, "--booking", json.dumps(booking), "--doctors", str(DOCTORS_PATH)])
    except Exception as e:
        print(f"[CHATBOT] notifier error: {e}")

    # 3) Register reminders (your reminder_agent defaults decide offsets; set to 2h & 10m there)
    try:
        subprocess.call([sys.executable, REMINDER, "--booking", json.dumps(booking), "--doctors", str(DOCTORS_PATH)])
    except Exception as e:
        print(f"[CHATBOT] reminder error: {e}")

    # >>> added: ensure reminder loop is running in background
    _start_reminder_loop()
    # <<< added

def main():
    print("MedBird — Appointment Chat. Type 'quit' to exit.")
    while True:
        try:
            user_text = input("you: ").strip()
        except KeyboardInterrupt:
            print("\nBye!"); break
        if not user_text: 
            continue
        if user_text.lower() in {"quit", "exit"}:
            break
        ask(user_text)

if __name__ == "__main__":
    main()
