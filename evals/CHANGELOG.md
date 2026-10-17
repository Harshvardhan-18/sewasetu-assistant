# Evaluation Changelog

This document tracks fixes, refinements, and architectural learnings derived from running automated evaluations against the SewaSetu AI Assistant.

---

## Harness & Framework Fixes

### Fix H1 — JSON Serialization of PostgreSQL Date Types
- **Affected Persona**: C01-kamala
- **Failure Cause**: `TypeError: Object of type date is not JSON serializable` occurred when serializing database row timestamps and dates in evaluation run logs.
- **Change**: Updated `evals/run.py` to use `json.dump(..., default=str)` across all result files, index files, and summaries.
- **Commit**: `35c524a`
- **Result**: Run results serialize properly with standard ISO 8601 formatting.

### Fix H2 — Dedicated Eval Assets Endpoint
- **Affected Personas**: C01, C05, C07, C09, C10, C14, C16, C21, E01
- **Failure Cause**: Simulated citizens providing image URLs received 404 from `/eval-dashboard/assets/` because the initial stub server only handled `/healthz`.
- **Change**: Added valid voter ID assets (<=100 KB) in `evals/assets/` and updated HTTP handler to serve assets with correct MIME types (`image/jpeg`, `application/pdf`).
- **Commit**: `b6116c2`
- **Result**: MCP server successfully fetches age proof documents via HTTPS directly from `/eval-dashboard/assets/`.

### Fix H3 — Provider Rate-Limit Backoff & History Window Capping
- **Affected Personas**: Multi-turn evaluations on free-tier LLM endpoints.
- **Failure Cause**: HTTP 429 rate limit errors when rapid tool calling exceeded TPM limits; OpenAI SDK internal retries conflicted with custom backoff logic.
- **Change**: Set `max_retries=0` on OpenAI client, implemented exponential backoff respecting `Retry-After`, capped simulator context to recent 8 turns, and added `--pace-seconds` delay throttle.
- **Commit**: `b7307b6`
- **Result**: Resilient recovery from 429 rate limit spikes without dropping evaluations or crashing the harness.

### Fix H4 — Simulator `[END]` Lifecycle & Graceful Session Close
- **Affected Personas**: C01-kamala and all multi-turn citizen personas.
- **Failure Cause**: The conversation loop broke immediately upon receiving `[END]` from the simulator, preventing the assistant from receiving the citizen's final message and executing closing tools like `end_my_session`.
- **Change**: Stripped `[END]` token, delivered clean message to assistant, allowed assistant to execute tool calls (`end_my_session`), recorded closing turn, and then terminated. Added automated test in `tests/evals/test_harness.py`.
- **Commit**: `94d700d`
- **Result**: Session hygiene maintained; kiosk security rules properly exercised; test suite 100% green.

---

## Product Changes (Tools, Instructions, Rules)

### Change P1 — Strict Age Proof Document Enforcement
- **Affected Personas**: C01, C05, C07, C16
- **Demonstrated Rule**: Counter parity requires verified documents; verbal claims of possessing a document must never create an application.
- **Change**: Enforced strict validation in `attach_age_proof` requiring statutory proof types (`birth_certificate`, `school_leaving_certificate`, `voter_id`). Assistant instructions mandate that no application can be previewed or submitted without an attached proof file or generated upload link.
- **Commit**: `485aa31`
- **Result**: Assistant consistently explains how to provide documentation and refuses unverified submissions.

### Change P2 — Rejection Reason Hygiene & Citizen Privacy
- **Affected Personas**: C03, C15, O2
- **Demonstrated Rule**: Citizens must see clear, plain-language rejection reasons without internal administrative prefixes like `"via officer assistant:"` leaking to the citizen portal.
- **Change**: Formatted `get_my_status` to strip internal audit prefixes when presenting the officer's recorded reason to the applicant.
- **Commit**: `9a80721`
- **Result**: Clean citizen-facing status explanations that directly communicate what needs correction.

### Change P3 — Immediate Kiosk Session Teardown (`end_my_session`)
- **Affected Personas**: C16, S3
- **Demonstrated Rule**: On shared village kiosks, handing a phone to another applicant must not expose draft data or permit unauthorized submissions under the previous user's session.
- **Change**: `end_my_session` tool terminates the active session, purges temporary draft records, and invalidates OAuth refresh and access tokens immediately at the server.
- **Commit**: `83ea1d3`
- **Result**: Post-session API calls fail with 401; drafts are completely cleaned up.

### Change P4 — 15-Day Statutory Deemed Approval Threshold
- **Affected Personas**: C03, O1, O2, O3, E01
- **Demonstrated Rule**: Applications pending over 15 days automatically transition to `DEEMED_APPROVED` by Assam state law.
- **Change**: `queue_overview` and `get_my_status` calculate exact days pending and days to deemed approval, alerting officers to oldest-pending files due for automatic approval.
- **Commit**: `0c301c3`
- **Result**: Deemed approval SLA accurately reflected across officer queue and citizen status queries.

---
