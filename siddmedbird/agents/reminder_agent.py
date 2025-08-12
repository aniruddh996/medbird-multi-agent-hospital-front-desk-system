import os, json, time, argparse
from pathlib import Path
from datetime import datetime, timedelta
import smtplib
from email.mime.text import MIMEText

REMINDERS_PATH = Path("data/reminders.json")
DOCTORS_PATH   = Path("config/doctors.json")

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default

def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")

def _send_email(to_email, subject, body):
    smtp_user = os.getenv("SMTP_USER", "siddharths2709@gmail.com")
    smtp_pass = os.getenv("SMTP_PASS", "qmgf oleb bwow slkd")
    if not smtp_user or not smtp_pass:
        print("[reminder] SMTP creds missing; skipping email.")
        return
    msg = MIMEText(body)
    msg["From"] = os.getenv("EMAIL_FROM", smtp_user)
    msg["To"] = to_email
    msg["Subject"] = subject
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(msg["From"], [to_email], msg.as_string())
    print(f"[reminder] Sent email -> {to_email}")

def add_reminder(booking, offsets_minutes=(24*60, 120)):
    """
    Register reminders for a booking. offsets_minutes are minutes before appt (e.g., 1440 = 1 day, 120 = 2 hours).
    """
    store = load_json(REMINDERS_PATH, {"reminders": []})
    slot = booking.get("slot") or booking.get("datetime")
    slot_dt = datetime.strptime(slot, "%Y-%m-%d %H:%M")

    for mins in offsets_minutes:
        remind_at = (slot_dt - timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M")
        store["reminders"].append({
            "booking": booking,
            "remind_at": remind_at,
            "sent": False,
            "kind": f"T-{mins}m"
        })
    save_json(REMINDERS_PATH, store)
    print(f"[reminder] Registered {len(offsets_minutes)} reminders for {slot}")

def loop(interval=60):
    doctors = load_json(DOCTORS_PATH, {})
    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        store = load_json(REMINDERS_PATH, {"reminders": []})
        changed = False

        for r in store["reminders"]:
            if r.get("sent"): 
                continue
            if r.get("remind_at") == now:
                b = r["booking"]
                doctor = doctors.get(b.get("doctor_id"), {})
                doc_email = doctor.get("email")
                doc_name  = doctor.get("name","Doctor")

                # Patient email if contact is an email
                contact = b.get("contact") or b.get("patient_email")
                if contact and "@" in contact:
                    _send_email(
                        contact,
                        "Appointment Reminder",
                        f"Reminder: Your appointment with {doc_name} is at {b.get('slot') or b.get('datetime')}.\n- MedBird"
                    )

                # Doctor email
                if doc_email:
                    _send_email(
                        doc_email,
                        "Upcoming Appointment Reminder",
                        f"Reminder: You have an appointment with {b.get('patient_name','')} at {b.get('slot') or b.get('datetime')}."
                    )

                # Mock SMS for phone contacts
                if contact and "@" not in contact:
                    print(f"[reminder] (MOCK SMS) to {contact}: Appt {b.get('slot') or b.get('datetime')} with {doc_name}")

                r["sent"] = True
                changed = True

        if changed:
            save_json(REMINDERS_PATH, store)
        time.sleep(interval)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--booking", help="JSON booking to register reminders")
    ap.add_argument("--doctors", help="Path to doctors.json (optional)")
    ap.add_argument("--loop", action="store_true", help="Run scheduler loop")
    ap.add_argument("--interval", type=int, default=60, help="Loop check interval seconds")
    args = ap.parse_args()

    if args.booking:
        booking = json.loads(args.booking)
        add_reminder(booking)
    if args.loop:
        loop(args.interval)

if __name__ == "__main__":
    main()
