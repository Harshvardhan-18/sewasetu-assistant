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


