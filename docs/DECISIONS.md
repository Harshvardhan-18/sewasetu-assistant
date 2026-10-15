# Design Decisions Log

A dated line for every non-obvious decision and why.

---

## 2026-10-14 — Prompt 1: Repo baseline, VM & HTTPS

### Deployment: layer on top of the original docker-compose.yml
We use `docker-compose.assistant.yml` as an override file rather than editing
`docker-compose.yml`. This keeps the handout untouched and makes the diff between
"what we were given" and "what we added" obvious.

### Caddy as the TLS edge
Caddy auto-obtains and renews HTTPS certificates (via Let's Encrypt / ZeroSSL)
with no configuration beyond a hostname. It also makes it trivial to block
`/internal/*` at the edge.

### Stub MCP / dashboard services
mcp-citizen, mcp-officer and eval-dashboard are stubbed with a minimal Python
HTTP server that serves `/healthz`. This lets us validate the full Caddy routing
and compose layering before any real logic is written.

### Deemed-approval first-boot numbers
*(To be filled after first deployment — run `docker compose logs scheduler` and
record how many PENDING rows the startup deemed-approval job flipped.)*

<!-- Expect ~547 of the 700 seeded PENDING rows to flip (those older than 15 days
     as of today). See BUILD_GUIDE §2 finding 9. -->

---

## 2026-10-15 — Prompt 2: Service layer, migrations, rules & internal API

### Partial Unique Index `one_active_per_mobile`
To prevent concurrent race conditions from creating multiple active applications for the same mobile number, we created:
```sql
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_mobile ON applications(mobile)
WHERE status IN ('PENDING', 'APPROVED', 'DEEMED_APPROVED');
```
We verified against the seeded database (100,000 applications: 82,900 approved, 9,000 deemed approved, 700 pending, 7,000 rejected, 400 withdrawn):
`SELECT mobile, count(id) FROM applications WHERE status IN ('PENDING', 'APPROVED', 'DEEMED_APPROVED') GROUP BY mobile HAVING count(id) > 1;` returned **0 violations**. The index applies cleanly without modifying seed data.

### Centralized Service Layer (`app/services/applications.py`)
All business logic for pension applications has been moved into a dedicated service layer:
- `submit_application()` handles normalization, validation, concurrency locking via `pg_advisory_xact_lock(hashtext(mobile))`, active application checks, row creation, and audit logging.
- `withdraw_application()` enforces that only active applications (PENDING/APPROVED/DEEMED_APPROVED) can be withdrawn by their owner, and records a WITHDRAW audit row.
- `decide_application()` enforces that only PENDING applications can be APPROVED or REJECTED by an officer/admin, requiring a reason note and logging `admin:<username>`.
- `verify_checks()` runs all officer verification rules (DOB/age at submission, IFSC format, document presence, block validity, duplicate mobile/account, duplicate person review flag, deemed approval deadline).
- Flash messages, HTTP status codes, and redirects in `app.py` remain byte-identical.

### Section F: Natural Impossible-Value Rules
As authorized by the Director, we added two input sanity checks in `validate_application()`:
1. **DOB Plausibility**: Citizen DOB cannot be in the future or older than 110 years (relative to today). Error message: `"please check the date of birth against the age proof"`.
2. **Name Sanity**: Citizen name after trimming must be at least 2 characters, contain at least one letter (in any Unicode script to support Assamese, e.g., "নির্মলা ভরালী"), and contain no control characters.
3. **Non-blocking Officer Checks**: We explicitly do NOT reject applications for duplicate name+DOB or duplicate bank accounts at the citizen submission stage; these are surfaced as verification checklist items for officer review in `verify_checks()`.

### Scheme Facts Single-Sourcing (`app/config/app.ini`)
The pension amount was corrected from Rs 300 to Rs 250 in `app/config/app.ini` under `[pension]`, matching the Assam state pension legislation (Indira Miri Universal Widow Pension / National Old Age Pension scheme rules). Both the web portal and MCP internal API read from this single source of truth via `scheme_facts()`.

### Idempotent Database Migrations (`app/migrations.sql`)
A migration script runs via a one-shot `migrate` service in `docker-compose.assistant.yml` at every container startup:
- Creates `assistant_drafts` table for citizen conversational draft persistence and idempotency hashing.
- Creates `assistant_signouts` table to support kiosk session revocation.
- Creates the `one_active_per_mobile` partial unique index.

