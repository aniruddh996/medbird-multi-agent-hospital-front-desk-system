import os, json, smtplib, argparse
from email.mime.text import MIMEText
from pathlib import Path

def _send_email(to_email, subject, body, sender=None):
    smtp_user = os.getenv("SMTP_USER", "siddharths2709@gmail.com")
    smtp_pass = os.getenv("SMTP_PASS", "qmgf oleb bwow slkd")
    smtp_server = "smtp.gmail.com"
    smtp_port = 587
    sender = sender or os.getenv("EMAIL_FROM", smtp_user)

    if not smtp_user or not smtp_pass:
        print("[notifier] SMTP creds missing; skipping real email.")
        return 0

    msg = MIMEText(body)
    msg["From"] = sender
    msg["To"] = to_email
    msg["Subject"] = subject

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(sender, [to_email], msg.as_string())
    print(f"[notifier] Sent email -> {to_email}")
    return 0

def send_notifications(booking, doctors_path: str):
    doctors = json.loads(Path(doctors_path).read_text(encoding="utf-8"))
    doctor = doctors.get(booking.get("doctor_id"), {})
    doctor_email = doctor.get("email")
    doctor_name  = doctor.get("name", booking.get("doctor_name","Doctor"))

    # Patient email
    patient_email = booking.get("contact") or booking.get("patient_email")
    if patient_email and "@" in patient_email:
        _send_email(
            patient_email,
            "Your Appointment Confirmation",
            (f"Hello {booking.get('patient_name','')},\n\n"
             f"Your appointment with {doctor_name} is booked for {booking.get('slot') or booking.get('datetime')}.\n"
             f"Visit type: {booking.get('visit_type','in_person')}\nLocation: {booking.get('location','')}\n\n- MedBird")
        )

    # Doctor email
    if doctor_email:
        _send_email(
            doctor_email,
            "New Appointment Booked",
            (f"Hello {doctor_name},\n\n"
             f"New appointment with patient {booking.get('patient_name','')} on {booking.get('slot') or booking.get('datetime')}.\n"
             f"Condition: {booking.get('condition','')}\nVisit type: {booking.get('visit_type','in_person')}\n\n- MedBird")
        )

    # Mock SMS (just prints)
    phone = booking.get("contact")
    if phone and "@" not in phone:
        print(f"[notifier] (MOCK SMS) to {phone}: Appt on {booking.get('slot') or booking.get('datetime')} with {doctor_name}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--booking", required=True, help="JSON booking payload")
    ap.add_argument("--doctors", required=True, help="Path to doctors.json")
    args = ap.parse_args()

    booking = json.loads(args.booking)
    send_notifications(booking, args.doctors)

if __name__ == "__main__":
    main()
