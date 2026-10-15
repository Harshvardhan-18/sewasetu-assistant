"""Pytest configuration for portal integration tests.

Uses a real Postgres database (sewasetu_test) created fresh for the test session.
Requires the database server to be reachable — run with:

    docker compose up -d db
    pytest tests/portal/

or set TEST_DB_URL to override the connection string.
"""
import os
import sys
import re
import io
import psycopg2
import pytest

os.environ["TESTING"] = "1"
_APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "app"))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

# ── Connection settings ──────────────────────────────────────────────────────

_DEFAULT_ADMIN_URL = "postgresql://sewasetu:sewasetu123@127.0.0.1:5432/postgres"
_TEST_DB_NAME = "sewasetu_test"
_SCHEMA_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "app", "schema.sql"
)

ADMIN_URL = os.environ.get("TEST_ADMIN_DB_URL", _DEFAULT_ADMIN_URL)
TEST_DB_URL = os.environ.get(
    "TEST_DB_URL",
    "postgresql://sewasetu:sewasetu123@127.0.0.1:5432/" + _TEST_DB_NAME,
)


def _dsn_parts(url):
    """Parse postgresql://user:pass@host:port/dbname → dict."""
    m = re.match(
        r"postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)", url
    )
    if not m:
        raise ValueError("Cannot parse DB URL: %s" % url)
    return dict(
        user=m.group(1), password=m.group(2),
        host=m.group(3), port=int(m.group(4)), dbname=m.group(5),
    )


# ── Session-scoped DB setup ──────────────────────────────────────────────────

@pytest.fixture(scope="session")
def db_admin_conn():
    """Connection to the postgres admin DB to create/drop the test DB."""
    parts = _dsn_parts(ADMIN_URL)
    conn = psycopg2.connect(**parts)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    yield conn
    conn.close()


_MIGRATIONS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "app", "migrations.sql"
)


# ── SMS Mock fixture (prevent 5s timeout on http://smsgw:8025) ────────────────

@pytest.fixture(scope="session", autouse=True)
def mock_sms_gateway():
    """Mock requests.post calls to SMS gateway so tests run fast with no timeouts."""
    import requests
    original_post = requests.post

    def _fast_post(url, *args, **kwargs):
        if "8025" in str(url) or "sms" in str(url):
            class _MockResp:
                status_code = 200
                content = b'{"status": "queued", "id": 1}'
                headers = {"Content-Type": "application/json"}
                def json(self):
                    return {"status": "queued", "id": 1}
            return _MockResp()
        return original_post(url, *args, **kwargs)

    requests.post = _fast_post
    yield
    requests.post = original_post


@pytest.fixture(scope="session")
def test_db(db_admin_conn):
    """Create sewasetu_test, load schema.sql and migrations.sql; drop at end of session."""
    cur = db_admin_conn.cursor()
    cur.execute(
        "SELECT 1 FROM pg_database WHERE datname = %s", (_TEST_DB_NAME,)
    )
    if not cur.fetchone():
        cur.execute("CREATE DATABASE %s" % _TEST_DB_NAME)

    # Load schema (not seed)
    parts = _dsn_parts(TEST_DB_URL)
    conn = psycopg2.connect(**parts)
    with conn.cursor() as c:
        with open(_SCHEMA_PATH, encoding="utf-8") as f:
            c.execute(f.read())
        if os.path.exists(_MIGRATIONS_PATH):
            with open(_MIGRATIONS_PATH, encoding="utf-8") as f:
                c.execute(f.read())
    conn.commit()
    conn.close()

    yield TEST_DB_URL

    # Teardown — drop so next run is clean
    cur.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid()",
        (_TEST_DB_NAME,),
    )
    cur.execute("DROP DATABASE IF EXISTS %s" % _TEST_DB_NAME)
    cur.close()


# ── Per-test table cleanup ───────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_db(test_db):
    """Truncate tables before each test to guarantee complete test isolation."""
    parts = _dsn_parts(TEST_DB_URL)
    conn = psycopg2.connect(**parts)
    with conn.cursor() as cur:
        cur.execute("""
            DO $$
            BEGIN
                TRUNCATE applications, portal_users, otps, audit_log CASCADE;
                IF EXISTS (SELECT FROM pg_tables WHERE tablename = 'assistant_drafts') THEN
                    TRUNCATE assistant_drafts, assistant_signouts CASCADE;
                END IF;
            END $$;
        """)
        conn.commit()
    conn.close()
    yield


# ── Flask test client ─────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def flask_app(test_db):
    """Flask test application wired to sewasetu_test."""
    parts = _dsn_parts(TEST_DB_URL)

    # Patch the config that app.py reads at module import time.
    # We do this by monkey-patching the module-level get_db function AFTER import.
    import sys
    # Remove cached module so we can re-configure
    for mod in list(sys.modules.keys()):
        if mod in ("app", "oauth"):
            del sys.modules[mod]

    # Set env so oauth.py picks up a deterministic ISSUER
    os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")
    os.environ.setdefault("ADMIN_USERNAME", "admin")
    os.environ.setdefault("ADMIN_PASSWORD", "testpassword")

    import app as portal_app

    # Override get_db to use the test database
    def _test_get_db():
        return psycopg2.connect(**parts)

    portal_app.get_db = _test_get_db
    # Also patch references inside oauth module
    import oauth as oauth_mod
    oauth_mod.portal.get_db = _test_get_db

    portal_app.app.config["TESTING"] = True
    portal_app.app.config["WTF_CSRF_ENABLED"] = False
    portal_app.ADMIN_PASSWORD = "testpassword"
    portal_app.send_sms = lambda mobile, text: None

    return portal_app.app


@pytest.fixture()
def client(flask_app):
    """A test client; database changes are rolled back after each test."""
    # Patch get_db to reuse the per-test connection
    import app as portal_app
    import oauth as oauth_mod

    original = portal_app.get_db

    # We can't reuse db_conn because Flask may open multiple connections.
    # Instead use a fresh connection per request but pointing at test_db.
    parts = _dsn_parts(TEST_DB_URL)

    def _fresh():
        return psycopg2.connect(**parts)

    portal_app.get_db = _fresh
    oauth_mod.portal.get_db = _fresh

    with flask_app.test_client() as c:
        yield c

    portal_app.get_db = original
    oauth_mod.portal.get_db = original


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_fake_doc():
    """A minimal valid PDF byte string."""
    return (
        b"%PDF-1.0\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj "
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj "
        b"3 0 obj<</Type/Page/MediaBox[0 0 3 3]>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f \n"
        b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n9\n%%EOF"
    )


def do_apply(client, mobile="9876543210"):
    """POST to /apply (bypassing captcha via session seeding)."""
    with client.session_transaction() as sess:
        sess["captcha_answer"] = 7
    return client.post(
        "/apply",
        data={"mobile": mobile, "captcha": "7"},
        follow_redirects=False,
    )


def do_otp(client, mobile="9876543210"):
    """Look up the latest OTP from the DB and POST it to /verify."""
    import psycopg2
    parts = _dsn_parts(TEST_DB_URL)
    conn = psycopg2.connect(**parts)
    cur = conn.cursor()
    cur.execute(
        "SELECT code FROM otps WHERE mobile=%s ORDER BY id DESC LIMIT 1",
        (mobile,),
    )
    row = cur.fetchone()
    cur.close(); conn.close()
    assert row, "No OTP found for %s" % mobile
    return client.post("/verify", data={"otp": row[0]}, follow_redirects=False)


def fill_form(client):
    """Fill all 3 form steps with valid data."""
    client.post("/form/1", data={
        "applicant_name": "Sunita Devi",
        "dob": "01/01/1955",
        "gender": "Female",
        "marital_status": "Married",
        "husband_name": "Ramesh Kumar",
    })
    client.post("/form/2", data={
        "village": "Lakhanpur",
        "block": "Sonari",
    })
    client.post("/form/3", data={
        "bank_account": "123456789012",
        "ifsc": "SBIN0003077",
    })


def upload_doc(client):
    """Upload a fake age-proof document."""
    return client.post(
        "/upload",
        data={"document": (io.BytesIO(make_fake_doc()), "ageproof.pdf")},
        content_type="multipart/form-data",
        follow_redirects=False,
    )


def submit_declaration(client):
    """POST the declaration to create the application."""
    return client.post("/declaration", follow_redirects=False)
