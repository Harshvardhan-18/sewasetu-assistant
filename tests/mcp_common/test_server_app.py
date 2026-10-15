"""Integration tests for MCP app factory (create_server) and middlewares (Section 2).

Tests:
- /healthz route works without authentication
- RFC 9728 protected-resource metadata route returns correct metadata
- Unauthenticated POST returns 401 with WWW-Authenticate containing resource_metadata and scope="<role>"
- Authenticated tool call works with valid token
- Response includes X-Request-ID
- Server instructions and custom role configurations
"""

import json
import logging
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from mcp_common.jwt_verifier import PortalTokenVerifier, current_subject
from mcp_common.server import create_server


@pytest.fixture(scope="module")
def server_keys():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    jwk_dict = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(pub))
    jwk_dict.update({"kid": "srv-kid-1", "alg": "RS256", "use": "sig"})
    return priv, pub, jwk_dict


def make_test_token(priv, sub="citizen:9876543210", role="citizen", aud="https://portal.example.com/citizen/mcp"):
    import time
    now = int(time.time())
    claims = {
        "iss": "https://portal.example.com",
        "aud": aud,
        "sub": sub,
        "role": role,
        "scope": role,
        "client_id": "test-client",
        "iat": now,
        "exp": now + 300,
        "kind": "access",
    }
    return jwt.encode(claims, priv, algorithm="RS256", headers={"kid": "srv-kid-1"})


def build_app(server_keys, role="citizen", path="/citizen/mcp"):
    priv, pub, jwk = server_keys
    canonical = f"https://portal.example.com{path}"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/.well-known/jwks.json"):
            return httpx.Response(200, json={"keys": [jwk]})
        if "/me/session-state" in str(request.url):
            return httpx.Response(200, json={"ok": True, "data": {"revoked_at": None}})
        return httpx.Response(404)

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    verifier = PortalTokenVerifier(
        public_base_url="https://portal.example.com",
        canonical_url=canonical,
        role=role,
        jwks_client=mock_client,
    )

    app = create_server(
        name=f"test-{role}-server",
        canonical_url=canonical,
        role=role,
        instructions=f"Instructions for {role} assistant.",
        public_base_url="https://portal.example.com",
        token_verifier=verifier,
    )

    # Register throwaway "whoami" / "echo whoami" tool
    @app.tool(name="whoami", description="Echo the authenticated caller subject")
    def whoami() -> str:
        sub = current_subject()
        return f"Hello, {sub}"

    return app, verifier, priv


# ── Tests ────────────────────────────────────────────────────────────────────

def test_healthz_unauthenticated(server_keys):
    app, verifier, priv = build_app(server_keys, role="citizen", path="/citizen/mcp")
    with TestClient(app) as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["role"] == "citizen"
        assert data["server"] == "test-citizen-server"


def test_protected_resource_metadata_route(server_keys):
    app, verifier, priv = build_app(server_keys, role="officer", path="/officer/mcp")
    with TestClient(app) as client:
        # RFC 9728 endpoint for /officer/mcp
        resp = client.get("/.well-known/oauth-protected-resource/officer/mcp")
        assert resp.status_code == 200
        data = resp.json()
        assert data["resource"] == "https://portal.example.com/officer/mcp"
        assert data["authorization_servers"] == ["https://portal.example.com"]
        assert data["scopes_supported"] == ["officer"]


def test_unauthenticated_post_returns_401_with_scope_and_metadata(server_keys):
    app, verifier, priv = build_app(server_keys, role="officer", path="/officer/mcp")
    with TestClient(app) as client:
        resp = client.post("/officer/mcp")
        assert resp.status_code == 401
        www_auth = resp.headers.get("www-authenticate", "")
        # Must contain resource_metadata
        assert 'resource_metadata="https://portal.example.com/.well-known/oauth-protected-resource/officer/mcp"' in www_auth
        # Must contain scope="officer" so client knows to request officer scope
        assert 'scope="officer"' in www_auth
        # Response carries X-Request-ID
        assert "x-request-id" in resp.headers


def test_unauthenticated_citizen_post_returns_401_with_citizen_scope(server_keys):
    app, verifier, priv = build_app(server_keys, role="citizen", path="/citizen/mcp")
    with TestClient(app) as client:
        resp = client.post("/citizen/mcp")
        assert resp.status_code == 401
        www_auth = resp.headers.get("www-authenticate", "")
        assert 'resource_metadata="https://portal.example.com/.well-known/oauth-protected-resource/citizen/mcp"' in www_auth
        assert 'scope="citizen"' in www_auth


def parse_sse_message(content_bytes: bytes) -> dict:
    for line in content_bytes.decode("utf-8", errors="ignore").splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return {}


def test_authenticated_whoami_tool_works(server_keys):
    app, verifier, priv = build_app(server_keys, role="citizen", path="/citizen/mcp")
    token = make_test_token(priv, sub="citizen:9876543210", role="citizen", aud="https://portal.example.com/citizen/mcp")

    with TestClient(app) as client:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }
        # 1. Initialize handshake
        init_resp = client.post(
            "/citizen/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0"},
                },
            },
        )
        assert init_resp.status_code == 200
        init_data = parse_sse_message(init_resp.content)
        assert init_data.get("result", {}).get("instructions") == "Instructions for citizen assistant."

        # 2. Tool call
        call_resp = client.post(
            "/citizen/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "whoami",
                    "arguments": {},
                },
            },
        )
        assert call_resp.status_code == 200
        call_data = parse_sse_message(call_resp.content)
        assert "result" in call_data
        content = call_data["result"].get("content", [])
        assert len(content) > 0
        assert "citizen:9876543210" in content[0]["text"]


def test_invalid_token_returns_401(server_keys):
    app, verifier, priv = build_app(server_keys, role="citizen", path="/citizen/mcp")
    with TestClient(app) as client:
        headers = {
            "Authorization": "Bearer invalid-garbage-token",
            "MCP-Protocol-Version": "2026-07-28",
        }
        resp = client.post(
            "/citizen/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}},
            },
        )
        assert resp.status_code == 401
        assert 'scope="citizen"' in resp.headers.get("www-authenticate", "")
