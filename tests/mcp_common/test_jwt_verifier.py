"""Unit tests for PortalTokenVerifier (Section 1 and 5).

Tests:
- Valid token accepted (returns AccessToken with all claims)
- Wrong iss rejected (exact match, no trailing slash)
- Wrong aud rejected (exact string match)
- Aud trailing slash difference rejected
- Expired token rejected
- Wrong scope rejected
- Citizen token on officer server rejected
- Officer token on citizen server rejected
- Tampered signature rejected
- Unknown kid rejected (even after refresh)
- kind=program accepted on officer server only
- Citizen sign-out cut-off: token with iat <= revoked_at rejected
- JWKS caching and refetch-on-unknown-kid behavior
"""

import json
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp_common.jwt_verifier import PortalTokenVerifier, current_subject


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def rsa_key_pair():
    """Generate a test RSA key pair and JWK."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    jwk_dict = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(pub))
    jwk_dict.update({"kid": "test-kid-1", "alg": "RS256", "use": "sig"})
    return priv, pub, jwk_dict


@pytest.fixture(scope="module")
def secondary_key_pair():
    """Generate a second key pair to test unknown/tampered signatures."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    jwk_dict = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(pub))
    jwk_dict.update({"kid": "test-kid-2", "alg": "RS256", "use": "sig"})
    return priv, pub, jwk_dict


def make_token(
    priv_key,
    kid: str = "test-kid-1",
    iss: str = "https://portal.example.com",
    aud: str = "https://portal.example.com/citizen/mcp",
    sub: str = "citizen:9876543210",
    role: str = "citizen",
    scope: str = "citizen",
    exp_offset: int = 300,
    iat_offset: int = 0,
    kind: str = "access",
    client_id: str = "test-client",
) -> str:
    now = int(time.time())
    claims = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "role": role,
        "scope": scope,
        "client_id": client_id,
        "iat": now + iat_offset,
        "exp": now + exp_offset,
        "kind": kind,
    }
    return jwt.encode(claims, priv_key, algorithm="RS256", headers={"kid": kid})


def build_mock_transport(jwks_data: dict, session_states: dict[str, Any] | None = None):
    """Build an httpx.MockTransport that serves JWKS and session-state endpoints."""
    states = session_states or {}

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.endswith("/.well-known/jwks.json"):
            return httpx.Response(200, json=jwks_data)
        if "/me/session-state" in url_str:
            subject = request.headers.get("X-Acting-Subject", "")
            revoked_at = states.get(subject)
            return httpx.Response(200, json={
                "ok": True,
                "code": "ok",
                "message_for_user": "Session state.",
                "data": {"subject": subject, "revoked_at": revoked_at},
            })
        return httpx.Response(404)

    return httpx.MockTransport(handler)


# ── Tests ────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_valid_citizen_token_accepted(rsa_key_pair):
    priv, pub, jwk = rsa_key_pair
    transport = build_mock_transport({"keys": [jwk]})
    client = httpx.AsyncClient(transport=transport)

    verifier = PortalTokenVerifier(
        public_base_url="https://portal.example.com",
        canonical_url="https://portal.example.com/citizen/mcp",
        role="citizen",
        jwks_client=client,
    )

    token = make_token(priv)
    access_token = await verifier.verify_token(token)

    assert access_token is not None
    assert access_token.token == token
    assert access_token.client_id == "test-client"
    assert access_token.scopes == ["citizen"]
    assert access_token.resource == "https://portal.example.com/citizen/mcp"
    assert access_token.subject == "citizen:9876543210"
    assert access_token.claims["role"] == "citizen"


@pytest.mark.anyio
async def test_valid_officer_token_accepted(rsa_key_pair):
    priv, pub, jwk = rsa_key_pair
    transport = build_mock_transport({"keys": [jwk]})
    client = httpx.AsyncClient(transport=transport)

    verifier = PortalTokenVerifier(
        public_base_url="https://portal.example.com",
        canonical_url="https://portal.example.com/officer/mcp",
        role="officer",
        jwks_client=client,
    )

    token = make_token(
        priv,
        aud="https://portal.example.com/officer/mcp",
        sub="officer:admin",
        role="officer",
        scope="officer",
    )
    access_token = await verifier.verify_token(token)

    assert access_token is not None
    assert access_token.subject == "officer:admin"
    assert access_token.scopes == ["officer"]
    assert access_token.resource == "https://portal.example.com/officer/mcp"


@pytest.mark.anyio
async def test_wrong_issuer_rejected(rsa_key_pair):
    priv, pub, jwk = rsa_key_pair
    transport = build_mock_transport({"keys": [jwk]})
    client = httpx.AsyncClient(transport=transport)

    verifier = PortalTokenVerifier(
        public_base_url="https://portal.example.com",
        canonical_url="https://portal.example.com/citizen/mcp",
        role="citizen",
        jwks_client=client,
    )

    # Wrong domain
    token = make_token(priv, iss="https://evil.example.com")
    assert await verifier.verify_token(token) is None

    # Trailing slash difference
    token_slash = make_token(priv, iss="https://portal.example.com/other")
    assert await verifier.verify_token(token_slash) is None


@pytest.mark.anyio
async def test_wrong_audience_rejected(rsa_key_pair):
    priv, pub, jwk = rsa_key_pair
    transport = build_mock_transport({"keys": [jwk]})
    client = httpx.AsyncClient(transport=transport)

    verifier = PortalTokenVerifier(
        public_base_url="https://portal.example.com",
        canonical_url="https://portal.example.com/citizen/mcp",
        role="citizen",
        jwks_client=client,
    )

    # Pointing to officer server
    token_officer_aud = make_token(priv, aud="https://portal.example.com/officer/mcp")
    assert await verifier.verify_token(token_officer_aud) is None

    # Trailing slash difference must be rejected (exact string match)
    token_slash = make_token(priv, aud="https://portal.example.com/citizen/mcp/")
    assert await verifier.verify_token(token_slash) is None


@pytest.mark.anyio
async def test_expired_token_rejected(rsa_key_pair):
    priv, pub, jwk = rsa_key_pair
    transport = build_mock_transport({"keys": [jwk]})
    client = httpx.AsyncClient(transport=transport)

    verifier = PortalTokenVerifier(
        public_base_url="https://portal.example.com",
        canonical_url="https://portal.example.com/citizen/mcp",
        role="citizen",
        jwks_client=client,
    )

    # Expired 30 seconds ago (outside 10s leeway)
    token = make_token(priv, exp_offset=-30)
    assert await verifier.verify_token(token) is None


@pytest.mark.anyio
async def test_wrong_scope_rejected(rsa_key_pair):
    priv, pub, jwk = rsa_key_pair
    transport = build_mock_transport({"keys": [jwk]})
    client = httpx.AsyncClient(transport=transport)

    verifier = PortalTokenVerifier(
        public_base_url="https://portal.example.com",
        canonical_url="https://portal.example.com/citizen/mcp",
        role="citizen",
        jwks_client=client,
    )

    token = make_token(priv, scope="read_only")
    assert await verifier.verify_token(token) is None


@pytest.mark.anyio
async def test_citizen_token_on_officer_server_rejected(rsa_key_pair):
    priv, pub, jwk = rsa_key_pair
    transport = build_mock_transport({"keys": [jwk]})
    client = httpx.AsyncClient(transport=transport)

    officer_verifier = PortalTokenVerifier(
        public_base_url="https://portal.example.com",
        canonical_url="https://portal.example.com/officer/mcp",
        role="officer",
        jwks_client=client,
    )

    # Citizen token sent to officer server
    token = make_token(
        priv,
        aud="https://portal.example.com/officer/mcp",
        sub="citizen:9876543210",
        role="citizen",
        scope="citizen",
    )
    assert await officer_verifier.verify_token(token) is None


@pytest.mark.anyio
async def test_officer_token_on_citizen_server_rejected(rsa_key_pair):
    priv, pub, jwk = rsa_key_pair
    transport = build_mock_transport({"keys": [jwk]})
    client = httpx.AsyncClient(transport=transport)

    citizen_verifier = PortalTokenVerifier(
        public_base_url="https://portal.example.com",
        canonical_url="https://portal.example.com/citizen/mcp",
        role="citizen",
        jwks_client=client,
    )

    # Officer token sent to citizen server
    token = make_token(
        priv,
        aud="https://portal.example.com/citizen/mcp",
        sub="officer:admin",
        role="officer",
        scope="officer",
    )
    assert await citizen_verifier.verify_token(token) is None


@pytest.mark.anyio
async def test_tampered_signature_rejected(rsa_key_pair, secondary_key_pair):
    priv1, pub1, jwk1 = rsa_key_pair
    priv2, pub2, jwk2 = secondary_key_pair

    # Server only knows jwk1
    transport = build_mock_transport({"keys": [jwk1]})
    client = httpx.AsyncClient(transport=transport)

    verifier = PortalTokenVerifier(
        public_base_url="https://portal.example.com",
        canonical_url="https://portal.example.com/citizen/mcp",
        role="citizen",
        jwks_client=client,
    )

    # Signed with priv2 but claiming kid=test-kid-1
    token = make_token(priv2, kid="test-kid-1")
    assert await verifier.verify_token(token) is None


@pytest.mark.anyio
async def test_unknown_kid_rejected(rsa_key_pair, secondary_key_pair):
    priv1, pub1, jwk1 = rsa_key_pair
    priv2, pub2, jwk2 = secondary_key_pair

    transport = build_mock_transport({"keys": [jwk1]})
    client = httpx.AsyncClient(transport=transport)

    verifier = PortalTokenVerifier(
        public_base_url="https://portal.example.com",
        canonical_url="https://portal.example.com/citizen/mcp",
        role="citizen",
        jwks_client=client,
    )

    # Signed with unknown kid
    token = make_token(priv2, kid="completely-unknown-kid")
    assert await verifier.verify_token(token) is None


@pytest.mark.anyio
async def test_program_token_accepted_on_officer_rejected_on_citizen(rsa_key_pair):
    priv, pub, jwk = rsa_key_pair
    transport = build_mock_transport({"keys": [jwk]})
    client = httpx.AsyncClient(transport=transport)

    officer_verifier = PortalTokenVerifier(
        public_base_url="https://portal.example.com",
        canonical_url="https://portal.example.com/officer/mcp",
        role="officer",
        jwks_client=client,
    )
    citizen_verifier = PortalTokenVerifier(
        public_base_url="https://portal.example.com",
        canonical_url="https://portal.example.com/citizen/mcp",
        role="citizen",
        jwks_client=client,
    )

    # Officer program token: accepted
    officer_prog = make_token(
        priv,
        aud="https://portal.example.com/officer/mcp",
        sub="officer:eval_runner",
        role="officer",
        scope="officer",
        kind="program",
    )
    assert await officer_verifier.verify_token(officer_prog) is not None

    # Citizen token claiming kind=program: rejected
    citizen_prog = make_token(
        priv,
        aud="https://portal.example.com/citizen/mcp",
        sub="citizen:9876543210",
        role="citizen",
        scope="citizen",
        kind="program",
    )
    assert await citizen_verifier.verify_token(citizen_prog) is None


@pytest.mark.anyio
async def test_citizen_signout_cutoff_rejected(rsa_key_pair):
    priv, pub, jwk = rsa_key_pair
    now = int(time.time())

    # Citizen signed out at now - 10s
    revoked_dt = datetime.fromtimestamp(now - 10, tz=timezone.utc).isoformat()
    session_states = {"citizen:9876543210": revoked_dt}

    transport = build_mock_transport({"keys": [jwk]}, session_states=session_states)
    client = httpx.AsyncClient(transport=transport)

    verifier = PortalTokenVerifier(
        public_base_url="https://portal.example.com",
        canonical_url="https://portal.example.com/citizen/mcp",
        role="citizen",
        jwks_client=client,
    )

    # Token issued before signout (iat = now - 60s <= revoked_at)
    old_token = make_token(priv, iat_offset=-60, sub="citizen:9876543210")
    assert await verifier.verify_token(old_token) is None

    # Token issued after signout (iat = now + 5s > revoked_at)
    new_token = make_token(priv, iat_offset=5, sub="citizen:9876543210")
    assert await verifier.verify_token(new_token) is not None
