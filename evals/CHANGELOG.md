# Evaluation Changelog

This document tracks fixes, refinements, and architectural learnings derived from running automated evaluations against the SewaSetu AI Assistant.

---

## Baseline Preparation & Smoke Iteration

### Iteration 0.1 — JSON Serialization of DB Dates
- **Affected Persona**: C01-kamala
- **Failure Cause**: `TypeError: Object of type date is not JSON serializable` when saving run results to JSON.
- **Change**: Updated `evals/run.py` to use `json.dump(..., default=str)` across all result files and summaries.
- **Commit**: `35c524a`
- **Result**: Run results serialize properly with ISO date formatting.

### Iteration 0.2 — Eval Assets Endpoint
- **Affected Personas**: C01, C05, C07, C09, C10, C14, C16, C21, E01
- **Failure Cause**: Simulated citizens providing image URLs received 404 from `/eval-dashboard/assets/` because stub server only handled `/healthz`.
- **Change**: Added valid voter ID assets (<=100 KB) in `evals/assets/` and updated HTTP handler to serve assets with correct MIME types (`image/jpeg`, `application/pdf`).
- **Commit**: `b6116c2`
- **Result**: MCP server successfully fetches age proof documents via HTTPS.

### Iteration 0.3 — Rate-Limit Backoff & History Window
- **Affected Personas**: Multi-turn evaluations on free-tier LLM endpoints.
- **Failure Cause**: HTTP 429 errors from Groq API when rapid tool calling exceeded TPM limits; OpenAI SDK internal retries conflicted with custom backoff.
- **Change**: Set `max_retries=0` on OpenAI client, implemented exponential backoff respecting `Retry-After`, capped simulator context to recent 8 turns, and added `--pace-seconds` throttle.
- **Commit**: `b7307b6`
- **Result**: 100% resilient recovery from 429 rate limit spikes without dropping evaluations.

### Iteration 0.4 — Simulator `[END]` Lifecycle & Graceful Session Close
- **Affected Personas**: C01-kamala and all multi-turn citizen personas.
- **Failure Cause**: Early harness broke conversation loop immediately upon seeing `[END]`, preventing the assistant from receiving the citizen's final sign-off and calling `end_my_session`.
- **Change**: Stripped `[END]` token, delivered clean message to assistant, allowed assistant to execute tool calls (`end_my_session`), recorded closing turn, and then terminated. Added automated test in `tests/evals/test_harness.py`.
- **Commit**: `94d700d`
- **Result**: Session hygiene maintained; kiosk security rules properly exercised; test suite 100% green.

---
