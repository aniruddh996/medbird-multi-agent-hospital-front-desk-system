# medbird_chatbot.py — Full Streamlit + watsonx + Mongo (users + appointments)
# ----------------------------------------------------------------------
# Quick start:
#   pip install --upgrade streamlit pymongo ibm-watsonx-ai
#   python -m streamlit run "medbird_chatbot.py"
#
# Optional: .streamlit/secrets.toml (same folder as this file)
# [ibm]
# api_key    = "..."
# url        = "https://us-south.ml.cloud.ibm.com"
# project_id = "..."
# model_id   = "ibm/granite-3-3-8b-instruct"
# [mongo]
# uri = "mongodb+srv://<user>:<pass>@<cluster>/"
# db  = "medbird"
# ----------------------------------------------------------------------

import os, json, re, calendar
from datetime import datetime, timedelta
import streamlit as st
import smtplib, ssl
from email.message import EmailMessage

# Optional backends (don’t crash if not installed)
HAS_IBM = True
try:
    from ibm_watsonx_ai.foundation_models import ModelInference
    from ibm_watsonx_ai.foundation_models.schema import TextChatParameters
except Exception:
    HAS_IBM = False

HAS_PYMONGO = True
try:
    from pymongo import MongoClient
except Exception:
    HAS_PYMONGO = False

# ---------------------------
# Page config & Styles
# ---------------------------
st.set_page_config(
    page_title="🏥 MedBird - Book Your Appointment",
    page_icon="🏥",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
    .main-header { text-align:center;color:#2E86C1;margin-bottom:1.25rem;font-size:2.2rem;font-weight:800; }
    .intro-text { text-align:center;color:#555;font-size:1.05rem;margin-bottom:1rem;padding:.8rem;background:linear-gradient(135deg,#f5f7fa 0%,#c3cfe2 100%); border-radius:12px; }
    .doctor-info { background:linear-gradient(135deg,#e3ffe7 0%,#d9e7ff 100%); padding:.75rem;border-radius:10px;margin:.4rem 0;border-left:4px solid #4caf50; }
</style>
""",
    unsafe_allow_html=True,
)

# ---------------------------
# Config (env → secrets → default)
# ---------------------------
def _env_or_secret(env, section, key, default=""):
    v = os.getenv(env)
    if v is not None and v.strip() != "":
        return v
    try:
        return st.secrets.get(section, {}).get(key, default)
    except Exception:
        return default

MODEL_ID      = _env_or_secret("WX_MODEL_ID", "ibm", "model_id", "ibm/granite-3-3-8b-instruct")
WX_URL        = _env_or_secret("WX_URL", "ibm", "url", "https://us-south.ml.cloud.ibm.com")
WX_API_KEY    = _env_or_secret("WX_API_KEY", "ibm", "api_key", "")
WX_PROJECT_ID = _env_or_secret("WX_PROJECT_ID", "ibm", "project_id", "")

MONGO_URI     = _env_or_secret("MONGO_URI", "mongo", "uri", "")
DB_NAME       = _env_or_secret("DB_NAME",  "mongo", "db",  "medbird")

# Email/SMTP (optional)
MAIL_ENABLED = bool(st.secrets.get("mail", {}).get("enabled", False))
MAIL_HOST    = _env_or_secret("SMTP_HOST", "mail", "host", "")
MAIL_PORT    = int(_env_or_secret("SMTP_PORT", "mail", "port", "587") or 587)
MAIL_USER    = _env_or_secret("SMTP_USER", "mail", "user", "")
MAIL_PASS    = _env_or_secret("SMTP_PASS", "mail", "pass", "")
MAIL_FROM    = _env_or_secret("SMTP_FROM", "mail", "from_email", "")
DEBUG_EMAIL  = bool(st.secrets.get("mail", {}).get("debug", False))

# Email/SMTP (fixed direct loading)
try:
    mail_section = st.secrets.get("mail", {})
    print(f"DEBUG: Raw mail section from secrets: {mail_section}")
    
    MAIL_ENABLED = str(mail_section.get("enabled", "false")).lower() == "true"
    MAIL_HOST = mail_section.get("host", "")
    MAIL_PORT = int(mail_section.get("port", 587))
    MAIL_USER = mail_section.get("user", "")
    MAIL_PASS = mail_section.get("pass", "")
    MAIL_FROM = mail_section.get("from_email", "")
    DEBUG_EMAIL = str(mail_section.get("debug", "false")).lower() == "true"
    
    print(f"DEBUG: MAIL_ENABLED = {MAIL_ENABLED}")
    print(f"DEBUG: MAIL_HOST = {MAIL_HOST}")
    print(f"DEBUG: MAIL_USER = {MAIL_USER}")
    print(f"DEBUG: MAIL_FROM = {MAIL_FROM}")
    
except Exception as e:
    print(f"ERROR loading mail config: {e}")
    MAIL_ENABLED = False
    MAIL_HOST = MAIL_PORT = MAIL_USER = MAIL_PASS = MAIL_FROM = ""
    DEBUG_EMAIL = False

# ---------------------------
# Session State (safe init)
# ---------------------------
st.session_state.setdefault("messages", [])
st.session_state.setdefault("booking_state", None)

# ---------------------------
# Helpers
# ---------------------------

def _maybe_set_visit_type_from_text(user_text: str, state) -> None:
    """Honor explicit user preference for visit type regardless of model drift."""
    lt = (user_text or "").lower()
    if any(k in lt for k in ["telehealth", "virtual", "video visit", "video call", "online appointment", "online visit"]):
        state.visit_type = "telehealth"
    elif any(k in lt for k in ["in-person", "in person", "clinic visit", "office visit"]):
        state.visit_type = "in-person"


def parse_schedule_days(schedule_string):
    if not schedule_string:
        return ["Monday","Tuesday","Wednesday","Thursday","Friday"]
    s = schedule_string.lower()
    days = []
    if "m-f" in s or "mon-fri" in s:
        days = ["Monday","Tuesday","Wednesday","Thursday","Friday"]
    elif "m-s" in s or "mon-sat" in s:
        days = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"]
    else:
        if "mon" in s: days.append("Monday")
        if "tue" in s: days.append("Tuesday")
        if "wed" in s: days.append("Wednesday")
        if "thu" in s: days.append("Thursday")
        if "fri" in s: days.append("Friday")
        if "sat" in s: days.append("Saturday")
        if "sun" in s: days.append("Sunday")
        if not days:
            days = ["Monday","Tuesday","Wednesday","Thursday","Friday"]
    return days

def parse_schedule_hours(schedule_string):
    if not schedule_string:
        return "10:00 AM - 6:00 PM"
    m = re.search(r'(\d+)\.?(\d*)\s*(am|pm)\s*-\s*(\d+)\.?(\d*)\s*(am|pm)', schedule_string.lower())
    if m:
        h1 = int(m.group(1)); m1 = m.group(2) or "00"; p1 = m.group(3).upper()
        h2 = int(m.group(4)); m2 = m.group(5) or "00"; p2 = m.group(6).upper()
        return f"{h1}:{m1} {p1} - {h2}:{m2} {p2}"
    return "10:00 AM - 6:00 PM"

def map_specialization_to_category(specialization):
    s = (specialization or "").lower()
    if 'cardiology' in s or 'cardiac' in s: return 'cardiology'
    if 'dermatology' in s or 'skin' in s:   return 'dermatology'
    if 'orthopedic' in s or 'ortho' in s or 'bone' in s: return 'orthopedics'
    return 'internal'

def get_fallback_doctors():
    doctors_dict = {
        "cardiology":  {"id":"d001","name":"Dr. Maya Patel","specialty":"Cardiology","location":"Downtown Clinic","schedule":"M-F 9:00am - 5:00pm","available_days":["Monday","Tuesday","Wednesday","Thursday","Friday"],"working_hours":"9:00 AM - 5:00 PM"},
        "dermatology": {"id":"d002","name":"Dr. Alex Nguyen","specialty":"Dermatology","location":"Uptown Medical Center","schedule":"M-F 10:00am - 4:00pm","available_days":["Monday","Tuesday","Wednesday","Thursday","Friday"],"working_hours":"10:00 AM - 4:00 PM"},
        "orthopedics": {"id":"d003","name":"Dr. Sara Haddad","specialty":"Orthopedics","location":"City Ortho Hub","schedule":"M-F 8:00am - 6:00pm","available_days":["Monday","Tuesday","Wednesday","Thursday","Friday"],"working_hours":"8:00 AM - 6:00 PM"},
        "internal":    {"id":"d004","name":"Dr. Priya Sharma","specialty":"Internal Medicine","location":"Riverside Family Practice","schedule":"M-F 9:00am - 5:00pm","available_days":["Monday","Tuesday","Wednesday","Thursday","Friday"],"working_hours":"9:00 AM - 5:00 PM"},
    }
    specialization_map = {
        "cardiology":"cardiology","dermatology":"dermatology","orthopedics":"orthopedics","internal medicine":"internal"
    }
    return doctors_dict, specialization_map

# ---------------------------
# Slow backends (cached) — PURE (no st.* inside)
# ---------------------------
appointments_collection = None
doctors_collection = None
users_collection = None
model = None
params = None

@st.cache_resource(show_spinner=False)
def init_connections_cached(MONGO_URI, DB_NAME, use_mongo: bool, use_ibm: bool):
    """Initialize resources *without* calling any Streamlit UI API.
    Returns: appointments, doctors, users, model, params, messages(list of (level, text))
    """
    msgs = []
    _appointments = _doctors = _users = _model = _params = None

    # --- Mongo (optional)
    if use_mongo and HAS_PYMONGO and MONGO_URI:
        try:
            mongo_client = MongoClient(
                MONGO_URI,
                serverSelectionTimeoutMS=3000,
                connectTimeoutMS=3000,
                socketTimeoutMS=3000,
                tls=True if "mongodb+srv://" in MONGO_URI else False,
            )
            db = mongo_client[DB_NAME]
            _appointments = db.appointments
            _doctors = db.doctor
            _users = db.users
            mongo_client.admin.command("ping")
            msgs.append(("success", "Connected to MongoDB ✅"))
        except Exception as e:
            msgs.append(("warning", f"MongoDB not available: {e}"))
            _appointments = None
            _doctors = None
            _users = None
    elif use_mongo:
        msgs.append(("info", "PyMongo not installed or Mongo URI missing."))

    # --- IBM (optional)
    if use_ibm and HAS_IBM and WX_API_KEY and WX_URL and WX_PROJECT_ID:
        try:
            _model = ModelInference(
                model_id=MODEL_ID,
                credentials={"apikey": WX_API_KEY, "url": WX_URL},
                project_id=WX_PROJECT_ID,
            )
            _params = TextChatParameters(temperature=0.25, max_tokens=320, top_p=0.9)
            msgs.append(("success", "Watson model ready ✅"))
        except Exception as e:
            msgs.append(("warning", f"Watson init failed: {e}"))
            _model = None
            _params = None
    elif use_ibm:
        msgs.append(("info", "IBM creds not set or SDK not installed; using rule-based prompts only."))

    return _appointments, _doctors, _users, _model, _params, msgs

def load_doctors_from_db(doctors_collection):
    if doctors_collection is None:
        return get_fallback_doctors()
    try:
        if doctors_collection.count_documents({}) == 0:
            return get_fallback_doctors()
        doctors_cursor = doctors_collection.find({})
        doctors_dict = {}
        specialization_map = {}
        for doc in doctors_cursor:
            doctor_id = doc.get('doctor_id')
            name = (doc.get('name') or '').strip()
            specialization = (doc.get('specialization') or '').strip()
            weekly_schedule = doc.get('weekly_schedule', '')
            if not doctor_id or not name:
                continue
            if not name.lower().startswith('dr'):
                name = f"Dr. {name}"
            cat = map_specialization_to_category(specialization)
            doctors_dict[cat] = {
                "id": doctor_id,
                "name": name,
                "specialty": specialization or cat.title(),
                "location": f"{(specialization or cat.title())} Department",
                "schedule": weekly_schedule,
                "available_days": parse_schedule_days(weekly_schedule),
                "working_hours": parse_schedule_hours(weekly_schedule),
            }
            if specialization:
                specialization_map[specialization.lower()] = cat
        if not doctors_dict:
            return get_fallback_doctors()
        if not specialization_map:
            return doctors_dict, get_fallback_doctors()[1]
        return doctors_dict, specialization_map
    except Exception:
        return get_fallback_doctors()

# ---------------------------
# Booking state & basic mapping
# ---------------------------
class SimpleBookingState:
    def __init__(self):
        self.step = 1
        self.condition = None
        self.doctor_id = None
        self.doctor_name = None
        self.specialty = None
        self.location = None
        self.visit_type = None
        self.patient_name = None
        self.contact = None
        self.selected_day = None
        self.selected_time = None
        self.final_slot = None
        # optional clinical/intake fields
        self.duration = None          # e.g., "3 days"
        self.severity = None          # "Low|Medium|High|0-5"
        self.allergies = None         # "penicillin, peanuts"
        self.medications = None       # "omeprazole 20mg daily"
        self.gender = None            # "M|F|Other|N/A"
        self.dob = None               # "YYYY-MM-DD"
        # intake flow flags
        self.asked_optional = False
        self.optional_declined = False
        # validation flag
        self.invalid_contact_notice = False
        # user meta
        self.existing_user = False
        self.last_booking_id = None

    def is_complete(self):
        # Require core scheduling + identity fields only.
        return all([
            self.doctor_id,
            self.doctor_name,
            self.specialty,
            self.location,
            self.visit_type,
            self.patient_name,
            self.contact,
            self.selected_day,
            self.selected_time,
        ])

CONDITIONS = {
    "chest pain":"cardiology","hypertension":"cardiology","palpitations":"cardiology","shortness of breath":"cardiology",
    "acne":"dermatology","eczema":"dermatology","rash":"dermatology","psoriasis":"dermatology","mole":"dermatology",
    "knee pain":"orthopedics","back pain":"orthopedics","shoulder pain":"orthopedics","sprain":"orthopedics","fracture":"orthopedics",
    "headache":"internal","fever":"internal","cold":"internal","migraine":"internal","fatigue":"internal","checkup":"internal","vomiting":"internal","nausea":"internal"
}

def match_condition_to_doctor(condition_text, DOCTORS):
    cl = (condition_text or "").lower()
    for condition, specialty in CONDITIONS.items():
        if condition in cl and specialty in DOCTORS:
            return DOCTORS[specialty]
    return DOCTORS["internal"]

def infer_condition(text: str):
    tl = (text or "").lower()
    for condition in CONDITIONS.keys():
        if condition in tl:
            return condition
    return None

def validate_contact(contact_text):
    email_match = re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', contact_text or "")
    if email_match: return email_match.group()
    digits = re.sub(r'\D', '', contact_text or "")
    if len(digits) >= 10: return digits
    return None

# ---- Users helpers ----

def _normalize_contact(contact: str):
    if (contact or "").find("@") != -1:
        return (contact, None)
    return (None, re.sub(r'\D', '', contact or ""))


def _generate_user_id(users_col):
    try:
        cur = users_col.find({"user_id": {"$regex": r"^u\\d+$"}}).sort([("user_id", -1)]).limit(1)
        last = list(cur)
        if last:
            m = re.search(r"u(\\d+)$", last[0].get("user_id", ""))
            if m:
                return f"u{int(m.group(1)) + 1:03d}"
        c = users_col.count_documents({})
        return f"u{c + 1:03d}"
    except Exception:
        return f"u{int(datetime.now().timestamp())}"


def upsert_user_for_booking(booking: dict, appt_date_dt: datetime):
    if users_collection is None:
        return False
    try:
        email, mobile = _normalize_contact(booking.get("contact", ""))
        name = booking.get("patient_name", "")
        symptoms = booking.get("condition", "unspecified")
        date_str = appt_date_dt.strftime("%m-%d-%Y")

        med = booking.get("medical", {}) or {}
        allergies   = med.get("allergies")
        medications = med.get("medications")
        gender      = med.get("gender") or booking.get("gender")
        dob         = med.get("dob") or booking.get("dob")

        or_filters = []
        if email: or_filters.append({"email": email})
        if mobile: or_filters.append({"mobile": mobile})
        filt = {"$or": or_filters} if or_filters else {"name": name}

        existing = users_collection.find_one(filt)
        if existing is None:
            user_id = _generate_user_id(users_collection)
            doc = {
                "user_id": user_id,
                "name": name,
                "mobile": mobile or "NA",
                "email": email or "NA",
                "dob": dob or "NA",
                "gender": gender or "NA",
                "height": "NA",
                "weight": "NA",
                "blood_group": "NA",
                "emergency_contact": "NA",
                "Chronic_condition": "NA",
                "diet_preferance": "NA",
                "last_appointment": date_str,
                "total_appointments": 1,
                "symptoms": symptoms,
                "allergies": allergies or "None",
                "medications": medications or "None",
                "history": "None",
            }
            users_collection.insert_one(doc)
        else:
            update_set = {
                "name": name or existing.get("name", ""),
                "email": email or existing.get("email", "NA"),
                "mobile": mobile or existing.get("mobile", "NA"),
                "last_appointment": date_str,
                "symptoms": symptoms or existing.get("symptoms", ""),
            }
            if dob:        update_set["dob"] = dob
            if gender:     update_set["gender"] = gender
            if allergies:  update_set["allergies"] = allergies
            if medications:update_set["medications"] = medications

            users_collection.update_one(
                {"_id": existing["_id"]},
                {"$set": update_set, "$inc": {"total_appointments": 1}},
            )
        return True
    except Exception:
        return False

# ---------------------------
# MODEL-DRIVEN FLOW (JSON only)
# ---------------------------
AI_SYSTEM = """
You are MedBird, a courteous medical appointment booking assistant.
You must return ONLY JSON, no extra text, using this exact schema:
{
  "say": "STRING (<=2 sentences) — what to show the user next",
  "set": {
    "condition": "STRING",
    "visit_type": "in-person|telehealth",
    "patient_name": "STRING",
    "contact": "STRING",
    "selected_day": "Monday|Tuesday|...",
    "selected_time": "e.g., 10:00 AM",

    "duration": "e.g., 3 days",
    "severity": "Low|Medium|High|0-5",
    "allergies": "comma list",
    "medications": "free text",
    "gender": "M|F|Other|N/A",
    "dob": "YYYY-MM-DD"
  },
  "done": false
}

Rules:
- If the user asks a QUESTION (e.g., “what is telehealth?”), answer briefly in “say” and DO NOT set visit_type unless they explicitly choose it.
- If the user explicitly says "telehealth" (or synonyms: virtual, video visit, online), set visit_type=telehealth and DO NOT switch to in-person unless they later ask to change it. Likewise, if they say "in-person" (or "in person"), set visit_type=in-person and do not switch away unless requested.
- Convert vague times like “tomorrow morning” into a concrete weekday and a time that is inside the provided working hours.
- Never invent unavailable days/hours. Stay within the provided availability.
- Keep “say” helpful, friendly, and short. If more info is needed, end “say” with exactly one clear question.
- Ask OPTIONAL clinical intake (duration, severity, allergies, medications) **at most once**. If the user declines or says “nothing else,” **do not ask again**.
- Map numeric severity 0–1→Low, 2–3→Medium, 4–5→High.
- **Do not book or mark done until BOTH patient name and contact are captured.**
- **When patient_name, contact, visit_type, selected_day and selected_time are all set, and you have NOT asked clinical intake yet, your NEXT `say` MUST (in one short sentence) ask for allergies and current medications (optional), then ask for confirmation to book.**
- **If the user declines the optional clinical intake and all core details are present, set `done=true` and confirm the booking.**
- **After explicit user confirmation (e.g., “yes/confirm/book it”), set done=true and do NOT ask for other times or additional preferences.**
- Only set "done": true when ALL scheduling details are set and the user has explicitly confirmed to book (or when the user declines optional intake after core details are present).
"""

def _extract_json(txt: str):
    if not txt:
        return None
    m = re.search(r'\{.*\}', txt, flags=re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except Exception:
        return None

def _booking_context(state, doctor):
    return {
        "state": {
            "condition": state.condition,
            "visit_type": state.visit_type,
            "patient_name": state.patient_name,
            "contact": state.contact,
            "selected_day": state.selected_day,
            "selected_time": state.selected_time,
            # optional (for awareness)
            "duration": state.duration,
            "severity": state.severity,
            "allergies": state.allergies,
            "medications": state.medications,
            "gender": state.gender,
            "dob": state.dob,
            "asked_optional": state.asked_optional,
            "optional_declined": state.optional_declined,
        },
        "doctor": {
            "id": doctor.get("id"),
            "name": doctor.get("name"),
            "specialty": doctor.get("specialty"),
            "available_days": doctor.get("available_days"),
            "working_hours": doctor.get("working_hours"),
            "location": doctor.get("location"),
        }
    }

def _apply_updates(state, updates: dict):
    if not updates:
        return
    # core fields
    for k in ["condition","visit_type","patient_name","contact","selected_day","selected_time"]:
        if k in updates and updates[k]:
            if k == "contact":
                vc = validate_contact(updates[k])
                if not vc:
                    state.invalid_contact_notice = True
                    continue
                setattr(state, k, vc)
            else:
                setattr(state, k, updates[k])
    # optional intake fields
    for k in ["duration","severity","allergies","medications","gender","dob"]:
        if k in updates and updates[k]:
            setattr(state, k, updates[k])
    # compute final slot if we have day+time
    if state.selected_day and state.selected_time:
        today = datetime.now()
        days_until = (list(calendar.day_name).index(state.selected_day) - today.weekday()) % 7
        if days_until == 0: days_until = 7
        appt_date = today + timedelta(days=days_until)
        state.final_slot = f"{state.selected_day}, {appt_date.strftime('%B %d')} at {state.selected_time}"

def ai_driver(user_text, state, doctors):
    """Delegate flow to the model."""
    # If no doctor chosen yet, map from user input or current condition
    if state.doctor_id is None:
        if state.condition is None:
            ic = infer_condition(user_text)
            if ic: state.condition = ic
        doc = match_condition_to_doctor(user_text or state.condition or "", doctors)
        state.doctor_id = doc["id"]; state.doctor_name = doc["name"]
        state.specialty = doc["specialty"]; state.location = doc["location"]

    # Build context for the model
    doc_info = next((v for v in doctors.values() if v["id"] == state.doctor_id), doctors["internal"])
    availability = {"available_days": doc_info["available_days"], "working_hours": doc_info["working_hours"]}
    ctx = _booking_context(state, doc_info)

    msgs = [
        {"role": "system", "content": AI_SYSTEM + "\n\nAvailability:\n" + json.dumps(availability)},
        {"role": "user", "content": f"Context: {json.dumps(ctx)}"},
        {"role": "user", "content": user_text},
    ]

    # If model isn't available, return a soft fallback
    if (not HAS_IBM) or model is None or params is None:
        # Minimal helpful fallback
        missing = []
        if not state.patient_name: missing.append("your full name")
        if not state.contact: missing.append("your email or 10-digit phone")
        if not state.visit_type: missing.append("in-person or telehealth")
        if not state.selected_day or not state.selected_time: missing.append("a preferred day and time")
        if missing:
            return {"say": "Thanks! Please share " + ", ".join(missing) + ".", "set": {}, "done": False}
        # gentle optional ask once
        if (not state.asked_optional) and (not state.optional_declined):
            return {"say": "(Optional) Any allergies or current medications? If not, say 'no'.", "set": {}, "done": False}
        return {"say": "Say 'confirm' to finalize your booking.", "set": {}, "done": False}

    try:
        resp = model.chat(messages=msgs, params=params)
        raw = resp["choices"][0]["message"]["content"]
        data = _extract_json(raw)
        if data is None:
            return {"say": raw.strip()[:400], "set": {}, "done": False}
        return data
    except Exception:
        return {"say": "Sorry, I didn’t catch that. When would you like to schedule your appointment?", "set": {}, "done": False}

# ---------------------------
# Persistence helpers
# ---------------------------

def save_appointment_and_user(booking_data: dict) -> bool:
    """Insert the appointment and upsert the users collection."""
    if appointments_collection is None:
        return False
    try:
        appointment_doc = {
            **booking_data,
            "booking_id": f"apt_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{booking_data['doctor_id']}",
            "status": "confirmed",
            "created_at": datetime.now(),
            "booking_method": "streamlit_chatbot"
        }
        appointments_collection.insert_one(appointment_doc)

        # Compute appointment date (next occurrence of selected_day)
        today = datetime.now()
        try:
            days_until = (list(calendar.day_name).index(booking_data.get("selected_day", "Monday")) - today.weekday()) % 7
        except Exception:
            days_until = 0
        if days_until == 0:
            days_until = 7
        appt_date_dt = today + timedelta(days=days_until)

        # Upsert user profile (best-effort)
        try:
            upsert_user_for_booking(booking_data, appt_date_dt)
        except Exception:
            pass

        return True
    except Exception:
        return False

# ---------------------------
# Email notification helpers (optional)
# ---------------------------

def _extract_email_from_contact(contact: str):
    c = (contact or "").strip()
    return c if ("@" in c and "." in c) else None


def send_email_via_smtp(to_email: str, subject: str, body_text: str) -> bool:
    if not (MAIL_HOST and MAIL_USER and MAIL_PASS and MAIL_FROM):
        if DEBUG_EMAIL:
            st.toast("Email not configured: check host/user/pass/from.", icon="📭")
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = MAIL_FROM
        msg["To"] = to_email
        msg.set_content(body_text)

        context = ssl.create_default_context()
        with smtplib.SMTP(MAIL_HOST, MAIL_PORT) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(MAIL_USER, MAIL_PASS)
            server.send_message(msg)
        if DEBUG_EMAIL:
            st.toast(f"Email sent to {to_email}", icon="📧")
        return True
    except Exception as e:
        if DEBUG_EMAIL:
            st.toast(f"SMTP error: {e}", icon="⚠️")
        return False


def notify_patient_email(booking: dict) -> bool:
    if not MAIL_ENABLED:
        if DEBUG_EMAIL:
            st.toast("Email disabled in secrets ([mail].enabled=false)", icon="📭")
        return False
    to_email = _extract_email_from_contact(booking.get("contact"))
    if not to_email:
        if DEBUG_EMAIL:
            st.toast("No valid email in contact; skipping email.", icon="📭")
        return False

    subject = f"Your MedBird appointment with {booking.get('doctor_name')} is confirmed"
    slot = booking.get("appointment_slot") or f"{booking.get('selected_day')} at {booking.get('selected_time')}"
    patient = booking.get("patient_name") or "Patient"
    visit = booking.get("visit_type") or "appointment"
    dept  = booking.get("location") or "Clinic"

    summary_lines = []
    if booking.get("condition"): summary_lines.append(f"Reason: {booking['condition']}")
    med = booking.get("medical") or {}
    if med.get("allergies"):   summary_lines.append(f"Allergies: {med['allergies']}")
    if med.get("medications"): summary_lines.append(f"Medications: {med['medications']}")
    summary = ("".join(summary_lines)) if summary_lines else "(No clinical details provided)"

    body = f"""Hi {patient},

Your {visit} with {booking.get('doctor_name')} is confirmed.

When: {slot}
Where: {dept}

Details:
{summary}

If you need to reschedule, reply to this email.

— MedBird"""
    return send_email_via_smtp(to_email, subject, body)

# ---------------------------
# UI — draw first, then init backends; show messages after
# ---------------------------

st.markdown('<h1 class="main-header">🏥 MedBird</h1>', unsafe_allow_html=True)
st.markdown('<div class="intro-text">Your AI-powered medical appointment booking assistant.<br>Tell me your symptoms and I\'ll help you book with the right doctor!</div>', unsafe_allow_html=True)

with st.spinner("Connecting to services…"):
    appointments_collection, doctors_collection, users_collection, model, params, init_msgs = init_connections_cached(
        MONGO_URI, DB_NAME, use_mongo=True, use_ibm=True
    )



# Doctors (from DB if available; otherwise fallback)
DOCTORS, SPECIALIZATION_MAP = load_doctors_from_db(doctors_collection)

# Booking state init
if st.session_state["booking_state"] is None:
    st.session_state["booking_state"] = SimpleBookingState()

# Chat history
with st.container():
    if not st.session_state["messages"]:
        with st.chat_message("assistant"):
            st.markdown("👋 Hello! I’m MedBird. What symptoms are you experiencing today?")
    for msg in st.session_state["messages"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

# Chat input & flow
user_text = st.chat_input("Type your message…")
if user_text:
    # Add user msg
    st.session_state["messages"].append({"role":"user","content":user_text})

    # Respect explicit visit-type preference before calling the model
    _maybe_set_visit_type_from_text(user_text, st.session_state["booking_state"]) 
    # Ask model (model sees the updated visit_type already)
    result = ai_driver(user_text, st.session_state["booking_state"], DOCTORS)

    # Apply updates from model
    _apply_updates(st.session_state["booking_state"], result.get("set"))
    state = st.session_state["booking_state"]

    # Re-assert user preference if the model tried to flip it
    _maybe_set_visit_type_from_text(user_text, state)


    # Confirmation & optional-intake guards
    CONFIRM_RE = re.compile(r"\b(yes|yep|yeah|confirm|confirmed|book it|go ahead|that works|sounds good|looks good|ok|okay|that's correct|correct)\b", re.I)
    DECLINE_OPT_RE = re.compile(r"\b(no|nope|none|nothing else|that's it|that is all|no other)\b", re.I)

    # If user declines optional after we asked once, remember it
    auto_finalize = False
    if state.asked_optional and DECLINE_OPT_RE.search(user_text or ""):
        state.optional_declined = True
        auto_finalize = state.is_complete()

    # Build assistant message
    to_say = result.get("say") or "OK."

    # Contact validity notice
    if state.invalid_contact_notice:
        to_say += "\n\n⚠️ That contact info doesn’t look valid—please double-check your email or enter a 10-digit phone number."
        state.invalid_contact_notice = False

    # Detect if the model is asking optional intake now
    if not state.asked_optional and re.search(r"allerg|medicat|severity|duration", to_say, re.I):
        state.asked_optional = True

    # If optional was declined, strip any repeated ask
    if state.optional_declined and re.search(r"allerg|medicat|severity|duration", to_say, re.I):
        to_say = "You're all set."

    # Decide if we should finalize regardless of model's 'done'
    done = bool(result.get("done"))
    if (not done) and state.is_complete() and CONFIRM_RE.search(user_text or ""):
        done = True

    # Gentle one-time optional-intake nudge when core fields are present
    if (not done) and (not state.asked_optional) and (not state.optional_declined):
        core_ok = all([
            state.patient_name, state.contact, state.visit_type,
            state.selected_day, state.selected_time
        ])
        if core_ok and not re.search(r"allerg|medicat|severity|duration", to_say, re.I):
            to_say += "\n\n(Optional) Any allergies or current medications? If not, just say 'no'."
            state.asked_optional = True

    if (auto_finalize or done) and state.is_complete():
        # Ensure final_slot is computed
        if not state.final_slot and state.selected_day and state.selected_time:
            today = datetime.now()
            days_until = (list(calendar.day_name).index(state.selected_day) - today.weekday()) % 7
            if days_until == 0:
                days_until = 7
            appt_date = today + timedelta(days=days_until)
            state.final_slot = f"{state.selected_day}, {appt_date.strftime('%B %d')} at {state.selected_time}"

        booking = {
            "patient_name": state.patient_name,
            "contact": state.contact,
            "condition": state.condition or "unspecified",
            "doctor_id": state.doctor_id,
            "doctor_name": state.doctor_name,
            "specialty": state.specialty,
            "location": state.location,
            "visit_type": state.visit_type,
            "appointment_slot": state.final_slot,
            "selected_day": state.selected_day,
            "selected_time": state.selected_time,
            # optional intake -> saved into appointment; Agent 2 can use them
            "duration": state.duration,
            "triage": {"severity": state.severity},
            "medical": {
                "allergies": state.allergies,
                "medications": state.medications,
                "gender": state.gender,
                "dob": state.dob,
            },
        }
        saved = save_appointment_and_user(booking)
        email_note = ""
        if saved and MAIL_ENABLED:
            try:
                if notify_patient_email(booking):
                    email_note = " I've also emailed your confirmation."
                else:
                    email_note = " (Email could not be sent, but your appointment is confirmed.)"
            except Exception:
                email_note = " (Email could not be sent, but your appointment is confirmed.)"
        elif saved and not MAIL_ENABLED:
            email_note = " (Email notifications are currently disabled.)"
        confirm = f"Perfect! Your {state.visit_type} appointment with {state.doctor_name} is confirmed for {state.final_slot}. You'll receive a confirmation at {state.contact}.{email_note}"
        if not saved:
            confirm += " (Note: failed to save to DB; please screenshot this confirmation.)"
        st.session_state["messages"].append({"role":"assistant","content":confirm})
        st.balloons()
        st.session_state["booking_state"] = SimpleBookingState()
    else:
        st.session_state["messages"].append({"role":"assistant","content":to_say})

    st.rerun()

# Sidebar
with st.sidebar:
    st.markdown("### 🏥 Available Specialists")
    for _, doctor in DOCTORS.items():
        st.markdown(f"""
        <div class=\"doctor-info\">
            <strong>{doctor['name']}</strong><br>
            <em>{doctor['specialty']}</em><br>
            📅 {', '.join(doctor['available_days'])}<br>
            🕐 {doctor['working_hours']}
        </div>
        """, unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("### 💡 Tips")
    st.markdown(
        """
    - Describe your symptoms clearly  
    - Ask questions freely (e.g., “what is telehealth?”)  
    - Say “tomorrow morning” or a weekday + time for scheduling  
    - Type “telehealth” if you want a virtual appointment
    """
    )

    # Test email tool (optional)
    st.markdown("---")
    st.markdown("### ✉️ Send test email")
    if MAIL_ENABLED:
        test_to = st.text_input("To email", value="", placeholder="you@example.com")
        colA, colB = st.columns(2)
        with colA:
            test_subj = st.text_input("Subject", value="MedBird test email")
        with colB:
            pass
        test_body = st.text_area("Body", value="This is a test email from MedBird.")
        if st.button("Send test email"):
            if test_to.strip():
                ok = send_email_via_smtp(test_to.strip(), test_subj.strip(), test_body)
                if ok:
                    st.success("Test email sent.")
                else:
                    st.error("Failed to send test email. Enable debug in secrets to see errors.")
            else:
                st.warning("Enter a recipient email address.")
    else:
        st.info("Email is OFF. Add [mail] settings in secrets.toml to enable.")

    if st.button("🔄 Start New Conversation"):
        st.session_state["messages"] = []
        st.session_state["booking_state"] = SimpleBookingState()
        st.rerun()

st.markdown("---")
st.markdown("*Powered by IBM watsonx (optional) and built with ❤️ using Streamlit*", unsafe_allow_html=True)
