#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/oauth/test_oauth_deployed.py — OAuth 2.1 verification test suite against deployed server.

Validates all security invariants required by BUILD_GUIDE and Phase A:
  1. Audience accepted only by its own server.
  2. Citizen token at /officer/mcp and officer token at /citizen/mcp rejected.
  3. Root-audience token rejected at both MCP endpoints.
  4. Refresh rotates refresh_token; session_ends is not extended. Replay of old refresh fails.
  5. Revoke kills refresh token immediately.
  6. Expired / invalid token returns a followable 401 with WWW-Authenticate.
  7. Consent Deny mints nothing (no code, no token).
  8. Client ID Metadata Document client succeeds end-to-end without prior DCR.
  9. After end_my_session, old access token is immediately rejected and refresh token fails.
"""

from __future__ import annotations

import os
import pytest
import urllib.parse
from tools.oauth_client import (
    OAuthClient,
    TokenResult,
    http_get,
    http_post_form,
    http_post_json,
    MODERN_PROTOCOL_VERSION,
    MODERN_META,
)

# Target configuration from environment or defaults
DEPLOYED_HOST = os.environ.get("HOST", "https://sewasetu-hv.koreacentral.cloudapp.azure.com").rstrip("/")
SMSGW_URL = os.environ.get("SMSGW_URL", f"{DEPLOYED_HOST}/__gateway").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin123")
TEST_MOBILE = "9990000003"


@pytest.fixture(scope="module")
def client() -> OAuthClient:
    return OAuthClient(
        portal_base=DEPLOYED_HOST,
        smsgw_base=SMSGW_URL,
    )


def test_discovery_and_followable_401(client: OAuthClient):
    """Assert unauthenticated / invalid token requests return followable 401."""
    # 1. Unauthenticated request to /citizen/mcp
    disc_cit = client.discover_from_401(f"{DEPLOYED_HOST}/citizen/mcp")
    assert disc_cit.issuer == DEPLOYED_HOST
    assert "citizen" in disc_cit.resource_scopes
    assert disc_cit.authorization_endpoint.endswith("/oauth/authorize")
    assert disc_cit.token_endpoint.endswith("/oauth/token")

    # 2. Unauthenticated request to /officer/mcp
    disc_off = client.discover_from_401(f"{DEPLOYED_HOST}/officer/mcp")
    assert "officer" in disc_off.resource_scopes

    # 3. Request with bogus / expired token -> 401 with WWW-Authenticate
    bogus_headers = {
        "Authorization": "Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjEwMDAwMDAwMDB9.bogussig",
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
    code, _, resp_headers = http_post_json(f"{DEPLOYED_HOST}/citizen/mcp", payload, headers=bogus_headers)
    assert code == 401
    assert "www-authenticate" in resp_headers
    assert 'scope="citizen"' in resp_headers["www-authenticate"]


def test_audience_isolation_and_cross_server_rejection(client: OAuthClient):
    """
    Assert audience accepted only by its own server:
      - citizen token at /officer/mcp is rejected.
      - officer token at /citizen/mcp is rejected.
    """
    # 1. Obtain legitimate citizen token for citizen MCP
    cit_tok = client.obtain_citizen_token(TEST_MOBILE, canonical_url=f"{DEPLOYED_HOST}/citizen/mcp")
    assert cit_tok.access_token

    # Valid on citizen server
    res_cit = client.call_tool(cit_tok.access_token, "whoami", mcp_url=f"{DEPLOYED_HOST}/citizen/mcp")
    assert TEST_MOBILE in str(res_cit)

    # REJECTED on officer server (wrong audience and wrong scope)
    with pytest.raises(Exception) as exc_info:
        client.call_tool(cit_tok.access_token, "whoami", mcp_url=f"{DEPLOYED_HOST}/officer/mcp")
    assert "401" in str(exc_info.value) or "403" in str(exc_info.value)

    # 2. Obtain legitimate officer token for officer MCP
    off_tok = client.obtain_officer_token(ADMIN_USER, ADMIN_PASS, canonical_url=f"{DEPLOYED_HOST}/officer/mcp")
    assert off_tok.access_token

    # Valid on officer server
    res_off = client.call_tool(off_tok.access_token, "whoami", mcp_url=f"{DEPLOYED_HOST}/officer/mcp")
    assert ADMIN_USER in str(res_off)

    # REJECTED on citizen server (wrong audience and wrong scope)
    with pytest.raises(Exception) as exc_info:
        client.call_tool(off_tok.access_token, "whoami", mcp_url=f"{DEPLOYED_HOST}/citizen/mcp")
    assert "401" in str(exc_info.value) or "403" in str(exc_info.value)


def test_root_audience_token_rejected(client: OAuthClient):
    """
    Assert token minted with root audience (DEPLOYED_HOST) is rejected by both MCP servers.
    MCP servers must strictly require their exact canonical URL as audience.
    """
    # Authorize with resource = root host URL
    rid, verifier, _ = client.start_authorization(role="citizen", canonical_url=DEPLOYED_HOST)
    wm_c, wm_t = client.submit_citizen_mobile(rid, TEST_MOBILE)
    from tools.oauth_client import fetch_gateway_otp
    otp = fetch_gateway_otp(client.smsgw_base, TEST_MOBILE, watermark_count=wm_c, watermark_time=wm_t)
    client.submit_citizen_otp(rid, otp)
    code = client.grant_consent(rid, decision="allow")
    root_tok = client.exchange_code(code, verifier, canonical_url=DEPLOYED_HOST)

    # Present root-audience token to /citizen/mcp -> MUST BE REJECTED
    with pytest.raises(Exception) as exc_cit:
        client.call_tool(root_tok.access_token, "whoami", mcp_url=f"{DEPLOYED_HOST}/citizen/mcp")
    assert "401" in str(exc_cit.value)

    # Present root-audience token to /officer/mcp -> MUST BE REJECTED
    with pytest.raises(Exception) as exc_off:
        client.call_tool(root_tok.access_token, "whoami", mcp_url=f"{DEPLOYED_HOST}/officer/mcp")
    assert "401" in str(exc_off.value)


def test_refresh_rotates_and_session_window_constant(client: OAuthClient):
    """
    Assert:
      1. Refresh token exchange succeeds and rotates (new_rt != old_rt).
      2. session_ends is not extended.
      3. Replay of the old refresh token fails with invalid_grant.
    """
    tok = client.obtain_citizen_token(TEST_MOBILE, canonical_url=f"{DEPLOYED_HOST}/citizen/mcp")
    old_rt = tok.refresh_token
    orig_session_ends = tok.session_ends
    assert old_rt, "Initial grant did not return refresh_token"

    # Refresh
    refreshed = client.refresh_token(old_rt, canonical_url=f"{DEPLOYED_HOST}/citizen/mcp")
    new_rt = refreshed.refresh_token
    assert new_rt, "Refresh response did not return new refresh_token"
    assert new_rt != old_rt, "Refresh token was not rotated!"
    assert refreshed.session_ends == orig_session_ends, "Refresh extended session_ends window!"

    # Verify new access token works
    res = client.call_tool(refreshed.access_token, "whoami", mcp_url=f"{DEPLOYED_HOST}/citizen/mcp")
    assert TEST_MOBILE in str(res)

    # Replay of old refresh token MUST fail
    with pytest.raises(RuntimeError) as exc_info:
        client.refresh_token(old_rt, canonical_url=f"{DEPLOYED_HOST}/citizen/mcp")
    assert "invalid_grant" in str(exc_info.value)


def test_revocation_kills_refresh(client: OAuthClient):
    """Assert calling /oauth/revoke kills refresh token immediately."""
    tok = client.obtain_citizen_token(TEST_MOBILE, canonical_url=f"{DEPLOYED_HOST}/citizen/mcp")
    assert tok.refresh_token

    # Revoke
    ok = client.revoke(tok.refresh_token)
    assert ok, "Revocation request failed"

    # Attempt to refresh with the revoked token -> invalid_grant
    with pytest.raises(RuntimeError) as exc_info:
        client.refresh_token(tok.refresh_token, canonical_url=f"{DEPLOYED_HOST}/citizen/mcp")
    assert "invalid_grant" in str(exc_info.value)


def test_consent_deny_mints_nothing(client: OAuthClient):
    """Assert choosing 'deny' during consent produces error redirect and mints no code."""
    rid, verifier, _ = client.start_authorization(role="citizen", canonical_url=f"{DEPLOYED_HOST}/citizen/mcp")
    wm_c, wm_t = client.submit_citizen_mobile(rid, TEST_MOBILE)
    from tools.oauth_client import fetch_gateway_otp
    otp = fetch_gateway_otp(client.smsgw_base, TEST_MOBILE, watermark_count=wm_c, watermark_time=wm_t)
    client.submit_citizen_otp(rid, otp)

    # Submit deny
    with pytest.raises(PermissionError) as exc_info:
        client.grant_consent(rid, decision="deny")
    assert "access_denied" in str(exc_info.value)


def test_client_id_metadata_document_client(client: OAuthClient):
    """Assert client using Client ID Metadata Document URL succeeds without prior DCR."""
    cimd_client = OAuthClient(
        portal_base=DEPLOYED_HOST,
        canonical_url=f"{DEPLOYED_HOST}/citizen/mcp",
        smsgw_base=SMSGW_URL,
    )
    # Configure to use metadata document URL as client_id
    doc_url = cimd_client.use_client_metadata_document()
    assert doc_url.startswith("http")

    # Full authorization flow using CIMD client_id
    tok = cimd_client.obtain_citizen_token(TEST_MOBILE)
    assert tok.access_token
    assert tok.client_id == doc_url

    # Check whoami tool call with CIMD-minted token
    res = cimd_client.call_tool(tok.access_token, "whoami")
    assert TEST_MOBILE in str(res)


def test_end_my_session_invalidates_access_and_refresh_tokens(client: OAuthClient):
    """
    Assert after citizen calls end_my_session:
      - The old access token is rejected at once (HTTP 401 / revoked cut-off).
      - The refresh token fails immediately (revoked in DB).
    """
    tok = client.obtain_citizen_token(TEST_MOBILE, canonical_url=f"{DEPLOYED_HOST}/citizen/mcp")
    assert tok.access_token
    assert tok.refresh_token

    # Verify whoami works prior to session end
    res_before = client.call_tool(tok.access_token, "whoami", mcp_url=f"{DEPLOYED_HOST}/citizen/mcp")
    assert TEST_MOBILE in str(res_before)

    # Call end_my_session
    end_res = client.call_tool(tok.access_token, "end_my_session", mcp_url=f"{DEPLOYED_HOST}/citizen/mcp")
    assert "Session ended" in str(end_res) or "sign-in(s) revoked" in str(end_res)

    # 1. Old access token MUST be rejected immediately on subsequent calls
    with pytest.raises(RuntimeError) as exc_access:
        client.call_tool(tok.access_token, "whoami", mcp_url=f"{DEPLOYED_HOST}/citizen/mcp")
    assert "401" in str(exc_access.value)

    # 2. Refresh token MUST fail
    with pytest.raises(RuntimeError) as exc_refresh:
        client.refresh_token(tok.refresh_token, canonical_url=f"{DEPLOYED_HOST}/citizen/mcp")
    assert "invalid_grant" in str(exc_refresh.value)
