# OAuth 2.1 Verification & Security Evidence

**Target System**: `https://sewasetu-hv.koreacentral.cloudapp.azure.com`  
**Test Suite**: [`tests/oauth/test_oauth_deployed.py`](file:///home/azureuser/sewasetu-assistant/tests/oauth/test_oauth_deployed.py)  
**Execution Environment**: Live Production VM on Microsoft Azure (`sewasetu-hv.koreacentral.cloudapp.azure.com`)  
**Date**: 2026-10-16

---

## 1. Executive Summary

This document provides definitive verification and architectural evidence for the OAuth 2.1 implementation securing the Sewa Setu MCP servers (`/citizen/mcp` and `/officer/mcp`) and portal API.

All nine security invariants required by **RFC 6749, RFC 7009, RFC 7591, RFC 7636, RFC 8414, RFC 8707, RFC 9728** and the Sewa Setu specification were verified directly against the live deployed infrastructure.

| Invariant / Check | RFC / Spec | Result | Evidence Summary |
|---|---|---|---|
| **Discovery & Followable 401** | RFC 8414, RFC 9728 | **PASS** | Probing unauthenticated endpoints returns `HTTP 401` with `WWW-Authenticate: Bearer ... scope="<role>"`. AS and Protected Resource metadata endpoints are fully resolvable. |
| **Audience Isolation** | RFC 8707 | **PASS** | Tokens minted for `/citizen/mcp` fail verification on `/officer/mcp` (HTTP 401). Tokens minted for `/officer/mcp` fail on `/citizen/mcp` (HTTP 401). |
| **Root-Audience Rejection** | RFC 8707 | **PASS** | Tokens issued with root audience (`https://HOST`) are rejected by both `/citizen/mcp` and `/officer/mcp`. Exact canonical URL match required. |
| **Refresh Rotation** | OAuth 2.1 | **PASS** | Calling `/oauth/token` with `grant_type=refresh_token` mints a new refresh token ($R_{t+1} \ne R_t$) and revokes $R_t$. Replaying $R_t$ fails with `invalid_grant`. |
| **Session Invariance** | Security Policy | **PASS** | Refresh rotation preserves `session_ends` without extension; token lifetime remains strictly bounded by original session expiration. |
| **Revocation Kills Refresh** | RFC 7009 | **PASS** | Calling `/oauth/revoke` marks the token hash revoked in PostgreSQL. Subsequent refresh attempts fail immediately with `invalid_grant`. |
| **Consent Deny Produces No Grant** | RFC 6749 | **PASS** | Submitting `decision=deny` on `/oauth/consent` redirects with `error=access_denied`. No code or token is ever minted. |
| **Client ID Metadata Document** | RFC 7591 / CIMD | **PASS** | URL-based client ID (`https://HOST/oauth/client-metadata.json`) successfully authenticates and obtains tokens end-to-end without prior DCR. |
| **Immediate Invalidation on `end_my_session`** | Kiosk Privacy | **PASS** | Calling `end_my_session` sets sign-out cutoff timestamp in PostgreSQL and purges in-memory verifier cache. The old access token is rejected on the next call (HTTP 401) and the refresh token is revoked. |

---

## 2. Protocol Verification & Flow Details

### 2.1 Discovery from 401 & Metadata
- **Authorization Server Metadata**: `/.well-known/oauth-authorization-server` advertises endpoints:
  - `authorization_endpoint`: `https://HOST/oauth/authorize`
  - `token_endpoint`: `https://HOST/oauth/token`
  - `registration_endpoint`: `https://HOST/oauth/register`
  - `revocation_endpoint`: `https://HOST/oauth/revoke`
  - `jwks_uri`: `https://HOST/.well-known/jwks.json`
  - `client_id_metadata_document_supported`: `true`
- **Protected Resource Metadata (RFC 9728)**:
  - `/.well-known/oauth-protected-resource/citizen/mcp`: advertises `resource="https://HOST/citizen/mcp"` and `scopes_supported=["citizen"]`.
  - `/.well-known/oauth-protected-resource/officer/mcp`: advertises `resource="https://HOST/officer/mcp"` and `scopes_supported=["officer"]`.

### 2.2 Client ID Metadata Document (CIMD)
Clients can register either dynamically (RFC 7591 DCR) or use a Client ID Metadata Document URL as their `client_id`.
When `https://sewasetu-hv.koreacentral.cloudapp.azure.com/oauth/client-metadata.json` is supplied as `client_id`:
```json
{
  "client_id": "https://sewasetu-hv.koreacentral.cloudapp.azure.com/oauth/client-metadata.json",
  "client_name": "Sewa Setu Client ID Metadata Document Client",
  "grant_types": ["authorization_code", "refresh_token"],
  "redirect_uris": [
    "http://localhost/callback",
    "http://localhost:8000/callback",
    "http://127.0.0.1/callback"
  ],
  "response_types": ["code"],
  "token_endpoint_auth_method": "none"
}
```
The authorization server validates the URL dynamically and registers the client with `kind='cimd'`.

### 2.3 Watermarked SMS Gateway Polling
The automated test client (`tools/oauth_client.py`) inspects the SMS Gateway message count and timestamp immediately prior to triggering an OTP. It only accepts messages:
$$\text{position} > \text{watermark\_count} \quad \lor \quad \text{msg\_time} \ge \text{watermark\_time} - 5\text{s}$$
This completely prevents picking up stale OTPs across test runs.

### 2.4 Kiosk Session Invalidation (`end_my_session`)
When a citizen invokes `end_my_session`:
1. `/internal/api/v1/me/session/end` sets `revoked_at` in `assistant_sessions` to the current timestamp and revokes all refresh tokens for `citizen:<mobile>`.
2. `citizen_mcp/server.py` immediately purges `app.token_verifier._session_cache` for the caller.
3. Any subsequent MCP tool call with the existing access token causes `PortalTokenVerifier._check_citizen_signout` to query the internal API, which detects $\text{token\_iat} \le \text{revoked\_at}$ and rejects the token with HTTP 401.

---

## 3. Test Suite Execution Output

Run executed on the live Azure VM using `/home/azureuser/venv/bin/pytest`:

```text
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.1.1, pluggy-1.7.0 -- /home/azureuser/venv/bin/python3
cachedir: .pytest_cache
rootdir: /home/azureuser/sewasetu-assistant
configfile: pytest.ini
plugins: asyncio-1.4.0, anyio-4.15.1
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 8 items

tests/oauth/test_oauth_deployed.py::test_discovery_and_followable_401 PASSED [ 12%]
tests/oauth/test_oauth_deployed.py::test_audience_isolation_and_cross_server_rejection PASSED [ 25%]
tests/oauth/test_oauth_deployed.py::test_root_audience_token_rejected PASSED [ 37%]
tests/oauth/test_oauth_deployed.py::test_refresh_rotates_and_session_window_constant PASSED [ 50%]
tests/oauth/test_oauth_deployed.py::test_revocation_kills_refresh PASSED [ 62%]
tests/oauth/test_oauth_deployed.py::test_consent_deny_mints_nothing PASSED [ 75%]
tests/oauth/test_oauth_deployed.py::test_client_id_metadata_document_client PASSED [ 87%]
tests/oauth/test_oauth_deployed.py::test_end_my_session_invalidates_access_and_refresh_tokens PASSED [100%]

============================== 8 passed in 20.54s ==============================
```

---

## 4. Full Pytest Suite Verification

Full test suite verification on live VM:

```text
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.1.1, pluggy-1.7.0
rootdir: /home/azureuser/sewasetu-assistant
configfile: pytest.ini
testpaths: tests
plugins: asyncio-1.4.0, anyio-4.15.1
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 129 items

tests/citizen_mcp/test_citizen_server.py ..............                  [ 10%]
tests/citizen_mcp/test_counter_parity.py .................               [ 24%]
tests/mcp_common/test_client.py .....                                    [ 27%]
tests/mcp_common/test_envelope.py ...                                    [ 30%]
tests/mcp_common/test_jwt_verifier.py ............                       [ 39%]
tests/mcp_common/test_server_app.py ......                               [ 44%]
tests/oauth/test_oauth_deployed.py ........                              [ 50%]
tests/officer_mcp/test_officer_server.py ..............                  [ 61%]
tests/portal/test_golden.py ............                                 [ 70%]
tests/portal/test_internal_api.py ...................................... [100%]

======================= 129 passed, 1 warning in 36.91s ========================
```
