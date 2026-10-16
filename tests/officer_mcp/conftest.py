# -*- coding: utf-8 -*-
"""Pytest fixtures for Officer MCP server tests."""

import os
import sys
import json
import time
import pytest
import psycopg2
import jwt
import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.middleware.wsgi import WSGIMiddleware

# Ensure app and root are on sys.path
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_APP_DIR = os.path.join(_REPO_ROOT, "app")
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from tests.portal.conftest import (
    test_db,
    clean_db,
    flask_app,
    client,
    _dsn_parts,
    TEST_DB_URL,
    db_admin_conn,
    mock_sms_gateway,
)
from mcp_common.client import InternalApiClient
import officer_mcp.server as officer_server
import citizen_mcp.server as citizen_server


@pytest.fixture(scope="session")
def server_keys():
    """RSA 2048-bit keypair and JWK dict for signing test tokens."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    jwk_dict = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(pub))
    jwk_dict.update({"kid": "test-kid-1", "alg": "RS256", "use": "sig"})
    return priv, pub, jwk_dict


def make_token(
    priv,
    sub="officer:admin",
    role="officer",
    aud="http://localhost:8000/officer/mcp",
    iss="http://localhost:8000",
    exp_offset=300,
    iat_offset=0,
    kid="test-kid-1",
    client_id="test-client",
):
    now = int(time.time())
    claims = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "role": role,
        "scope": role,
        "client_id": client_id,
        "iat": now + iat_offset,
        "exp": now + exp_offset,
        "kind": "access",
    }
    return jwt.encode(claims, priv, algorithm="RS256", headers={"kid": kid})


@pytest.fixture(autouse=True)
def setup_officer_mcp(flask_app, server_keys, monkeypatch, tmp_path):
    """Wire officer_mcp and citizen_mcp to the test database and mock JWKS."""
    priv, pub, jwk = server_keys
    canonical_officer = "http://localhost:8000/officer/mcp"
    canonical_citizen = "http://localhost:8000/citizen/mcp"

    upload_dir = str(tmp_path / "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    monkeypatch.setattr(officer_server, "PUBLIC_BASE_URL", "http://localhost:8000")
    monkeypatch.setattr(officer_server, "CANONICAL_URL", canonical_officer)
    monkeypatch.setattr(citizen_server, "PUBLIC_BASE_URL", "http://localhost:8000")
    monkeypatch.setattr(citizen_server, "CANONICAL_URL", canonical_citizen)
    monkeypatch.setenv("UPLOAD_DIR", upload_dir)

    import internal_api as iapi
    import app as portal
    monkeypatch.setattr(iapi, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(portal, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(iapi, "INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    parts = _dsn_parts(TEST_DB_URL)
    monkeypatch.setattr(portal, "get_db", lambda: psycopg2.connect(**parts))
    monkeypatch.setattr(iapi, "_get_db", lambda: psycopg2.connect(**parts))

    # In-memory ASGI transport wrapping WSGI Flask test app
    asgi_app = WSGIMiddleware(flask_app)
    asgi_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi_app),
        base_url="http://app:8000",
    )

    # Wire internal API clients
    test_internal_client = InternalApiClient(
        base_url="http://app:8000/internal/api/v1",
        internal_api_key="test-internal-key",
        client=asgi_client,
    )
    monkeypatch.setattr(officer_server, "client", test_internal_client)
    monkeypatch.setattr(citizen_server, "client", test_internal_client)

    # Set key in officer verifier cache
    key_obj = jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
    verifier_off = officer_server.app.token_verifier
    verifier_off._keys_by_kid = {"test-kid-1": key_obj}
    verifier_off._jwks_fetched_at = time.time()
    verifier_off._custom_client = asgi_client
    verifier_off.internal_api_key = "test-internal-key"

    if hasattr(officer_server.app.mcp_server, "token_verifier"):
        mv = officer_server.app.mcp_server.token_verifier
        mv._keys_by_kid = {"test-kid-1": key_obj}
        mv._jwks_fetched_at = time.time()
        mv._custom_client = asgi_client
        mv.internal_api_key = "test-internal-key"

    # Set key in citizen verifier cache too
    verifier_cit = citizen_server.app.token_verifier
    verifier_cit._keys_by_kid = {"test-kid-1": key_obj}
    verifier_cit._jwks_fetched_at = time.time()
    verifier_cit._custom_client = asgi_client
    verifier_cit.internal_api_key = "test-internal-key"

    if hasattr(citizen_server.app.mcp_server, "token_verifier"):
        mv2 = citizen_server.app.mcp_server.token_verifier
        mv2._keys_by_kid = {"test-kid-1": key_obj}
        mv2._jwks_fetched_at = time.time()
        mv2._custom_client = asgi_client
        mv2.internal_api_key = "test-internal-key"

    yield

    # Cleanup marker mobile test rows (9990000001 - 9990000099)
    conn = psycopg2.connect(**parts)
    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM assistant_submissions WHERE mobile >= '9990000001' AND mobile <= '9990000099';
            DELETE FROM audit_log WHERE application_id IN (
                SELECT id FROM applications WHERE mobile >= '9990000001' AND mobile <= '9990000099'
            );
            DELETE FROM applications WHERE mobile >= '9990000001' AND mobile <= '9990000099';
            DELETE FROM portal_users WHERE mobile >= '9990000001' AND mobile <= '9990000099';
            DELETE FROM assistant_drafts WHERE mobile >= '9990000001' AND mobile <= '9990000099';
            DELETE FROM assistant_upload_tokens WHERE mobile >= '9990000001' AND mobile <= '9990000099';
            DELETE FROM assistant_signouts WHERE subject LIKE 'citizen:99900000%';
        """)
        conn.commit()
    conn.close()
