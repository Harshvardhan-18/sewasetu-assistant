#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/oauth_client.py — Production OAuth 2.1 client library and MCP caller.

Provides full RFC-compliant OAuth 2.1 protocol support:
  - Discovery from unauthenticated 401 (WWW-Authenticate, RFC 9728 resource metadata, RFC 8414 AS metadata)
  - Dynamic Client Registration (RFC 7591)
  - Client ID Metadata Document clients (RFC 7591 / OAuth draft specification)
  - PKCE S256 code challenge and verifier generation (RFC 7636)
  - Explicit resource (RFC 8707) and scope parameter binding
  - Watermarked OTP polling from the SMS gateway (count + timestamp watermark)
  - Explicit user consent step (allow vs deny)
  - Authorization code exchange for access & refresh tokens
  - Refresh token rotation (with session_ends invariant)
  - Token revocation (RFC 7009)
  - MCP tool caller for both modern (2026-07-28) and legacy (2025-11-25) transports
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional


# ── Protocol constants ────────────────────────────────────────────────────────

MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"

MODERN_META: dict[str, Any] = {
    "io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {
        "name": "sewasetu-oauth-client",
        "version": "1.0",
    },
}


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class TokenResult:
    access_token: str
    refresh_token: str = ""
    scope: str = ""
    expires_in: int = 0
    session_ends: str = ""
    client_id: str = ""
    token_type: str = "Bearer"


@dataclass
class OAuthDiscovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    revocation_endpoint: str
    jwks_uri: str
    scopes_supported: list[str] = field(default_factory=list)
    resource: str = ""
    resource_scopes: list[str] = field(default_factory=list)
    raw_as_metadata: dict[str, Any] = field(default_factory=dict)
    raw_resource_metadata: dict[str, Any] = field(default_factory=dict)


# ── Low-level HTTP utilities ──────────────────────────────────────────────────

def http_get(url: str, *, headers: Optional[dict[str, str]] = None, timeout: int = 10) -> tuple[int, dict[str, Any], dict[str, str]]:
    """GET url -> (status, json_body, response_headers)."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
            body = json.loads(data) if data.strip() else {}
            headers_dict = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, body, headers_dict
    except urllib.error.HTTPError as e:
        data = e.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(data)
        except Exception:
            body = {"raw": data}
        headers_dict = {k.lower(): v for k, v in e.headers.items()}
        return e.code, body, headers_dict


def http_post_form(url: str, data: dict[str, Any], *, headers: Optional[dict[str, str]] = None, timeout: int = 10) -> tuple[int, dict[str, Any], dict[str, str]]:
    """POST application/x-www-form-urlencoded -> (status, json_body, response_headers)."""
    body_bytes = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body_bytes, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data_str = resp.read().decode("utf-8")
            body = json.loads(data_str) if data_str.strip() else {}
            return resp.status, body, {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        data_str = e.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(data_str)
        except Exception:
            body = {"raw": data_str}
        return e.code, body, {k.lower(): v for k, v in e.headers.items()}


def http_post_json(url: str, payload: dict[str, Any], *, headers: Optional[dict[str, str]] = None, timeout: int = 10) -> tuple[int, dict[str, Any], dict[str, str]]:
    """POST application/json -> (status, json_body, response_headers)."""
    body_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body_bytes, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data_str = resp.read().decode("utf-8")
            body = json.loads(data_str) if data_str.strip() else {}
            return resp.status, body, {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        data_str = e.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(data_str)
        except Exception:
            body = {"raw": data_str}
        return e.code, body, {k.lower(): v for k, v in e.headers.items()}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Halts on 3xx redirects to expose Location header via HTTPError."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code, msg, headers, fp)


def build_no_redirect_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        NoRedirect(),
    )


# ── PKCE Helpers ──────────────────────────────────────────────────────────────

def pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge_S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ── SMS Gateway Watermarked OTP ───────────────────────────────────────────────

def count_gateway_messages(smsgw_base: str) -> int:
    """Return message count from gateway as high-water mark."""
    try:
        code, data, _ = http_get(f"{smsgw_base.rstrip('/')}/api/messages", timeout=5)
        if isinstance(data, list):
            return len(data)
        return len(data.get("messages", []))
    except Exception:
        return 0


def fetch_gateway_otp(
    smsgw_base: str,
    mobile: str,
    *,
    watermark_count: int = 0,
    watermark_time: float = 0.0,
    timeout: int = 25,
) -> str:
    """
    Poll GET {smsgw_base}/api/messages until a NEW OTP for `mobile` arrives.
    Matches messages newer than watermark_count or watermark_time - 5s.
    """
    deadline = time.monotonic() + timeout
    clean_smsgw = smsgw_base.rstrip("/")
    while time.monotonic() < deadline:
        try:
            code, data, _ = http_get(f"{clean_smsgw}/api/messages", timeout=5)
            items = data if isinstance(data, list) else data.get("messages", [])
            total = len(items)
            for idx, msg in enumerate(reversed(items)):
                to = str(msg.get("to", "") or msg.get("recipient", ""))
                if mobile not in to:
                    continue

                pos_from_end = total - idx
                is_new_by_count = pos_from_end > watermark_count
                is_new_by_time = False

                ts_str = msg.get("created_at") or msg.get("timestamp") or ""
                if ts_str:
                    try:
                        ts_epoch = datetime.datetime.fromisoformat(
                            str(ts_str).replace("Z", "+00:00")
                        ).timestamp()
                        is_new_by_time = ts_epoch >= watermark_time - 5.0
                    except Exception:
                        is_new_by_time = True

                if not (is_new_by_count or is_new_by_time):
                    continue

                body = str(msg.get("body", "") or msg.get("text", "") or msg.get("message", ""))
                m = re.search(r"\b(\d{6})\b", body)
                if m:
                    return m.group(1)
        except Exception:
            pass
        time.sleep(1)
    raise TimeoutError(f"OTP for {mobile} not received within {timeout}s from {clean_smsgw}")


# ── MCP Streamable HTTP response parsing ──────────────────────────────────────

def parse_sse_or_json(text: str, expected_id: Optional[int | str] = None) -> Any:
    """Parse SSE event stream or plain JSON response from an MCP server."""
    stripped = text.strip()
    if not stripped:
        raise ValueError("Empty MCP response body")

    # Fast direct JSON
    if (stripped.startswith("{") and stripped.endswith("}")) or (stripped.startswith("[") and stripped.endswith("]")):
        try:
            data = json.loads(stripped)
            if expected_id is None or data.get("id") == expected_id or str(data.get("id")) == str(expected_id):
                return data
        except json.JSONDecodeError:
            pass

    # SSE parser
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    candidates: list[dict[str, Any]] = []

    blocks = [b.strip() for b in normalized.split("\n\n") if b.strip()]
    for block in blocks:
        parts: list[str] = []
        for line in block.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                parts.append(line[5:].lstrip(" "))
        if parts:
            comb = "\n".join(parts).strip()
            if comb:
                try:
                    p = json.loads(comb)
                    if isinstance(p, dict):
                        candidates.append(p)
                except json.JSONDecodeError:
                    pass

    if not candidates:
        for line in normalized.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].lstrip(" ")
                if payload:
                    try:
                        p = json.loads(payload)
                        if isinstance(p, dict):
                            candidates.append(p)
                    except json.JSONDecodeError:
                        pass

    if expected_id is not None:
        for c in candidates:
            if c.get("id") == expected_id or str(c.get("id")) == str(expected_id):
                return c

    if candidates:
        for c in reversed(candidates):
            if "result" in c or "error" in c:
                return c
        return candidates[-1]

    return json.loads(stripped)


# ── OAuth Client Class ────────────────────────────────────────────────────────

class OAuthClient:
    """
    Full-featured OAuth 2.1 client supporting PKCE, DCR, Client ID Metadata Documents,
    explicit resource/scope binding, refresh rotation, and revocation.
    """

    def __init__(
        self,
        portal_base: str,
        canonical_url: str = "",
        *,
        smsgw_base: str = "http://localhost:8025",
        client_id: str = "",
        client_name: str = "sewasetu-agent",
        redirect_uri: str = "http://localhost/callback",
    ) -> None:
        self.portal_base = portal_base.rstrip("/")
        self.canonical_url = canonical_url.rstrip("/") if canonical_url else ""
        self.smsgw_base = smsgw_base.rstrip("/")
        self.client_id = client_id
        self.client_name = client_name
        self.redirect_uri = redirect_uri
        self._discovery: Optional[OAuthDiscovery] = None

    # ── Discovery ─────────────────────────────────────────────────────────────

    def discover_from_401(self, mcp_url: Optional[str] = None) -> OAuthDiscovery:
        """
        Probe mcp_url unauthenticated, extract scope and auth server from 401 WWW-Authenticate,
        and fetch RFC 8414 AS metadata + RFC 9728 protected resource metadata.
        """
        target = (mcp_url or self.canonical_url).rstrip("/")
        if not target:
            raise ValueError("mcp_url must be specified for discovery")

        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
            "Mcp-Method": "tools/call",
            "Mcp-Name": "whoami",
        }
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"_meta": MODERN_META, "name": "whoami", "arguments": {}},
        }

        code, body, resp_headers = http_post_json(target, payload, headers=headers)
        if code != 401:
            raise RuntimeError(f"Expected HTTP 401 from {target}, got {code}")

        www_auth = resp_headers.get("www-authenticate", "")
        # Parse scope="..."
        scope_match = re.search(r'scope="([^"]+)"', www_auth)
        discovered_scope = scope_match.group(1) if scope_match else ""

        # Protected resource metadata (RFC 9728)
        # Suffix is path after portal root, e.g. citizen/mcp
        path_suffix = urllib.parse.urlparse(target).path.lstrip("/")
        res_meta_url = f"{self.portal_base}/.well-known/oauth-protected-resource/{path_suffix}"
        res_code, res_meta, _ = http_get(res_meta_url)

        # Authorization server metadata (RFC 8414)
        as_meta_url = f"{self.portal_base}/.well-known/oauth-authorization-server"
        as_code, as_meta, _ = http_get(as_meta_url)
        if as_code != 200:
            raise RuntimeError(f"Failed to fetch AS metadata from {as_meta_url}: HTTP {as_code}")

        self._discovery = OAuthDiscovery(
            issuer=as_meta.get("issuer", self.portal_base),
            authorization_endpoint=as_meta.get("authorization_endpoint", f"{self.portal_base}/oauth/authorize"),
            token_endpoint=as_meta.get("token_endpoint", f"{self.portal_base}/oauth/token"),
            registration_endpoint=as_meta.get("registration_endpoint", f"{self.portal_base}/oauth/register"),
            revocation_endpoint=as_meta.get("revocation_endpoint", f"{self.portal_base}/oauth/revoke"),
            jwks_uri=as_meta.get("jwks_uri", f"{self.portal_base}/.well-known/jwks.json"),
            scopes_supported=as_meta.get("scopes_supported", []),
            resource=res_meta.get("resource", target) if res_code == 200 else target,
            resource_scopes=res_meta.get("scopes_supported", [discovered_scope]) if res_code == 200 else [discovered_scope],
            raw_as_metadata=as_meta,
            raw_resource_metadata=res_meta if res_code == 200 else {},
        )
        return self._discovery

    # ── Registration (DCR & CIMD) ─────────────────────────────────────────────

    def register_dcr(self, client_name: Optional[str] = None, redirect_uris: Optional[list[str]] = None) -> str:
        """Dynamic Client Registration (RFC 7591) -> client_id."""
        reg_url = f"{self.portal_base}/oauth/register"
        payload = {
            "client_name": client_name or self.client_name,
            "redirect_uris": redirect_uris or [self.redirect_uri],
            "token_endpoint_auth_method": "none",
        }
        code, body, _ = http_post_json(reg_url, payload)
        if code not in (200, 201) or "client_id" not in body:
            raise RuntimeError(f"DCR registration failed (HTTP {code}): {body}")
        self.client_id = body["client_id"]
        return self.client_id

    def use_client_metadata_document(self, metadata_url: Optional[str] = None) -> str:
        """
        Configure this client to identify via a Client ID Metadata Document (CIMD).
        The client_id is set to the document's URL.
        """
        doc_url = metadata_url or f"{self.portal_base}/oauth/client-metadata.json"
        self.client_id = doc_url
        return self.client_id

    # ── Authorization Flow ───────────────────────────────────────────────────

    def start_authorization(
        self,
        role: str,
        canonical_url: Optional[str] = None,
    ) -> tuple[str, str, str]:
        """
        Initiate PKCE authorization. Returns (rid, verifier, state).
        """
        if not self.client_id:
            self.register_dcr()

        target_resource = canonical_url or self.canonical_url
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(12)

        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": role,
            "resource": target_resource,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        auth_url = f"{self.portal_base}/oauth/authorize?" + urllib.parse.urlencode(params)
        opener = build_no_redirect_opener()
        location = ""
        try:
            opener.open(auth_url)
        except urllib.error.HTTPError as e:
            location = e.headers.get("Location", "")

        qs = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        rid = (qs.get("rid") or [""])[0]
        if not rid:
            raise RuntimeError(f"Failed to extract rid from /oauth/authorize redirect: Location={location!r}")
        return rid, verifier, state

    def submit_citizen_mobile(self, rid: str, mobile: str) -> tuple[int, float]:
        """Submit applicant mobile. Returns (watermark_count, watermark_time)."""
        wm_count = count_gateway_messages(self.smsgw_base)
        wm_time = time.time()

        login_url = f"{self.portal_base}/oauth/login?rid={rid}"
        body = urllib.parse.urlencode({"mobile": mobile}).encode("utf-8")
        req = urllib.request.Request(login_url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        try:
            with opener.open(req) as resp:
                pass
        except urllib.error.HTTPError:
            pass
        return wm_count, wm_time

    def submit_citizen_otp(self, rid: str, otp: str) -> None:
        """Submit the SMS OTP."""
        login_url = f"{self.portal_base}/oauth/login?rid={rid}"
        body = urllib.parse.urlencode({"otp": otp}).encode("utf-8")
        req = urllib.request.Request(login_url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        try:
            with opener.open(req) as resp:
                pass
        except urllib.error.HTTPError as e:
            if e.code not in (200, 302, 303):
                raise RuntimeError(f"OTP submission failed with HTTP {e.code}")

    def submit_officer_credentials(self, rid: str, username: str, password: str) -> None:
        """Submit officer username and password."""
        login_url = f"{self.portal_base}/oauth/login?rid={rid}"
        body = urllib.parse.urlencode({"username": username, "password": password}).encode("utf-8")
        req = urllib.request.Request(login_url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        try:
            with opener.open(req) as resp:
                pass
        except urllib.error.HTTPError as e:
            if e.code not in (200, 302, 303):
                raise RuntimeError(f"Officer credentials submission failed with HTTP {e.code}")

    def grant_consent(self, rid: str, decision: str = "allow") -> str:
        """
        Record consent decision ('allow' or 'deny').
        Returns authorization code if allow; raises error if deny or failure.
        """
        consent_url = f"{self.portal_base}/oauth/consent?rid={rid}"
        body = urllib.parse.urlencode({"decision": decision}).encode("utf-8")
        req = urllib.request.Request(consent_url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        opener = build_no_redirect_opener()
        location = ""
        try:
            opener.open(req)
        except urllib.error.HTTPError as e:
            location = e.headers.get("Location", "")

        if not location:
            raise RuntimeError(f"Consent POST ({decision}) did not produce a redirect")

        qs = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        if decision == "deny":
            err = (qs.get("error") or ["access_denied"])[0]
            desc = (qs.get("error_description") or ["User denied consent"])[0]
            raise PermissionError(f"Consent denied: error={err}, description={desc}")

        if "error" in qs:
            raise RuntimeError(f"Authorization error in consent response: {dict(qs)}")

        code = (qs.get("code") or [""])[0]
        if not code:
            raise RuntimeError(f"No authorization code in consent redirect: Location={location!r}")
        return code

    def exchange_code(
        self,
        code: str,
        verifier: str,
        canonical_url: Optional[str] = None,
    ) -> TokenResult:
        """Exchange authorization code with code_verifier for tokens."""
        target_resource = canonical_url or self.canonical_url
        token_url = f"{self.portal_base}/oauth/token"
        payload = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": verifier,
            "resource": target_resource,
        }
        status, body, _ = http_post_form(token_url, payload)
        if status != 200 or "access_token" not in body:
            raise RuntimeError(f"Token exchange failed (HTTP {status}): {body}")

        return TokenResult(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token", ""),
            scope=body.get("scope", ""),
            expires_in=body.get("expires_in", 0),
            session_ends=body.get("session_ends", ""),
            client_id=self.client_id,
            token_type=body.get("token_type", "Bearer"),
        )

    # ── High-level login methods ──────────────────────────────────────────────

    def obtain_citizen_token(
        self,
        mobile: str,
        canonical_url: Optional[str] = None,
    ) -> TokenResult:
        """Full end-to-end citizen authorization flow."""
        target_url = canonical_url or self.canonical_url or f"{self.portal_base}/citizen/mcp"
        rid, verifier, _ = self.start_authorization(role="citizen", canonical_url=target_url)
        wm_count, wm_time = self.submit_citizen_mobile(rid, mobile)
        otp = fetch_gateway_otp(self.smsgw_base, mobile, watermark_count=wm_count, watermark_time=wm_time)
        self.submit_citizen_otp(rid, otp)
        code = self.grant_consent(rid, decision="allow")
        return self.exchange_code(code, verifier, canonical_url=target_url)

    def obtain_officer_token(
        self,
        username: str,
        password: str,
        canonical_url: Optional[str] = None,
    ) -> TokenResult:
        """Full end-to-end officer authorization flow."""
        target_url = canonical_url or self.canonical_url or f"{self.portal_base}/officer/mcp"
        rid, verifier, _ = self.start_authorization(role="officer", canonical_url=target_url)
        self.submit_officer_credentials(rid, username, password)
        code = self.grant_consent(rid, decision="allow")
        return self.exchange_code(code, verifier, canonical_url=target_url)

    # ── Refresh & Revoke ──────────────────────────────────────────────────────

    def refresh_token(
        self,
        current_refresh_token: str,
        canonical_url: Optional[str] = None,
    ) -> TokenResult:
        """
        Refresh access token and rotate refresh token.
        Enforces RFC refresh token rotation: old refresh token is consumed.
        """
        target_resource = canonical_url or self.canonical_url
        token_url = f"{self.portal_base}/oauth/token"
        payload = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "refresh_token": current_refresh_token,
            "resource": target_resource,
        }
        status, body, _ = http_post_form(token_url, payload)
        if status != 200 or "access_token" not in body:
            raise RuntimeError(f"Refresh failed (HTTP {status}): {body}")

        new_refresh = body.get("refresh_token", "")
        if new_refresh == current_refresh_token:
            raise AssertionError("Refresh token was not rotated by authorization server")

        return TokenResult(
            access_token=body["access_token"],
            refresh_token=new_refresh,
            scope=body.get("scope", ""),
            expires_in=body.get("expires_in", 0),
            session_ends=body.get("session_ends", ""),
            client_id=self.client_id,
            token_type=body.get("token_type", "Bearer"),
        )

    def revoke(self, token: str) -> bool:
        """Revoke a token (RFC 7009)."""
        revoke_url = f"{self.portal_base}/oauth/revoke"
        status, body, _ = http_post_form(revoke_url, {"token": token, "client_id": self.client_id})
        return status == 200

    # ── MCP Tool Calling ──────────────────────────────────────────────────────

    def call_tool(
        self,
        access_token: str,
        tool_name: str,
        arguments: Optional[dict[str, Any]] = None,
        mcp_url: Optional[str] = None,
        timeout: int = 15,
    ) -> dict[str, Any]:
        """Call an MCP tool via the modern Streamable HTTP transport (2026-07-28)."""
        target = (mcp_url or self.canonical_url).rstrip("/")
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
            "Mcp-Method": "tools/call",
            "Mcp-Name": tool_name,
        }
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "_meta": MODERN_META,
                "name": tool_name,
                "arguments": arguments or {},
            },
        }

        call_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(target, data=call_bytes, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"tools/call {tool_name} failed HTTP {e.code}: {raw[:500]}")

        return parse_sse_or_json(raw, expected_id=1)

    def call_tool_legacy(
        self,
        access_token: str,
        tool_name: str,
        arguments: Optional[dict[str, Any]] = None,
        mcp_url: Optional[str] = None,
        timeout: int = 15,
    ) -> dict[str, Any]:
        """Call an MCP tool via the legacy stateless transport (2025-11-25)."""
        target = (mcp_url or self.canonical_url).rstrip("/")
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": LEGACY_PROTOCOL_VERSION,
        }

        # Step 1: initialize
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LEGACY_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "oauth_client_legacy", "version": "1.0"},
            },
        }
        req_init = urllib.request.Request(target, data=json.dumps(init_payload).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req_init, timeout=timeout) as resp:
            init_raw = resp.read().decode("utf-8")
        init_res = parse_sse_or_json(init_raw, expected_id=1)
        if isinstance(init_res, dict) and "error" in init_res:
            raise RuntimeError(f"Legacy initialize error: {init_res['error']}")

        # Step 2: tools/call
        call_payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        }
        req_call = urllib.request.Request(target, data=json.dumps(call_payload).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req_call, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        return parse_sse_or_json(raw, expected_id=2)


# ── Backwards-Compatible Helpers for Existing Scripts ─────────────────────────

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
    """Convenience function matching scripts/oauth_smoke.py signature."""
    client = OAuthClient(
        portal_base=portal_base,
        canonical_url=canonical_url,
        smsgw_base=smsgw_base,
        client_id=client_id,
    )
    if role == "citizen":
        return client.obtain_citizen_token(mobile)
    elif role == "officer":
        return client.obtain_officer_token(admin_username, admin_password)
    raise ValueError(f"Unknown role: {role!r}")


def call_tool(
    mcp_url: str,
    access_token: str,
    tool_name: str,
    arguments: Optional[dict[str, Any]] = None,
    timeout: int = 15,
) -> dict[str, Any]:
    """Convenience function matching scripts/oauth_smoke.py signature."""
    client = OAuthClient(portal_base="", canonical_url=mcp_url)
    return client.call_tool(access_token, tool_name, arguments=arguments, timeout=timeout)
