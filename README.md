# EventSphere — IT4123 ICA 02

A university venue booking and digital ticketing prototype with five cooperating agents. FastAPI + Pydantic + SQLAlchemy, SQLite for development and PostgreSQL for deployment. Responsive HTML/CSS/JavaScript, QR ticket generation, browser camera scanning and simulated payments.

## Local setup (Python 3.11+)

```sh
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q
python -m uvicorn main:app --reload
```

Open http://127.0.0.1:8000. API docs: http://127.0.0.1:8000/docs.

Local demo accounts: `admin@example.com`, `staff@example.com`, `attendee@example.com`; all use the development-only password `LocalDemo123!`. On Render, all three use the `DEMO_PASSWORD` environment value set before first startup. Do not use your personal password. Database seeding runs only on an empty user table. Changing DEMO_PASSWORD later does not rotate existing accounts. The seeded event starts one hour after the database is initialized; create a fresh published event for the viva if needed.

## Deployment on Render

Create a PostgreSQL database and a Python web service in the same region. Connect this GitHub repository. Build: `pip install -r requirements.txt`. Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`. Configure environment variables before deploying:

| Variable | Value |
| --- | --- |
| DATABASE_URL | Internal PostgreSQL connection URL from Render |
| SECRET_KEY | Random value, generated with `python -c "import secrets; print(secrets.token_hex(32))"` |
| DEMO_PASSWORD | Choose a unique demonstration password (not personal) |

No real payment details are collected. Do not commit these variables to the repository. Use one Uvicorn worker. Render provides HTTPS. Free hosting sleeps and free PostgreSQL expires after 30 days; renew/upgrade for a later viva or use a persistent equivalent service. The timer runs only while the web process is active.

## Agent architecture

Web API → JSON Message Bus → VenueAgent / BookingAgent / TicketAgent / WaitlistAgent / NotificationAgent → relational tables. Each message includes sender, receiver, performative, content and conversation ID. The bus serializes to JSON and dispatches to agent handlers. Replies and INFORM broadcasts/deliveries are persisted; agent-to-agent communication uses the bus rather than invoking another agent's method.

- VenueAgent: venue capacity, start/end validation, 30-minute buffer, event publication, alternative venue/time proposals, hourly hire pricing, escalation for >100 guests or >6 hours.
- BookingAgent: 1–6 tickets, published/future events, available inventory minus holds, simulated payment references, refund 100% at least 24 hours before event or 50% before start, no attendee cancellation after start or after a ticket is used; event cancellation refunds 100% and closes waitlists.
- TicketAgent: cryptographically random unique ticket codes, cancellation, correct event and status checks, check-in from 2 hours before start until end, one admission only, scan counts persist.
- WaitlistAgent: FIFO offers, 5-minute seat hold, claim via BookingAgent, 15-second background expiry sweep with re-offer; holds reduce available inventory.
- NotificationAgent: reacts to broadcast INFORM messages and stores notifications for the recipient.

All booking and calendar mutations take an exclusive database row-write lock on a mutex before reading allocation state and commit in the same transaction. PostgreSQL serializes concurrent writers to this row; SQLite serializes writers. The deliberately coarse lock trades throughput for simpler correctness in the prototype. Persistent tables keep state across process restarts. Passwords use salted PBKDF2-SHA256; role checks protect admin/staff/attendee operations; signed cookie sessions and CSRF tokens protect mutations.

## Tests

`python -m pytest -q` runs 14 tests against SQLite, including concurrent last-seat allocation, repeated scanning, each agent, message tracing, refund, roles and CSRF. PostgreSQL and browser camera require deployment/manual verification; use screenshots from your actual deployment. See REPORT.md for a report draft to adapt and verify.

## Known limitations

Single-process synchronous bus, coarse global locking, a minimal browser UI, URL-based venue images, and CDN-hosted QR scanner library. No real payments, email, or horizontal scaling. The camera requires browser permission and HTTPS or localhost. Free hosting cannot execute timers when asleep. A public demo should not contain personal data; change or remove demo accounts after assessment.
