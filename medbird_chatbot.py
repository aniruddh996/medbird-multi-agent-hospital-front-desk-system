import os, sys, json, re
from pathlib import Path
from ibm_watsonx_ai.foundation_models import ModelInference
from ibm_watsonx_ai.foundation_models.schema import TextChatParameters

# ---------------------------
# Config (env or defaults)
# ---------------------------
MODEL_ID = os.getenv("WX_MODEL_ID", "ibm/granite-3-3-8b-instruct")
WX_URL = os.getenv("WX_URL", "https://us-south.ml.cloud.ibm.com")
WX_API_KEY = "FrTeMV6PrYLAWNt4nsbIFFGhyXuSW3bw0CMtcKUpLwh3" # required
WX_PROJECT_ID = "3beeec8a-fb9a-4fd0-9acd-8b3479e60625"  # required

INSTRUCTIONS_PATH = Path(os.getenv(
    "INSTRUCTIONS_PATH",
    "C:\\Users\\Aniruddh Rajagopal\\Downloads\\instructions to chatbot.txt"
))

# Minimal example roster — replace with your real list.
ROSTER = """\
- id: d001; name: Dr. Maya Patel; specialty: Cardiology; conditions: chest pain, hypertension, palpitations, shortness of breath; location: Downtown Clinic; availability: 2025-08-10 10:00, 2025-08-10 11:30, 2025-08-11 09:00
- id: d002; name: Dr. Alex Nguyen; specialty: Dermatology; conditions: acne, eczema, rash, psoriasis, mole; location: Uptown Medical Center; availability: 2025-08-10 15:00, 2025-08-12 13:00
- id: d003; name: Dr. Sara Haddad; specialty: Orthopedics; conditions: knee pain, back pain, shoulder pain, sprain, fracture; location: City Ortho Hub; availability: 2025-08-11 14:00, 2025-08-12 10:30
- id: d004; name: Dr. Priya Sharma; specialty: Internal Medicine; conditions: headache, fever, cold, migraine, fatigue, checkup, general checkup; location: Riverside Family Practice; availability: 2025-08-10 09:30, 2025-08-10 16:30, 2025-08-11 11:00
"""

# ---------------------------
# Load instructions & inject roster
# ---------------------------
if not INSTRUCTIONS_PATH.exists():
    raise FileNotFoundError(f"Instructions file not found: {INSTRUCTIONS_PATH}")

instructions_raw = INSTRUCTIONS_PATH.read_text(encoding="utf-8")
system_message = instructions_raw.replace("{{ROSTER}}", ROSTER)

# ---------------------------
# Model client
# ---------------------------
model = ModelInference(
    model_id=MODEL_ID,
    credentials={"apikey": WX_API_KEY, "url": WX_URL},
    project_id=WX_PROJECT_ID,
)

params = TextChatParameters(
    temperature=0.2,        # tight for slot-filling
    max_tokens=220,         # short turns; prevents rambles
    top_p=0.9,
    # Some SDK versions support stop sequences; if yours does, uncomment:
    # stop_sequences=["\nyou:", "\nYou:", "\nuser:", "\nUser:"]
)

# ---------------------------
# Chat state
# ---------------------------
history = [{"role": "system", "content": system_message}]

# Booking detector – allow multiline, tolerate spaces
BOOKING_RE = re.compile(
    r"^BOOKING_REQUEST\s*:\s*(\{.*\})\s*$",
    re.DOTALL | re.MULTILINE
)

def clean_reply(text: str) -> str:
    """
    Keep only the assistant’s message. If a BOOKING_REQUEST is present,
    return *only* that exact line. Otherwise, strip any accidental 'you:' or 'assistant:'.
    """
    t = text.strip()

    # If booking line exists, return exactly that line
    m = BOOKING_RE.search(t)
    if m:
        return m.group(0).strip()

    # Stop at the point it tries to write user lines
    t = re.split(r'\n(?:you:|user:)\s*', t, flags=re.IGNORECASE)[0]

    # Remove accidental "assistant:" prefixes
    t = re.sub(r'^(assistant:\s*)', '', t.strip(), flags=re.IGNORECASE)

    # Keep it to one short message
    return t.strip()

def stream_chat(messages):
    """
    Stream but *don’t* print chunk-by-chunk; we want to clean the full text first.
    """
    for chunk in model.chat_stream(messages=messages, params=params):
        if not isinstance(chunk, dict):
            continue
        choices = chunk.get("choices", [])
        if choices:
            msg = choices[0].get("message", {}) or {}
            piece = msg.get("content") or choices[0].get("delta", {}).get("content")
            if piece:
                yield piece

def ask(user_text: str):
    history.append({"role": "user", "content": user_text})

    # Collect full reply first
    reply_parts = []
    try:
        for piece in stream_chat(history):
            reply_parts.append(piece)
    except Exception as e:
        # Fallback to non-streaming
        out = model.chat(messages=history, params=params)
        reply_parts = [out["choices"][0]["message"]["content"]]

    reply_raw = "".join(reply_parts) if reply_parts else ""
    reply = clean_reply(reply_raw)

    # Print once, after cleaning
    print("\nassistant: " + reply)

    # Append cleaned reply to history (prevents poisoning with 'you:' lines)
    history.append({"role": "assistant", "content": reply})

    # Detect booking line (post-clean)
    m = BOOKING_RE.search(reply)
    if m:
        try:
            booking = json.loads(m.group(1))
            print("\n[BOOKING_REQUEST detected]")
            print(json.dumps(booking, indent=2))
            # (Later) trigger summary/notifications here, or emit an event/DB write
        except json.JSONDecodeError:
            pass

def main():
    print("Doctor Booking Chat (Prompt-Lab style). Type 'quit' to exit.")
    while True:
        try:
            user_text = input("you: ").strip()
            if not user_text:
                continue
            if user_text.lower() in {"quit", "exit"}:
                break
            ask(user_text)
        except KeyboardInterrupt:
            print("\nBye!")
            break

if __name__ == "__main__":
    main()
