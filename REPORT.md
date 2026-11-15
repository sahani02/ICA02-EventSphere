# EventSphere — Agent-Based Venue Booking and Digital Ticketing
**IT4123 Agent Based Computing Practical — ICA 02**

Student name: S.H.Maddumage
Student ID: 2021/ICT/15
Submission date: 15 November 2026

Live URL:https://ica02-eventsphere.onrender.com
GitHub repository: https://github.com/sahani02/ICA02-EventSphere.git

## 1. Introduction and objectives
EventSphere is a mobile-oriented university venue booking and ticketing prototype. It enables administrators to manage venues and events, attendees to obtain tickets and request private hire, and gate staff to validate tickets. Five cooperating agents make scheduling, ticket allocation, check-in, waitlist and notification decisions. Payments are simulated; no card data is collected.

## 2. Architecture
The responsive HTML/CSS/JavaScript browser client calls a FastAPI REST API with Pydantic validation. The API submits structured messages to an in-process message bus. The five agents process requests using persistent relational tables via SQLAlchemy. SQLite is used locally; the deployment is configured for PostgreSQL. The message bus serializes messages to JSON, dispatches by recipient and records both requests and replies. The NotificationAgent subscribes to INFORM broadcasts. A background task invokes WaitlistAgent every 15 seconds when there are expired offers.

Architecture diagram (draw or screenshot this in your report):
Browser → FastAPI (authentication/CSRF) → JSON Message Bus → Five agents → Relational database
Timer → Message Bus → WaitlistAgent → NotificationAgent / BookingAgent (through bus)

### Persistent agent beliefs and decision rules
| Agent | Persistent state | Rules |
|---|---|---|
| VenueAgent | venues, events, hires | Enforce capacity and future valid start/end; refuse conflicting publication within 30-minute buffer; routine hires confirmed; >100 guests or >6 hours escalated; price = hours × venue rate; offer available alternatives on conflicts. Revalidate at approval. |
| BookingAgent | bookings, categories | Only published future events; 1–6 tickets per booking; quantity ≤ total minus sold minus held. Simulated payment reference. Refund 100% at least 24 h before start, otherwise 50% before start. No attendee cancellation after start or after use. Event cancellation gives full refunds. |
| TicketAgent | tickets, codes, scan count, status | Cryptographically random unique codes. Validate correct event, validity, status and check-in window (2 h before start to end). Reject duplicates and cancellations. |
| WaitlistAgent | waitlist, hold expiry, held inventory | FIFO offers when inventory frees; hold for five minutes; prevent sale of held seats; permit only owner to claim before expiry; 15-second proactive expiry and re-offer. |
| NotificationAgent | notifications | React to INFORM broadcasts and store message for affected user. |

### Message structure and trace
A message contains sender, receiver, performative, content and conversation_id. REQUEST asks an agent to make a decision. AGREE reports acceptance, REFUSE includes reasons, PROPOSE returns alternatives and a reason, INFORM communicates state changes, and FAILURE is available in the schema for errors. The same conversation ID is retained in downstream messages. A booking request is routed from Web to BookingAgent; BookingAgent checks capacity, requests TicketAgent to issue codes, broadcasts INFORM, and receives a response through the bus. The dashboard displays the persisted trace. 

## 3. Database, concurrency and security
Relational entities: users, venues, events, categories, bookings, tickets, hires, waitlist, notifications, messages and allocation_mutex. Foreign keys connect the entities. The agent decision transaction acquires a database row-write lock by updating the mutex row before reading inventory and persists booking, ticket, counter and message changes atomically. A second transaction waits until the first commits and then sees the remaining capacity. This prevents the last seat being sold twice; the approach deliberately serializes allocation operations rather than maximizing throughput. Passwords use salted PBKDF2-SHA256 and constant-time verification. Signed sessions, CSRF tokens, server-side role checks and environment-based deployment configuration are implemented. Ticket codes use cryptographically secure randomness. HTTPS is provided by the cloud host if deployment succeeds.

## 4. Testing and results
The starter implementation passed 14 Pytest tests on SQLite in its development environment. Re-run these tests locally and update this section with your own result and screenshot. Tests cover booking and unique codes, overbooking, quantity limit, double scan, wrong event, cancellation refund, venue capacity, hire alternatives, hire price and confirmation, waitlist expiry/re-offer, notification and trace, full event refund, concurrent last-seat allocation, and role/CSRF enforcement.

## 5. Deployment and demo accounts
Host: [Render or actual equivalent]. The service uses Python, pip install -r requirements.txt and Uvicorn bound to $PORT. PostgreSQL connection URL, signing secret and demo password are supplied through environment variables, not committed to source control. The README provides local setup and test commands.

| Role | Email | Password |
|---|---|---|
| Administrator | admin@example.com | Hasa02@# |
| Gate staff | staff@example.com | Hasa02@# |
| Attendee | attendee@example.com | Hasa02@# |

## 6. Limitations and conclusion
This prototype uses a synchronous, in-process message bus and a coarse database mutex. It is not designed for horizontal scaling or live payments. QR camera scanning depends on browser permissions and a CDN library. Free Render hosting may sleep, pausing the background timer until the process resumes; its free PostgreSQL database expires after 30 days. PostgreSQL and mobile camera behavior must be verified in the deployed environment. The core architecture demonstrates goal-directed agent rules, persistent state, reactive notification, proactive waitlist processing, and conversation-level traceability.
