import os, json, time, argparse, smtplib
from pathlib import Path
from datetime import datetime, timedelta
from email.mime.text import MIMEText

# Anchor all paths to project root (one level up from agents/)
BASE = Path(__file__).resolve().parents[1]
REMINDERS_PATH = BASE / "data" / "reminders.json"
DEFAULT_DOCTORS_PATH = BASE / "config" / "doctors.json"

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default

def save_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")

def _send_email(to_email, subject, body):
    # NOTE: Prefer env vars; if empty, skip sending
    smtp_user = os.getenv("SMTP_USER", "siddharths2709@gmail.com")
    smtp_pass = os.getenv("SMTP_PASS", "qmgf oleb bwow slkd")
    if not smtp_user or not smtp_pass or not to_email:
        print("[reminder] SMTP creds missing or no recipient; skipping email.")
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

def add_reminder(booking, offsets_minutes=(120, 10)):
    """
    Register reminders for a booking.
    Default: 120 minutes (2 hours) and 10 minutes before the appointment.
    If you also want day-before, change to (1440, 120, 10).
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
    print(f"[reminder] Registered {len(offsets_minutes)} reminders for {slot} -> {offsets_minutes}")

def loop(doctors_path: Path, interval=60):
    doctors = load_json(doctors_path, {})
    print(f"[reminder] Loop running. Checking every {interval}s\n  reminders: {REMINDERS_PATH}\n  doctors:   {doctors_path}")
    while True:
        now_min = datetime.now().strftime("%Y-%m-%d %H:%M")  # minute precision
        store = load_json(REMINDERS_PATH, {"reminders": []})
        changed = False

        for r in store["reminders"]:
            if r.get("sent"):
                continue

            if r.get("remind_at") == now_min:
                b = r["booking"]
                doctor = doctors.get(b.get("doctor_id"), {})
                doc_email = doctor.get("email")
                doc_name  = doctor.get("name", "Doctor")

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
    ap.add_argument("--doctors", help="Path to doctors.json (optional)", default=str(DEFAULT_DOCTORS_PATH))
    ap.add_argument("--loop", action="store_true", help="Run scheduler loop continuously")
    ap.add_argument("--interval", type=int, default=60, help="Loop check interval seconds")
    args = ap.parse_args()

    doctors_path = Path(args.doctors)

    if args.booking:
        booking = json.loads(args.booking)
        # Default offsets now 2h and 10m
        add_reminder(booking, offsets_minutes=(120, 10))

    if args.loop:
        loop(doctors_path, args.interval)

if __name__ == "__main__":
    main()
