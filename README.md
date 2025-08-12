# Medbird
A multi-agent hospital front desk system that automates patient intake, appointment booking, clinical summary generation, cost estimation, and notifications — streamlining workflows for both patients and healthcare staff.

Here is a basic idea of all the agents and their purpose (might change)
## Agent 1 — Intake, Match, Schedule & Persist (OWNER)
Purpose: End-to-end booking in one convo.
Does: verify/create patient → capture symptoms + duration → severity score → match best doctor → confirm slot → write to DB (users, appointments) → emit events for other agents.
Emits: BOOKING_CONFIRMED, optional NEEDS_COST_ESTIMATE, SAFETY_FLAG.

## Agent 3 — Notifications & Prep
Purpose: Reduce no-shows and keep everyone informed.
Triggers: BOOKING_CONFIRMED, reschedule/cancel.
Does: patient SMS/email + calendar invite; doctor ping; prep instructions (telehealth link or fasting note); adaptive reminder cadence.
Emits: MESSAGE_SENT, REMINDER_SCHEDULED.

## Agent 4 — Doctor Pre-Visit Summary
Purpose: One screen the doc can skim before the visit.
Triggers: BOOKING_CONFIRMED.
Does: pull history (if any), allergies/meds, symptoms + duration, severity score, booking details, red-flag checklist; post to EHR/inbox.
Emits: DOCTOR_SUMMARY_READY.

## Agent 5 — Insurance & Cost Estimator
Purpose: Answer “how much?” and prevent front-desk surprises.
Triggers: NEEDS_COST_ESTIMATE or BOOKING_CONFIRMED with cost intent.
Does: eligibility check; copay/out-of-pocket estimate for visit type/specialty; optional pre-pay link.
Emits: COST_ESTIMATE_READY.

## Agent 6 — Safety & Escalation Guard
Purpose: Catch dangerous symptom combos and advise escalation.
Triggers: live intake stream from Agent 1.
Does: detect ER patterns (e.g., chest pain + dyspnea); inject safety message; optionally bump priority/notify on-call; mark appointment as “priority.”
Emits: SAFETY_ALERT, PRIORITY_UPDATED.

## Agent 7 — Post-Visit Patient Summary & eRx
Purpose: Close the loop after the visit.
Triggers: VISIT_COMPLETED.
Does: generate plain-language summary (diagnosis, meds, dosage, red flags, follow-ups); send eRx; schedule follow-up/labs if ordered.
Emits: AFTER_VISIT_SUMMARY_READY, ERX_SENT, FOLLOWUP_SCHEDULE_NEEDED.

## Agent 8 — No-Show & Wait-Time Predictor
Purpose: Smooth flow and staffing.
Triggers: continuous feed of schedules/outcomes.
Does: predict no-show risk (feeds Agent 3 cadence); estimate wait times by hour/doctor; suggest micro-rebalancing.
Emits: NOSHOW_RISK_SCORED, WAIT_TIME_ESTIMATE.

## Agent 9 — Data Quality & Compliance Guard
Purpose: Keep data clean and auditable (HIPAA-minded).
Triggers: DB writes & logs.
Does: validate contact formats, dedup by phone/email; enforce consent flags; redact PHI in non-clinical logs; maintain audit trails.
Emits: DATA_CLEANED, DUPLICATE_FLAGGED, AUDIT_LOGGED.

## Agent 10 — Admin Analytics & Capacity Planner
Purpose: Real-time KPIs + forecasts for ops.
Triggers: streaming events (BOOKING_CONFIRMED, NOSHOW_RISK_SCORED, etc.).
Does: dashboards (arrivals, wait times, utilization, cancellations, revenue leakage); demand forecasts; staffing/rooming recommendations.
Emits: CAPACITY_RECOMMENDATION, KPI_REPORT_READY.
