#!/usr/bin/env python3
"""
scripts/oauth_smoke.py — headless OAuth 2.1 smoke test for Sewa Setu MCP servers.

Performs the full PKCE + DCR flow for both citizen and officer roles, calls the
`whoami` tool on each MCP server, and prints a pass/fail summary.

Usage (terminal):
    python scripts/oauth_smoke.py [--host HOST] [--mobile MOBILE]
                                  [--admin-user USER] [--admin-pass PASS]
                                  [--smsgw SMSGW_BASE] [--role citizen|officer|both]

Environment variables (all overridable by CLI flags):
    HOST            https://sewasetu-hv.koreacentral.cloudapp.azure.com
    ADMIN_USERNAME  (from .env)
    ADMIN_PASSWORD  (from .env)
    SMSGW_URL       http://localhost:8025  (inside compose network: http://smsgw:8025)

Importable API for the eval harness:
    from scripts.oauth_smoke import obtain_token, call_tool, smoke_all, SmokeConfig

Returns exit code 0 if every checked step passes, 1 on the first failure.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from dataclasses import dataclass
from typing import Any

# ── colour helpers ────────────────────────────────────────────────────────────

_TTY = sys.stdout.isatty()
_GRN = "\033[32m" if _TTY else ""
_RED = "\033[31m" if _TTY else ""
_YLW = "\033[33m" if _TTY else ""
_RST = "\033[0m"  if _TTY else ""


def _ok(msg: str) -> None:
    print(f"  {_GRN}✓{_RST} {msg}")


def _fail(msg: str) -> None:
    print(f"  {_RED}✗{_RST} {msg}")


def _info(msg: str) -> None:
    print(f"  {_YLW}·{_RST} {msg}")


# ── low-level HTTP helpers ────────────────────────────────────────────────────

def _get(url: str, *, headers: dict | None = None, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _post_form(url: str, data: dict, *, timeout: int = 10) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _post_json(url: str, payload: dict, *, timeout: int = 10) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ── PKCE helpers ──────────────────────────────────────────────────────────────

def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge_S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ── No-redirect opener helper ─────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop at the first 3xx and surface the Location header via HTTPError."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        raise urllib.error.HTTPError(newurl, code, msg, headers, fp)


def _no_redirect_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        _NoRedirect(),
    )


# ── OTP fetch from SMS gateway ────────────────────────────────────────────────

def _fetch_otp(smsgw_base: str, mobile: str, *, after: float, timeout: int = 25) -> str:
    """
    Poll GET {smsgw_base}/api/messages until an OTP for `mobile` arrives.
    `after` is a UNIX timestamp; messages older than that are skipped.
    Returns the 6-digit OTP string.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msgs = _get(f"{smsgw_base}/api/messages", timeout=5)
        except Exception:
            time.sleep(1)
            continue
        items = msgs if isinstance(msgs, list) else msgs.get("messages", [])
        for msg in reversed(items):
            to = str(msg.get("to", "") or msg.get("recipient", ""))
            if mobile not in to:
                continue
            # Parse timestamp — try several field names
            ts_str = msg.get("created_at") or msg.get("timestamp") or ""
            if ts_str:
                try:
                    import datetime as _dt
                    ts_epoch = _dt.datetime.fromisoformat(
                        str(ts_str).replace("Z", "+00:00")
                    ).timestamp()
                    if ts_epoch < after - 5:  # 5 s clock-skew grace
                        continue
                except Exception:
                    pass  # unparseable timestamp → accept
            body = str(msg.get("body", "") or msg.get("text", "") or msg.get("message", ""))
            m = re.search(r"\b(\d{6})\b", body)
            if m:
                return m.group(1)
        time.sleep(1)
    raise TimeoutError(f"OTP for {mobile} not received within {timeout}s")


# ── DCR ───────────────────────────────────────────────────────────────────────

def _dcr(portal_base: str) -> str:
    """Dynamic Client Registration → client_id."""
    resp = _post_json(
        f"{portal_base}/oauth/register",
        {
            "client_name": "oauth_smoke.py",
            "redirect_uris": ["http://localhost/callback"],
            "token_endpoint_auth_method": "none",
        },
    )
    return resp["client_id"]


# ── Headless authorize → extract rid ─────────────────────────────────────────

def _start_auth(portal_base: str, client_id: str, redirect_uri: str,
                role: str, canonical_url: str) -> tuple[str, str]:
    """
    Start the authorization flow; return (rid, state).
    Sends GET /oauth/authorize with PKCE params, follows the redirect to
    /oauth/login?rid=…, and extracts rid.
    """
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(12)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": role,
        "resource": canonical_url,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{portal_base}/oauth/authorize?" + urllib.parse.urlencode(params)
    opener = _no_redirect_opener()
    location = ""
    try:
        opener.open(auth_url)
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location", "")

    qs = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    rid = (qs.get("rid") or [""])[0]
    if not rid:
        raise RuntimeError(f"Could not extract rid from /oauth/authorize → Location: {location!r}")
    return rid, verifier, state


# ── Headless login ────────────────────────────────────────────────────────────

def _login_citizen(portal_base: str, rid: str, mobile: str, smsgw_base: str) -> None:
    """POST mobile, read OTP from SMS gateway, POST OTP."""
    login_url = f"{portal_base}/oauth/login?rid={rid}"
    t_before = time.time()

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    # Step 1: POST mobile number
    body1 = urllib.parse.urlencode({"mobile": mobile}).encode()
    req1 = urllib.request.Request(login_url, data=body1, method="POST")
    req1.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with opener.open(req1) as _:
            pass
    except urllib.error.HTTPError:
        pass  # page likely re-renders; that's fine

    # Step 2: Fetch OTP
    otp = _fetch_otp(smsgw_base, mobile, after=t_before)
    _info(f"OTP received: {otp}")

    # Step 3: POST OTP
    body2 = urllib.parse.urlencode({"otp": otp}).encode()
    req2 = urllib.request.Request(login_url, data=body2, method="POST")
    req2.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with opener.open(req2) as _:
            pass
    except urllib.error.HTTPError:
        pass


def _login_officer(portal_base: str, rid: str, username: str, password: str) -> None:
    """POST credentials to the portal login page."""
    login_url = f"{portal_base}/oauth/login?rid={rid}"
    body = urllib.parse.urlencode({"username": username, "password": password}).encode()
    req = urllib.request.Request(login_url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        with opener.open(req) as _:
            pass
    except urllib.error.HTTPError:
        pass


# ── Headless consent → extract authorization code ────────────────────────────

def _consent_and_get_code(portal_base: str, rid: str) -> str:
    """
    POST decision=allow to /oauth/consent?rid=, capture the 302 Location,
    return the authorization code.
    """
    consent_url = f"{portal_base}/oauth/consent?rid={rid}"
    body = urllib.parse.urlencode({"decision": "allow"}).encode()
    req = urllib.request.Request(consent_url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    opener = _no_redirect_opener()
    location = ""
    try:
        opener.open(req)
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location", "")

    if not location:
        raise RuntimeError(
            "Consent POST did not produce a redirect. "
            "The rid may have already been consumed or the login step failed."
        )
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    if "error" in qs:
        raise RuntimeError(f"Authorization server returned error: {qs}")
    code = (qs.get("code") or [""])[0]
    if not code:
        raise RuntimeError(f"No code in consent redirect Location: {location!r}")
    return code


# ── Token exchange ────────────────────────────────────────────────────────────

@dataclass
class TokenResult:
    access_token: str
    refresh_token: str
    scope: str
    expires_in: int
    session_ends: str
    client_id: str = ""


def obtain_token(
    *,
    portal_base: str,
    canonical_url: str,
    role: str,
    mobile: str = "",
    admin_username: str = "",
    admin_password: str = "",
    smsgw_base: str = "http://localhost:8025",
    client_id: str = "",
) -> TokenResult:
    """
    Full headless PKCE + DCR flow.

    Parameters
    ----------
    portal_base     Portal root URL, e.g. https://sewasetu-hv.koreacentral.cloudapp.azure.com
    canonical_url   MCP server URL used as `resource` (audience), e.g. {portal_base}/citizen/mcp
    role            "citizen" or "officer"
    mobile          10-digit mobile (citizen only; must be in test range 9990000001-99)
    admin_username  Officer username
    admin_password  Officer password
    smsgw_base      SMS gateway base URL (GET /api/messages)
    client_id       Re-use an existing DCR client_id; if empty, a new one is registered.

    Returns TokenResult with access_token, refresh_token, scope, expires_in, session_ends.
    """
    redirect_uri = "http://localhost/callback"

    if not client_id:
        client_id = _dcr(portal_base)
        _info(f"DCR → client_id={client_id}")

    rid, verifier, state = _start_auth(portal_base, client_id, redirect_uri, role, canonical_url)
    _info(f"rid={rid}")

    if role == "citizen":
        if not mobile:
            raise ValueError("mobile is required for role='citizen'")
        _login_citizen(portal_base, rid, mobile, smsgw_base)
    elif role == "officer":
        if not admin_username or not admin_password:
            raise ValueError("admin_username and admin_password are required for role='officer'")
        _login_officer(portal_base, rid, admin_username, admin_password)
    else:
        raise ValueError(f"Unknown role: {role!r}")

    code = _consent_and_get_code(portal_base, rid)
    _info(f"code={code[:12]}…")

    token_resp = _post_form(
        f"{portal_base}/oauth/token",
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "resource": canonical_url,
        },
    )
    if "error" in token_resp:
        raise RuntimeError(f"Token exchange failed: {token_resp}")

    return TokenResult(
        access_token=token_resp["access_token"],
        refresh_token=token_resp.get("refresh_token", ""),
        scope=token_resp.get("scope", role),
        expires_in=token_resp.get("expires_in", 0),
        session_ends=token_resp.get("session_ends", ""),
        client_id=client_id,
    )


# ── MCP tool call ─────────────────────────────────────────────────────────────

def _parse_sse_or_json(text: str) -> Any:
    """Extract JSON-RPC payload from SSE data: lines or a plain JSON body."""
    text = text.strip()
    if text.startswith("data:"):
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    return json.loads(payload)
    return json.loads(text)


def call_tool(
    mcp_url: str,
    access_token: str,
    tool_name: str,
    arguments: dict | None = None,
    *,
    timeout: int = 15,
) -> Any:
    """
    Call an MCP tool via the Streamable HTTP transport (2026-07-28 spec).

    Sends `initialize` followed by `tools/call` as separate stateless POSTs.
    Returns the full JSON-RPC response dict for the tools/call.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2026-07-28",
    }

    # initialize
    init_payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2026-07-28", "capabilities": {},
            "clientInfo": {"name": "oauth_smoke", "version": "1.0"},
        },
    }).encode()
    req = urllib.request.Request(mcp_url, data=init_payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            _parse_sse_or_json(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"initialize failed HTTP {e.code}: {body[:400]}")

    # tools/call
    call_payload = json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments or {}},
    }).encode()
    req2 = urllib.request.Request(mcp_url, data=call_payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req2, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"tools/call failed HTTP {e.code}: {body[:400]}")

    return _parse_sse_or_json(raw)


# ── Unauthenticated 401 check ─────────────────────────────────────────────────

def check_401(mcp_url: str, expected_scope: str) -> None:
    """Assert an unauthenticated POST to `mcp_url` returns 401 with correct scope."""
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2026-07-28",
    }
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2026-07-28", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}},
    }).encode()
    req = urllib.request.Request(mcp_url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raise AssertionError(f"Expected 401 but got HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise AssertionError(f"Expected HTTP 401 but got {e.code}")
        www_auth = e.headers.get("WWW-Authenticate", "")
        if expected_scope and f'scope="{expected_scope}"' not in www_auth:
            raise AssertionError(
                f'WWW-Authenticate missing scope="{expected_scope}": {www_auth!r}'
            )


# ── RFC 9728 metadata check ───────────────────────────────────────────────────

def check_protected_resource_metadata(
    host: str, path_suffix: str, expected_scope: str
) -> dict:
    """GET /.well-known/oauth-protected-resource/<path_suffix> and validate fields."""
    url = f"{host}/.well-known/oauth-protected-resource/{path_suffix}"
    meta = _get(url)
    assert "resource" in meta, f"'resource' missing from metadata: {meta}"
    assert "authorization_servers" in meta, f"'authorization_servers' missing: {meta}"
    scopes = meta.get("scopes_supported", [])
    assert expected_scope in scopes, \
        f"scope {expected_scope!r} not in scopes_supported {scopes}"
    return meta


# ── Main smoke orchestration ──────────────────────────────────────────────────

@dataclass
class SmokeConfig:
    host: str = ""
    mobile: str = "9990000001"
    admin_username: str = ""
    admin_password: str = ""
    smsgw_base: str = "http://localhost:8025"
    role: str = "both"          # "citizen" | "officer" | "both"


def smoke_all(cfg: SmokeConfig) -> bool:
    """
    Run the full OAuth smoke suite.
    Returns True if all checks pass, False if any fail.
    Importable by the eval harness; no sys.exit() calls.
    """
    host = cfg.host.rstrip("/")
    citizen_mcp = f"{host}/citizen/mcp"
    officer_mcp = f"{host}/officer/mcp"
    all_ok = True

    def check(label: str, fn, *args, **kwargs):
        nonlocal all_ok
        try:
            result = fn(*args, **kwargs)
            _ok(label)
            return result
        except Exception as exc:
            _fail(f"{label}\n      ↳ {exc}")
            all_ok = False
            return None

    print(f"\n{'─'*62}")
    print(f"  Sewa Setu OAuth smoke  →  {host}")
    print(f"{'─'*62}\n")

    # 1. Discovery
    print("1. Discovery")
    as_meta = check(
        "AS metadata issuer == host",
        lambda: _validate_issuer(_get(f"{host}/.well-known/oauth-authorization-server"), host),
    )
    check(
        "JWKS has ≥1 key",
        lambda: _validate_jwks(_get(f"{host}/.well-known/jwks.json")),
    )

    # 2. RFC 9728 metadata
    print("\n2. Protected-resource metadata (RFC 9728)")
    if cfg.role in ("citizen", "both"):
        check("citizen/mcp metadata has scope=citizen",
              check_protected_resource_metadata, host, "citizen/mcp", "citizen")
    if cfg.role in ("officer", "both"):
        check("officer/mcp metadata has scope=officer",
              check_protected_resource_metadata, host, "officer/mcp", "officer")

    # 3. Unauthenticated → 401
    print("\n3. Unauthenticated requests → 401 with scope")
    if cfg.role in ("citizen", "both"):
        check('citizen /mcp → 401 with scope="citizen"',
              check_401, citizen_mcp, "citizen")
    if cfg.role in ("officer", "both"):
        check('officer /mcp → 401 with scope="officer"',
              check_401, officer_mcp, "officer")

    # 4. Citizen full flow
    citizen_tok: TokenResult | None = None
    if cfg.role in ("citizen", "both"):
        print(f"\n4. Citizen OAuth flow  (mobile={cfg.mobile})")
        citizen_tok = check(
            "DCR → PKCE → OTP → consent → token",
            obtain_token,
            portal_base=host,
            canonical_url=citizen_mcp,
            role="citizen",
            mobile=cfg.mobile,
            smsgw_base=cfg.smsgw_base,
        )
        if citizen_tok:
            _info(f"expires_in={citizen_tok.expires_in}s  "
                  f"session_ends={citizen_tok.session_ends}")
            result = check(
                "whoami tool echoes citizen subject",
                call_tool, citizen_mcp, citizen_tok.access_token, "whoami",
            )
            if result:
                text = _extract_text(result)
                _info(f"whoami → {text!r}")
                check(
                    f"whoami response contains mobile {cfg.mobile!r}",
                    lambda t=text: None if cfg.mobile in t
                                   else (_ for _ in ()).throw(
                                       AssertionError(f"{cfg.mobile!r} not in {t!r}")
                                   ),
                )

    # 5. Officer full flow
    officer_tok: TokenResult | None = None
    if cfg.role in ("officer", "both"):
        print(f"\n5. Officer OAuth flow  (user={cfg.admin_username})")
        officer_tok = check(
            "DCR → PKCE → password → consent → token",
            obtain_token,
            portal_base=host,
            canonical_url=officer_mcp,
            role="officer",
            admin_username=cfg.admin_username,
            admin_password=cfg.admin_password,
        )
        if officer_tok:
            _info(f"expires_in={officer_tok.expires_in}s  "
                  f"session_ends={officer_tok.session_ends}")
            result = check(
                "whoami tool echoes officer subject",
                call_tool, officer_mcp, officer_tok.access_token, "whoami",
            )
            if result:
                text = _extract_text(result)
                _info(f"whoami → {text!r}")
                check(
                    f"whoami response contains username {cfg.admin_username!r}",
                    lambda t=text: None if cfg.admin_username in t
                                   else (_ for _ in ()).throw(
                                       AssertionError(
                                           f"{cfg.admin_username!r} not in {t!r}"
                                       )
                                   ),
                )

    # 6. Refresh token present
    print("\n6. Refresh tokens")
    for label, tok in [("citizen", citizen_tok), ("officer", officer_tok)]:
        if tok is None:
            continue
        if cfg.role not in (label, "both"):
            continue
        if tok.refresh_token:
            _ok(f"{label} refresh_token returned (rotation tested in portal suite)")
        else:
            _fail(f"{label} refresh_token missing from token response")
            all_ok = False

    print(f"\n{'─'*62}")
    if all_ok:
        print(f"  {_GRN}ALL CHECKS PASSED{_RST}")
    else:
        print(f"  {_RED}SOME CHECKS FAILED — see ✗ lines above{_RST}")
    print(f"{'─'*62}\n")
    return all_ok


def _validate_issuer(meta: dict, host: str) -> dict:
    iss = meta.get("issuer", "")
    assert iss == host, f"issuer={iss!r} != {host!r}"
    return meta


def _validate_jwks(data: dict) -> dict:
    keys = data.get("keys", [])
    assert len(keys) >= 1, f"JWKS has no keys: {data}"
    return data


def _extract_text(rpc_response: Any) -> str:
    """Extract human-readable text from a JSON-RPC tools/call response."""
    try:
        content = rpc_response.get("result", {}).get("content", [])
        if content:
            return content[0].get("text", str(rpc_response))
    except Exception:
        pass
    return str(rpc_response)


# ── CLI entry-point ───────────────────────────────────────────────────────────

def _parse_args() -> SmokeConfig:
    p = argparse.ArgumentParser(
        description="Headless OAuth 2.1 smoke test for Sewa Setu MCP servers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Returns exit")[0].strip(),
    )
    p.add_argument(
        "--host",
        default=os.environ.get(
            "HOST", "https://sewasetu-hv.koreacentral.cloudapp.azure.com"
        ),
        help="Portal base URL  (default: $HOST or the live Azure VM)",
    )
    p.add_argument(
        "--mobile",
        default=os.environ.get("SMOKE_MOBILE", "9990000001"),
        help="Test mobile number 9990000001-99  (default: 9990000001)",
    )
    p.add_argument(
        "--admin-user",
        default=os.environ.get("ADMIN_USERNAME", "admin"),
        dest="admin_username",
        help="Officer username  (default: $ADMIN_USERNAME)",
    )
    p.add_argument(
        "--admin-pass",
        default=os.environ.get("ADMIN_PASSWORD", ""),
        dest="admin_password",
        help="Officer password  (default: $ADMIN_PASSWORD)",
    )
    p.add_argument(
        "--smsgw",
        default=os.environ.get("SMSGW_URL", "http://localhost:8025"),
        dest="smsgw_base",
        help="SMS gateway URL  (default: $SMSGW_URL or http://localhost:8025)",
    )
    p.add_argument(
        "--role",
        choices=["citizen", "officer", "both"],
        default=os.environ.get("SMOKE_ROLE", "both"),
        help="Role(s) to exercise  (default: both)",
    )
    args = p.parse_args()
    return SmokeConfig(
        host=args.host,
        mobile=args.mobile,
        admin_username=args.admin_username,
        admin_password=args.admin_password,
        smsgw_base=args.smsgw_base,
        role=args.role,
    )


if __name__ == "__main__":
    cfg = _parse_args()
    ok = smoke_all(cfg)
    sys.exit(0 if ok else 1)
