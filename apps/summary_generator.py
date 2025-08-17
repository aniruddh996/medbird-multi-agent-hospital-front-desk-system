# doctor_summary_agent.py
# -------------------------------------------------
# Quick start:
#   pip install --upgrade streamlit pymongo ibm-watsonx-ai
#   python -m streamlit run "doctor_summary_agent.py"
#
# Optional: .streamlit/secrets.toml (same folder)
# [ibm]
# api_key    = "..."
# url        = "https://us-south.ml.cloud.ibm.com"
# project_id = "..."
# [mongo]
# uri = "..."
# db  = "medbird"
# [clinic]
# tz  = "America/New_York"
# -------------------------------------------------

import os, json, re, calendar
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import streamlit as st

# Optional deps
HAS_WX = True
try:
    from ibm_watsonx_ai.foundation_models import ModelInference
    from ibm_watsonx_ai.foundation_models.schema import TextChatParameters
except Exception:
    HAS_WX = False

HAS_PYMONGO = True
try:
    from pymongo import MongoClient
except Exception:
    HAS_PYMONGO = False

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore

# -------------------------------------------------
# Config helpers
# -------------------------------------------------

def _env_or_secret(env: str, section: str, key: str, default: str = "") -> str:
    v = os.getenv(env)
    if v is not None and v.strip() != "":
        return v
    try:
        return st.secrets.get(section, {}).get(key, default)
    except Exception:
        return default

CLINIC_TZ = _env_or_secret("CLINIC_TZ", "clinic", "tz", "America/New_York")
MODEL_ID      = _env_or_secret("WX_MODEL_ID", "ibm", "model_id", "ibm/granite-3-3-8b-instruct")
WX_URL        = _env_or_secret("WX_URL", "ibm", "url", "https://us-south.ml.cloud.ibm.com")
WX_API_KEY    = _env_or_secret("WX_API_KEY", "ibm", "api_key", "")
WX_PROJECT_ID = _env_or_secret("WX_PROJECT_ID", "ibm", "project_id", "")
MONGO_URI     = _env_or_secret("MONGO_URI", "mongo", "uri", "")
DB_NAME       = _env_or_secret("DB_NAME",  "mongo", "db",  "medbird")

# -------------------------------------------------
# Prompt
# -------------------------------------------------
SYSTEM = """You are a clinical intake summarizer. Convert the provided intake JSON into the EXACT human-readable summary below. Follow this format precisely—same section titles, order, punctuation, and dash bullets. Output ONLY the text summary (no JSON, no explanations, no code fences).

Format:
Patient: <Full name>
Age: <number or N/A>
Gender: <value or N/A>
Contact: <phone/email or N/A>
Appointment ID: <id>
Scheduled Time: <DD Mon YYYY, h:mm AM/PM (TZ)>

Reason for Visit:
- Symptoms: <comma-separated symptoms or N/A>
- Duration: <N days/weeks or N/A>
- Severity: <Low|Medium|High>
- Suspected cause: <text or N/A>

Medical Background:
- Allergies: <comma-separated or None>
- Chronic conditions: <comma-separated or None>
- Current medications: <name + dose + frequency or None>
- Relevant history: <brief or None>

Triage Notes:
- Urgency: <Low|Medium|High> – <short rationale or N/A>
- Flag: <brief flag or None>

Booking Details:
- Doctor: <Dr. Full Name> (<Specialty>)
- Appointment Type: <In-person|Telehealth>
- Location: <clinic address or N/A>

Rules:
- Do not invent facts; if unknown, write 'N/A' or 'None' as appropriate.
- Map numeric severity (severity_score) to: 0–1=Low, 2–3=Medium, ≥4=High. If a text 'triage'/'severity' is provided, use it.
- 'Urgency' mirrors the severity and may include a one-line rationale from symptoms/duration.
- If 'scheduled_time_human' is provided, use it as-is; otherwise convert slot_start (ISO 8601) to 'DD Mon  YYYY, h:mm AM/PM (TZ)' (e.g., '11 Aug 2025, 3:30 PM (EDT)').
- Keep exactly one blank line between sections."""

# -------------------------------------------------
# Mongo connections (cached, pure)
# -------------------------------------------------

@st.cache_resource(show_spinner=False)
def _init_mongo(uri: str, dbname: str):
    if not (HAS_PYMONGO and uri):
        return None, None, None
    client = MongoClient(
        uri,
        serverSelectionTimeoutMS=3000,
        connectTimeoutMS=3000,
        socketTimeoutMS=3000,
        tls=True if "mongodb+srv://" in uri else False,
    )
    db = client[dbname]
    try:
        client.admin.command("ping")
    except Exception:
        pass
    return db.appointments, db.users, db

# -------------------------------------------------
# Utilities
# -------------------------------------------------

def _normalize_contact(contact: str) -> Tuple[Optional[str], Optional[str]]:
    if (contact or "").find("@") != -1:
        return (contact, None)
    digits = re.sub(r"\D", "", contact or "")
    return (None, digits if digits else None)


def _compute_age(dob_str: Optional[str]) -> Optional[int]:
    if not dob_str or dob_str.upper() == "NA":
        return None
    fmts = ["%Y-%m-%d", "%d-%m-%Y", "%m-%d-%Y", "%d/%m/%Y", "%m/%d/%Y"]
    for fmt in fmts:
        try:
            dob = datetime.strptime(dob_str, fmt).date()
            today = datetime.now().date()
            years = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            return max(0, years)
        except Exception:
            continue
    return None


def _next_occurrence(day_name: str, ref: Optional[datetime] = None) -> datetime:
    ref = ref or datetime.now()
    try:
        idx = list(calendar.day_name).index(day_name)
    except ValueError:
        idx = ref.weekday()
    delta = (idx - ref.weekday()) % 7
    if delta == 0:
        delta = 7
    return ref + timedelta(days=delta)


def _enrich_time(payload: Dict[str, Any], tz_name: str = CLINIC_TZ) -> Dict[str, Any]:
    out = dict(payload)
    tz = ZoneInfo(tz_name) if ZoneInfo else None
    # Priority: scheduled_time_human, then slot_start, then selected_day+selected_time
    if "scheduled_time_human" in out and out.get("scheduled_time_human"):
        return out

    if out.get("slot_start"):
        try:
            iso_in = out["slot_start"].replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_in)
            local = dt.astimezone(tz) if tz else dt
            out["scheduled_time_human"] = local.strftime("%d %b %Y, %I:%M %p (%Z)").replace(", 0", ", ")
            return out
        except Exception:
            pass

    # selected_day + selected_time
    sday, stime = out.get("selected_day"), out.get("selected_time")
    if sday and stime:
        try:
            base = _next_occurrence(sday)
            # Parse time like "10:00 AM"
            t = datetime.strptime(stime.upper(), "%I:%M %p").time()
            local = datetime.combine(base.date(), t)
            if tz:
                local = local.replace(tzinfo=tz)
            out["slot_start"] = local.isoformat()
            out["scheduled_time_human"] = local.strftime("%d %b %Y, %I:%M %p (%Z)").replace(", 0", ", ")
        except Exception:
            pass
    return out

# Fallback (no LLM) formatter to guarantee demo works

def _format_summary_deterministic(p: Dict[str, Any]) -> str:
    def pick(*vals):
        for v in vals:
            if v not in (None, "", "NA"): return v
        return "N/A"

    age = p.get("age")
    if age is None:
        age = _compute_age(p.get("dob"))
    contact = pick(p.get("mobile"), p.get("email"), p.get("contact"))
    appt_id = pick(p.get("appointment_id"), p.get("booking_id"))
    t_human = p.get("scheduled_time_human") or "N/A"

    symptoms = p.get("symptoms") or p.get("reason", {}).get("symptoms")
    if isinstance(symptoms, list):
        symptoms = ", ".join([s for s in symptoms if s]) or "N/A"
    symptoms = symptoms or pick(p.get("condition"))

    duration = pick(p.get("duration"), p.get("reason", {}).get("duration"))
    sev_text = p.get("severity") or p.get("triage", {}).get("severity")
    sev_score = p.get("severity_score") or p.get("reason", {}).get("severity_score")
    if not sev_text and sev_score is not None:
        try:
            sev_text = "Low" if sev_score <= 1 else ("Medium" if sev_score <= 3 else "High")
        except Exception:
            pass
    sev_text = sev_text or "N/A"

    cause = pick(p.get("suspected_cause"), p.get("reason", {}).get("suspected_cause"))

    allergies = p.get("allergies") or p.get("medical", {}).get("allergies") or "None"
    chron = p.get("Chronic_condition") or p.get("medical", {}).get("chronic_conditions") or "None"
    meds = p.get("medications") or p.get("medical", {}).get("medications") or "None"
    hist = p.get("history") or p.get("medical", {}).get("history") or "None"

    urgency = p.get("urgency") or p.get("triage", {}).get("urgency") or (sev_text if sev_text in ("Low","Medium","High") else "N/A")
    flag = p.get("flag") or p.get("triage", {}).get("flag") or "None"

    doctor = pick(p.get("doctor_name"))
    spec = pick(p.get("specialty"))
    vtype = (p.get("visit_type") or "").replace("telehealth","Telehealth").replace("in-person","In-person") or "N/A"
    loc = pick(p.get("location"))

    return (
        f"Patient: {pick(p.get('patient_name'), p.get('name'))}\n"
        f"Age: {age if age is not None else 'N/A'}\n"
        f"Gender: {pick(p.get('gender'))}\n"
        f"Contact: {contact}\n"
        f"Appointment ID: {appt_id}\n"
        f"Scheduled Time: {t_human}\n\n"
        f"Reason for Visit:\n"
        f"- Symptoms: {symptoms or 'N/A'}\n"
        f"- Duration: {duration}\n"
        f"- Severity: {sev_text}\n"
        f"- Suspected cause: {cause}\n\n"
        f"Medical Background:\n"
        f"- Allergies: {allergies}\n"
        f"- Chronic conditions: {chron}\n"
        f"- Current medications: {meds}\n"
        f"- Relevant history: {hist}\n\n"
        f"Triage Notes:\n"
        f"- Urgency: {urgency} – {'N/A' if urgency=='N/A' else 'as per reported symptoms'}\n"
        f"- Flag: {flag}\n\n"
        f"Booking Details:\n"
        f"- Doctor: {doctor} ({spec})\n"
        f"- Appointment Type: {vtype}\n"
        f"- Location: {loc}"
    )

# -------------------------------------------------
# LLM summarizer
# -------------------------------------------------

def generate_summary_llm(payload: Dict[str, Any]) -> str:
    if not (HAS_WX and WX_API_KEY and WX_PROJECT_ID and WX_URL):
        return _format_summary_deterministic(payload)
    model = ModelInference(
        model_id=MODEL_ID,
        credentials={"apikey": WX_API_KEY, "url": WX_URL},
        project_id=WX_PROJECT_ID,
    )
    params = TextChatParameters(temperature=0.1, max_tokens=700)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    try:
        resp = model.chat(messages=messages, params=params)
        out = resp["choices"][0]["message"]["content"].strip()
        # Safety: ensure no JSON/code fences leaked
        if out.startswith("{") or out.startswith("```"):
            return _format_summary_deterministic(payload)
        return out
    except Exception:
        return _format_summary_deterministic(payload)

# -------------------------------------------------
# Build intake from latest appointment
# -------------------------------------------------

def _fetch_latest(ap_col, users_col) -> Optional[Dict[str, Any]]:
    if ap_col is None:
        return None
    doc = ap_col.find_one({}, sort=[("created_at", -1)])
    if not doc:
        return None
    intake: Dict[str, Any] = {
        "patient_name": doc.get("patient_name"),
        "contact": doc.get("contact"),
        "appointment_id": doc.get("booking_id"),
        "condition": doc.get("condition"),
        "symptoms": [doc.get("condition")] if doc.get("condition") else None,
        "doctor_name": doc.get("doctor_name"),
        "specialty": doc.get("specialty"),
        "location": doc.get("location"),
        "visit_type": doc.get("visit_type"),
        "selected_day": doc.get("selected_day"),
        "selected_time": doc.get("selected_time"),
    }
    # enrich from users
    if users_col is not None:
        email, mobile = _normalize_contact(intake.get("contact"))
        filt = {"$or": ([{"email": email}] if email else []) + ([{"mobile": mobile}] if mobile else [])}
        user = users_col.find_one(filt) if filt.get("$or") else users_col.find_one({"name": intake.get("patient_name")})
        if user:
            intake.update({
                "email": user.get("email"),
                "mobile": user.get("mobile"),
                "dob": user.get("dob"),
                "gender": user.get("gender"),
                "allergies": user.get("allergies", "None"),
                "Chronic_condition": user.get("Chronic_condition", "NA"),
                "medications": user.get("medications", "None"),
                "history": user.get("history", "None"),
            })
    return intake

# Extra queries for doctor-specific views

def _distinct_doctors(ap_col):
    if ap_col is None:
        return []
    try:
        vals = ap_col.distinct('doctor_name')
        return sorted([v for v in vals if v])
    except Exception:
        return []


def _fetch_recent_by_doctor(ap_col, users_col, doctor_name: str, limit: int = 5):
    if ap_col is None or not doctor_name:
        return []
    try:
        cur = ap_col.find({"doctor_name": doctor_name}, sort=[("created_at", -1)]).limit(int(limit))
    except Exception:
        return []
    items = []
    for doc in cur:
        intake: Dict[str, Any] = {
            "patient_name": doc.get("patient_name"),
            "contact": doc.get("contact"),
            "appointment_id": doc.get("booking_id"),
            "condition": doc.get("condition"),
            "symptoms": [doc.get("condition")] if doc.get("condition") else None,
            "doctor_name": doc.get("doctor_name"),
            "specialty": doc.get("specialty"),
            "location": doc.get("location"),
            "visit_type": doc.get("visit_type"),
            "selected_day": doc.get("selected_day"),
            "selected_time": doc.get("selected_time"),
        }
        # enrich from users
        if users_col is not None:
            email, mobile = _normalize_contact(intake.get("contact"))
            filt = {"$or": ([{"email": email}] if email else []) + ([{"mobile": mobile}] if mobile else [])}
            user = users_col.find_one(filt) if filt.get("$or") else users_col.find_one({"name": intake.get("patient_name")})
            if user:
                intake.update({
                    "email": user.get("email"),
                    "mobile": user.get("mobile"),
                    "dob": user.get("dob"),
                    "gender": user.get("gender"),
                    "allergies": user.get("allergies", "None"),
                    "Chronic_condition": user.get("Chronic_condition", "NA"),
                    "medications": user.get("medications", "None"),
                    "history": user.get("history", "None"),
                })
        items.append(_enrich_time(intake, CLINIC_TZ))
    return items

# -------------------------------------------------
# Streamlit UI
# -------------------------------------------------

st.set_page_config(page_title="🧾 MedBird – Doctor Handoff", page_icon="🧾", layout="centered")
st.title("🧾 Doctor Handoff Summarizer")

with st.spinner("Connecting…"):
    ap_col, users_col, _db = _init_mongo(MONGO_URI, DB_NAME)

status = []
if not HAS_WX:
    status.append("watsonx SDK missing → fallback formatter active")
if not WX_API_KEY or not WX_PROJECT_ID:
    status.append("IBM credentials not set → fallback formatter active")
if ap_col is None:
    status.append("Mongo not connected → using sample booking")

if status:
    st.info("\n".join(f"• {s}" for s in status))

# ---- Selection controls ----
doctors = _distinct_doctors(ap_col) if ap_col is not None else []
colf1, colf2, colf3 = st.columns([2,1,1])
with colf1:
    doc_sel = st.selectbox("Doctor", options=(['(All doctors)'] + doctors) if doctors else ['(All doctors)'])
with colf2:
    mode = st.selectbox("Mode", ["Latest overall", "Latest for selected doctor", "Batch for selected doctor"])
with colf3:
    batch_n = st.number_input("Batch size", min_value=1, max_value=10, value=5) if mode.endswith("Batch for selected doctor") else 5

latest = None
batch_items = []
if mode == "Latest overall":
    latest = _fetch_latest(ap_col, users_col) if ap_col is not None else None
elif mode == "Latest for selected doctor" and doc_sel != "(All doctors)":
    items = _fetch_recent_by_doctor(ap_col, users_col, doc_sel, limit=1)
    latest = items[0] if items else None
elif mode == "Batch for selected doctor" and doc_sel != "(All doctors)":
    batch_items = _fetch_recent_by_doctor(ap_col, users_col, doc_sel, limit=int(batch_n))
    latest = batch_items[0] if batch_items else None

with st.expander("Input payload", expanded=True):
    if latest is None:
        sample = {
            "patient_name": "Aniruddh Rajagopal",
            "contact": "simontruelove@gmail.com",
            "condition": "headache, nausea",
            "doctor_name": "Dr. Maya Patel",
            "specialty": "Internal Medicine",
            "location": "Internal Medicine Department",
            "visit_type": "in-person",
            "selected_day": "Tuesday",
            "selected_time": "12:00 PM",
            "email": "simontruelove@gmail.com",
            "gender": "M",
            "dob": "1996-04-05",
        }
        st.caption("No record loaded; using sample payload. You can edit below.")
        raw = st.text_area("Edit JSON", value=json.dumps(sample, indent=2), height=260)
    else:
        raw = st.text_area("Edit JSON", value=json.dumps(latest, indent=2), height=260)

# Show batch preview (if any)
if batch_items:
    st.markdown("**Batch preview (most recent first):**")
    for i, it in enumerate(batch_items, 1):
        st.write(f"{i}. {it.get('patient_name','N/A')} — {it.get('selected_day','?')} {it.get('selected_time','?')} — {it.get('doctor_name','N/A')}")

try:
    payload = json.loads(raw)
except Exception:
    st.error("Invalid JSON. Please fix and try again.")
    st.stop()

# Enrich time fields for the LLM / fallback
payload = _enrich_time(payload, CLINIC_TZ)
st.session_state['batch_payloads'] = batch_items if batch_items else None

col1, col2, col3 = st.columns([1,1,1])
with col1:
    gen = st.button("Generate Summary", type="primary")
    if gen:
        try:
            with st.spinner("Generating summary…"):
                summary = generate_summary_llm(payload)
            st.session_state["last_summary"] = summary
            st.session_state["last_payload"] = payload
        except Exception as e:
            st.error(f"Failed to generate summary: {e}")
with col2:
    if st.button("Copy Latest to Clipboard"):
        if "last_summary" in st.session_state:
            st.toast("Copy the summary from the box below.", icon="📋")
        else:
            st.toast("Generate a summary first.", icon="ℹ️")
with col3:
    if st.session_state.get('batch_payloads') and st.button("Generate Batch Summary"):
        try:
            texts = []
            with st.spinner("Generating batch…"):
                for p in st.session_state['batch_payloads']:
                    texts.append(generate_summary_llm(p))
            st.session_state['last_batch_summary'] = ("---").join(texts)
        except Exception as e:
            st.error(f"Batch failed: {e}")


if "last_summary" in st.session_state:
    st.subheader("Summary")
    st.code(st.session_state["last_summary"], language="markdown")

if 'last_batch_summary' in st.session_state:
    st.subheader("Batch Summary")
    st.code(st.session_state['last_batch_summary'], language="markdown")

    # Optional: store handoff record
    if _db is not None and st.button("Save batch to handoffs"):
        try:
            _db.handoffs.insert_one({
                "created_at": datetime.now(),
                "payload": st.session_state.get("last_payload"),
                "summary": st.session_state.get("last_batch_summary"),
                "type": "batch",
                "doctor": doc_sel if 'doc_sel' in locals() else None,
            })
            st.success("Saved batch to handoffs ✅")
        except Exception as e:
            st.warning(f"Could not save handoff: {e}")

st.markdown("---")
st.caption("Uses IBM watsonx Granite when available; otherwise falls back to a deterministic formatter. Timezone configurable via [clinic] tz in secrets.")