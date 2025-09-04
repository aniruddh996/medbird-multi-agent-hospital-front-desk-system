# MedBird — Agentic Appointment & Intake (IBM watsonx)

### Submission for the IBM TechXchange 2025 Pre-Conference watsonx Hackathon (Agentic AI track).

MedBird is an AI front-desk assistant for clinics. It books appointments from natural language, validates contact info, stores to MongoDB, emails the patient a confirmation, and generates a concise doctor-ready summary.

### Agent 1 
Booking & Notifications: Streamlit chatbot + MongoDB + SMTP email

### Agent 2 
Clinical Summary: Fixed-format patient summary for doctors (Streamlit)

AI: IBM watsonx Granite via ModelInference.chat


# Demo

https://youtu.be/vM9zD4_DfPU

# Why MedBird

Scheduling and intake create bottlenecks for clinics. Patients want simple, natural-language booking; doctors want a crisp summary before the visit. MedBird automates both with agentic AI.

# Features

Understands free-text symptoms and maps to the right specialist
Interprets time phrases (“Monday 10am”, “tomorrow morning”) within working hours
Validates email/phone before confirming
Persists to MongoDB (appointments, users upsert)
Sends email confirmations (patient) and optional doctor notifications
Generates a clean, no-hallucination clinical summary for physicians

