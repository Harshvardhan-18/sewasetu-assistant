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

---

## 2026-10-15 — Prompt 3: MCP Common Library & Python SDK Authorization Research

Before building `mcp_common`, we reviewed the official documentation and source code for the Python `mcp` SDK v2 (version 2.2.0).

### 1. `MCPServer`, `AuthSettings`, and `TokenVerifier`
- **Doc URLs:**
  - Authorization: [https://py.sdk.modelcontextprotocol.io/run/authorization/](https://py.sdk.modelcontextprotocol.io/run/authorization/) (source: `docs/run/authorization.md`)
  - Server Migration: [https://py.sdk.modelcontextprotocol.io/migration/](https://py.sdk.modelcontextprotocol.io/migration/)
- **Findings:**
  - `MCPServer` in `mcp.server.mcpserver` is the v2 replacement for `FastMCP`.
  - Over streamable HTTP, the MCP server acts strictly as an **OAuth 2.1 Resource Server** (it does not sign users in or issue tokens; it validates incoming `Authorization: Bearer <token>` headers).
  - The SDK has no opinion on token format; token verification is delegated to a class implementing the `TokenVerifier` protocol (`verify_token(token: str) -> AccessToken | None`).
  - `token_verifier` and `auth` (`AuthSettings`) must always be provided together to `MCPServer`, or a `ValueError` is raised before serving.
  - `AuthSettings` (`mcp.server.auth.settings`) configures:
    - `issuer_url`: the portal's authorization server URL (`PUBLIC_BASE_URL`).
    - `resource_server_url`: the canonical URL of the resource server (e.g. `https://HOST/citizen/mcp` or `https://HOST/officer/mcp`).
    - `required_scopes`: list of required scopes (e.g. `['citizen']` or `['officer']`).
    - `validate_token_resource = True`: enforces that the token's `resource` matches `resource_server_url`.

### 2. `AccessToken` and Context Injection via `get_access_token`
- **Doc URL:** [https://py.sdk.modelcontextprotocol.io/run/authorization/](https://py.sdk.modelcontextprotocol.io/run/authorization/)
- **Findings:**
  - `AccessToken` (`mcp.server.auth.provider`) represents the verified token state: `token`, `client_id`, `scopes`, `expires_at`, `resource`, `subject`, `claims`.
  - During request handling, `AuthContextMiddleware` stores the `AccessToken` in `auth_context_var`.
  - Tools and application code can call `get_access_token()` from `mcp.server.auth.middleware.auth_context` to retrieve the current token context without needing parameters passed in from the client.
  - We expose `current_subject()` in `mcp_common` based on `get_access_token().subject` to guarantee that identity comes solely from the verified JWT.

### 3. Protected Resource Metadata (RFC 9728)
- **Doc URL:** [https://py.sdk.modelcontextprotocol.io/run/authorization/](https://py.sdk.modelcontextprotocol.io/run/authorization/)
- **Findings:**
  - When `resource_server_url` is specified in `AuthSettings`, the SDK automatically registers a route for RFC 9728 metadata at `/.well-known/oauth-protected-resource/<resource_path>`.
  - It serves `{resource, authorization_servers: [issuer_url], scopes_supported: [role], bearer_methods_supported: ["header"]}`.
  - Caddy proxies `/.well-known/oauth-protected-resource/citizen/*` to `mcp-citizen` and `/.well-known/oauth-protected-resource/officer/*` to `mcp-officer`.

### 4. Stateless Streamable HTTP, Custom Endpoint Paths & Transport Security
- **Doc URLs:**
  - ASGI Integration: [https://py.sdk.modelcontextprotocol.io/run/asgi/](https://py.sdk.modelcontextprotocol.io/run/asgi/) (source: `docs/run/asgi.md`)
  - Deployment & Host Allowlisting: [https://py.sdk.modelcontextprotocol.io/run/deploy/](https://py.sdk.modelcontextprotocol.io/run/deploy/) (source: `docs/run/deploy.md`)
- **Findings:**
  - `streamable_http_app(streamable_http_path="/citizen/mcp", stateless_http=True)` returns a Starlette ASGI app.
  - On the modern `2026-07-28` protocol, requests are stateless by design (no `Mcp-Session-Id` header). `stateless_http=True` ensures legacy clients are also treated statelessly.
  - **DNS Rebinding Trap:** By default, `streamable_http_app()` arms DNS-rebinding protection allowing only `localhost` / `127.0.0.1`. When deployed behind a reverse proxy (Caddy), requests with a non-localhost `Host` header are rejected with `421 Misdirected Request` before reaching any tool handler.
  - We pass `transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)` because Caddy terminates TLS and controls host routing at the network edge.

### 5. `WWW-Authenticate` Scope Header Correction
- **Finding:**
  - In `mcp.server.lowlevel.server.RequireAuthMiddleware`, an unauthenticated request returns `401 Unauthorized` with `WWW-Authenticate: Bearer error="invalid_token", error_description="Authentication required", resource_metadata="..."`, but **omits `scope="<role>"`**.
  - Since the Sewa Setu portal defaults to `citizen` scope when no scope is requested, an officer client connecting to `/officer/mcp` needs to know that `scope=officer` is required.
  - We add `EnsureWWWAuthenticateScopeMiddleware` in `mcp_common` to append `scope="<role>"` to `WWW-Authenticate` on 401/403 responses.

### 6. Server `instructions`
- **Doc URL:** [https://py.sdk.modelcontextprotocol.io/servers/](https://py.sdk.modelcontextprotocol.io/servers/)
- **Findings:**
  - `instructions` is passed to `MCPServer(name=..., instructions=...)` and returned in the `initialize` handshake result.
  - Because we do not control the client's system prompt in evaluations, all operational rules, personas, language policies, and kiosk sign-out procedures must be encoded directly in the server `instructions`.



---

## 2026-10-16 — Prompt 3 context: MCP protocol version routing (oauth_smoke fixes)

### Problem 1: stale OTP picked from SMS gateway

`_fetch_otp()` polled the gateway and matched any recent message for the mobile
number. On a retried smoke run the OTP from the *previous* attempt was still in
the gateway and had a newer timestamp than "5 seconds ago", so it was accepted
before the new OTP arrived.

**Fix:** Record the total message count (`_count_gateway_messages()`) as a
watermark *before* triggering the OTP request. In the polling loop, a message is
only considered new when the total list length exceeds the watermark **or** the
message timestamp is ≥ `t_before - 5s`. This is robust even when the gateway
does not return parseable timestamps.

### Problem 2: `initialize` rejected on the 2026-07-28 modern path

**Root cause (read from SDK source `mcp==2.2.0`):**

`StreamableHTTPSessionManager._handle_request()` routes based on the
`MCP-Protocol-Version` HTTP header:

| Header value | Handler | What it expects |
|---|---|---|
| `2026-07-28` (or anything not in handshake list) | `handle_modern_request` | No `initialize`. Every POST is self-contained and must carry `params._meta` with `"io.modelcontextprotocol/protocolVersion"` and `"io.modelcontextprotocol/clientCapabilities"`. |
| `2025-11-25`, `2025-06-18`, `2025-03-26`, `2024-11-05` (handshake list), or **absent** | Legacy stateless or stateful handler | Standard `initialize` → `tools/call` flow. |

`HANDSHAKE_PROTOCOL_VERSIONS = ('2024-11-05','2025-03-26','2025-06-18','2025-11-25')`
`MODERN_PROTOCOL_VERSIONS = ('2026-07-28',)`

The classifier `classify_inbound_request()` (in `mcp.shared.inbound`) runs the
"ladder" and **rejects** any request body that lacks the `_meta` envelope:

```
params._meta must be an object carrying the required
'io.modelcontextprotocol/protocolVersion' and
'io.modelcontextprotocol/clientCapabilities' envelope keys
```

This is the exact 400 error seen when `oauth_smoke.py` sent
`MCP-Protocol-Version: 2026-07-28` with a plain `initialize` body.

**Fix in `oauth_smoke.py`:**
- `call_tool()` uses the **modern path**: no `initialize`, sends `tools/call`
  directly with `params._meta` containing the required envelope.
- `check_legacy_handshake()` uses the **legacy path** (`2025-11-25`): sends
  `initialize` then `tools/call` in the standard handshake style.
- Both paths are exercised in the officer smoke run.

**Server factory unchanged:** `mcp_common/server.py` already works correctly
for both paths. The `StreamableHTTPSessionManager` routes by header; our servers
accept `2026-07-28` (via `handle_modern_request`) and all handshake versions
(via the legacy stateless path). Claude Code, MCP Inspector, and other real
clients that send `2025-11-25` or earlier will work without any server change.

**Sources read:** `mcp/server/streamable_http_manager.py` L183-204,
`mcp/server/_streamable_http_modern.py`, `mcp/shared/inbound.py`
`classify_inbound_request()`, `mcp_types.version` constants.

---

## 2026-10-16 — Prompt 4+5: Citizen MCP, Age Proof, Uploads, Confirmation Code

### Age-Proof Validation (`app/services/proof.py`)

All age-proof logic lives in `app/services/proof.py`:

- **`detect_and_validate_file(data: bytes, declared_ext: str) -> str`** — Reads the first 8 magic
  bytes and matches against a whitelist of PDF (`%PDF-`), JPEG (`FF D8 FF`), and PNG (`89 50 4E 47`)
  signatures. Rejects any file that does not match, regardless of the client-supplied filename or
  Content-Type. Maximum size is 5 MB (enforced before calling this).
- **`save_proof_file(data: bytes, display_name: str) -> str`** — Generates a random UUID-based
  server-side filename, strips the client-supplied name to a safe ASCII-only display name for the
  draft, and writes to the upload directory. The client filename is never used on disk.
- **`fetch_file_url(url: str) -> bytes`** — HTTPS-only fetch with SSRF guards:
  1. URL must use the `https://` scheme.
  2. Resolves the hostname to IP(s); blocks all RFC-1918/loopback/link-local/metadata ranges
     (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 127.0.0.0/8, 169.254.0.0/16, ::1, fc00::/7).
  3. Pins the resolved IP to the socket before connecting (defends against DNS rebinding).
  4. No redirects; hard 10-second timeout; 5 MB byte cap.

### Confirmation Code Design (`assistant_drafts` table)

The confirmation code binds the full draft snapshot to prevent substitution attacks:

```
code = SHA-256(mobile || applicant_name || dob || gender || marital_status ||
               village || block || bank_account || ifsc ||
               doc_path || doc_display_name || scheme_window_closes_at)[:24]  # hex prefix
```

- **Storage:** `assistant_drafts.confirmation_hash` (the hash) and `confirmation_expires_at`
  (30 minutes from when `preview_application` was called).
- **Invalidation:** Any call to `save_application_draft` or `attach_age_proof` clears
  `confirmation_hash` and `confirmation_expires_at` in the draft row.
- **Idempotency:** A successful `submit_application` inserts a row in `assistant_submissions`
  keyed on `(mobile, confirmation_hash)`. A second call with the same code finds the existing
  row and returns the original `application_no` without creating a duplicate.
- **Expiry:** `submit_application` rejects codes where `confirmation_expires_at < now()`.

### `/upload/*` Public Route and Controls

`/upload/direct` is a public, unauthenticated file-writing route deliberately exposed by Caddy.
It is guarded by a single-use DB token in `assistant_upload_tokens`:

| Control | Detail |
|---|---|
| Token binding | `mobile` column; token for mobile A cannot be used for mobile B |
| Single-use | `used_at` column; first use stamps it; reuse returns 409 |
| Expiry | `expires_at = now() + 30 minutes`; checked on every request |
| File type | Magic-byte check: PDF, JPEG, PNG only |
| Size cap | 5 MB; enforced before writing |
| Filename on disk | UUID-generated; client filename never used on disk |
| Personal data | The `/upload/direct` page shows only the scheme name and the upload form — no name, DOB, mobile, or application details |
| Write target | Writes only to the draft's `doc_path`; no arbitrary path traversal |
| Rate limit | 5 token requests per mobile per 10 minutes (enforced in `get_upload_link`) |

Caddy config: `/upload/*` → `app:5000`. `/internal/*` remains blocked at the edge (no backend match).

Tests added in `tests/citizen_mcp/test_server.py`:
- Reused token → 409
- Expired token → 410
- Wrong file type → 400
- Oversized file → 413
- Token for mobile A used for mobile B → 403

### Deemed-Approval Count (Live VM, 2026-10-16)

Query: `SELECT status, count(*) FROM applications GROUP BY status ORDER BY status;`

| status | count |
|---|---|
| APPROVED | 82 900 |
| DEEMED_APPROVED | 9 700 |
| PENDING | 0 (all flipped at first boot) |
| REJECTED | 7 000 |
| WITHDRAWN | 400 |

All `DEEMED_APPROVED` rows have `decided_by = 'RTPS-AUTO'`, set by the nightly 02:00 cron
(`app/scripts/deemed_approval.py`). The cron flips any `PENDING` application that has been
waiting longer than the RTPS statutory limit (currently 15 days per `app/config/app.ini`).

### Counter-Parity Test Strategy (`tests/citizen_mcp/test_counter_parity.py`)

Rules enforced from the service layer with no DB side-effects:

| Test | Mobile | What is asserted |
|---|---|---|
| Age 59 | 9990000040 | `preview_application` returns `ok=False`; 0 rows in `applications` |
| Turns 60 after window | 9990000041 | Same (skipped if window already passed) |
| Window closed (monkeypatch) | 9990000042 | `patch.object(svc, "SCHEME_DEADLINE", past)` → `ok=False`; 0 rows |
| Wrong block | 9990000043 | `ok=False` with "block" in problems; 0 rows |
| 12-char IFSC (`UTBI0PUR2819`) | 9990000044 | `ok=False` with "ifsc" in problems; 0 rows |
| Missing age proof | 9990000045 | `ok=False` with "proof/document" in problems; 0 rows |
| Already PENDING | 9990000046 | Pre-inserted row; submit returns `ok=False`; count stays 1 |
| Already APPROVED | 9990000047 | Same |
| Already DEEMED_APPROVED | 9990000048 | Same |
| REJECTED → re-apply | 9990000049 | Submit succeeds; count becomes 2 |
| WITHDRAWN → prefilled draft | 9990000050 | `get_my_status` shows `has_draft=True`; corrected resubmit succeeds |

Cross-channel and integration tests (same file):

| Test | Assertion |
|---|---|
| MCP submit → web status portal | Application number appears in portal HTML |
| MCP submit → admin list | Application visible at `/admin/applications?status=PENDING` |
| Web flow → `get_my_status` | DB-inserted row returned by MCP tool |
| Assamese name `নির্মলা ভরালী` | DB read-back is byte-identical; `get_my_status` returns it |
| `end_my_session` revocation | `assistant_signouts` row created; `revoked_at > token.iat` |
