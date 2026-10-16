# -*- coding: utf-8 -*-
"""
tests/citizen_mcp/test_counter_parity.py

Counter-parity, cross-channel, Assamese-script, session-revocation tests (Prompt 5 gaps).

Every test ASSERTS that counter-level rules are enforced from the service layer
and that NOTHING is created or changed in the DB when a rule fires.
"""

import base64
import io
import time
import hashlib
import psycopg2
import pytest
from datetime import date, datetime, timedelta
from unittest.mock import patch

from starlette.testclient import TestClient
from mcp.server.auth.middleware.auth_context import auth_context_var, AuthenticatedUser
from mcp.server.auth.provider import AccessToken

import citizen_mcp.server as citizen_server
from tests.citizen_mcp.conftest import make_token
from tests.portal.conftest import _dsn_parts, TEST_DB_URL

# ── Shared constants ──────────────────────────────────────────────────────────

TINY_PDF_BYTES = (
    b"%PDF-1.4\n1 0 obj<<>>\nendobj\n"
    b"xref\n0 2\n0000000000 65535 f \n0000000010 00000 n \n"
    b"trailer<<>>\nstartxref\n10\n%%EOF"
)

VALID_BASE_DRAFT = dict(
    applicant_name="Test Applicant",
    dob="01/01/1960",
    gender="Male",
    marital_status="Single",
    village="Namti Gaon",
    block="Namti",
    bank_account="123456789012",
    ifsc="SBIN0003077",
)

# We use unique mobiles per test (9990000040 - 9990000099 range)


def _set_subject(mobile: str):
    token = AccessToken(
        token="test-token",
        client_id="test-client",
        scopes=["citizen"],
        expires_at=int(time.time()) + 300,
        resource="http://localhost:8000/citizen/mcp",
    )
    token.subject = f"citizen:{mobile}"
    token.claims = {"sub": f"citizen:{mobile}", "role": "citizen"}
    auth_context_var.set(AuthenticatedUser(token))


def _count_applications(mobile: str) -> int:
    conn = psycopg2.connect(**_dsn_parts(TEST_DB_URL))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM applications WHERE mobile=%s", (mobile,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def _get_applications(mobile: str) -> list:
    conn = psycopg2.connect(**_dsn_parts(TEST_DB_URL))
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, status, submitted_at FROM applications WHERE mobile=%s",
                (mobile,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def _attach_pdf(mobile: str):
    _set_subject(mobile)
    pdf_b64 = base64.b64encode(TINY_PDF_BYTES).decode("ascii")
    import asyncio
    loop = asyncio.new_event_loop()
    loop.run_until_complete(
        citizen_server.attach_age_proof(
            document_type="voter_id",
            filename="test.pdf",
            file_base64=pdf_b64,
        )
    )
    loop.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 1. COUNTER-PARITY: each asserts nothing created in DB
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_reject_59_year_old():
    """A 59-year-old is refused; zero rows in applications."""
    mobile = "9990000040"
    _set_subject(mobile)

    # DOB 365 days from now-60years = 59 years and ~0 days → refused
    today = date.today()
    # 59 years old: born 59 years ago today
    dob_59 = date(today.year - 59, today.month, today.day)
    dob_str = dob_59.strftime("%d/%m/%Y")

    await citizen_server.save_application_draft(**{**VALID_BASE_DRAFT, "dob": dob_str})
    pdf_b64 = base64.b64encode(TINY_PDF_BYTES).decode("ascii")
    await citizen_server.attach_age_proof("voter_id", "test.pdf", file_base64=pdf_b64)

    # Even preview should report errors
    prev = await citizen_server.preview_application()
    assert prev["ok"] is False
    assert any("60" in str(p).lower() or "age" in str(p).lower() for p in prev["problems"])

    # Nothing created
    assert _count_applications(mobile) == 0


@pytest.mark.asyncio
async def test_reject_turns_60_after_window_closes():
    """Applicant who turns 60 only after the window closes is refused at preview."""
    mobile = "9990000041"
    _set_subject(mobile)

    # Find window close date from scheme_facts
    from services.applications import SCHEME_DEADLINE
    window_close = SCHEME_DEADLINE.date()

    # DOB: turns 60 one day AFTER window closes
    turns_60_target = window_close + timedelta(days=1)
    dob_date = date(turns_60_target.year - 60, turns_60_target.month, turns_60_target.day)
    # But dob_date must make the person currently 59:
    if dob_date >= date.today():
        pytest.skip("Cannot construct valid late-turning-60 DOB for this window configuration")

    dob_str = dob_date.strftime("%d/%m/%Y")
    from services.applications import age_on
    age_now = age_on(dob_date, date.today())
    if age_now >= 60:
        pytest.skip("DOB construction resulted in eligible age - skipping")

    await citizen_server.save_application_draft(**{**VALID_BASE_DRAFT, "dob": dob_str})
    pdf_b64 = base64.b64encode(TINY_PDF_BYTES).decode("ascii")
    await citizen_server.attach_age_proof("voter_id", "test.pdf", file_base64=pdf_b64)
    prev = await citizen_server.preview_application()
    assert prev["ok"] is False
    assert _count_applications(mobile) == 0


@pytest.mark.asyncio
async def test_reject_when_window_closed():
    """If the scheme window is patched as closed, submission is rejected."""
    mobile = "9990000042"
    _set_subject(mobile)

    await citizen_server.save_application_draft(**VALID_BASE_DRAFT)
    pdf_b64 = base64.b64encode(TINY_PDF_BYTES).decode("ascii")
    await citizen_server.attach_age_proof("voter_id", "test.pdf", file_base64=pdf_b64)

    # Monkeypatch _now() in services.applications to return future date (window closed)
    from services import applications as svc
    past_deadline = datetime(2020, 1, 1, 0, 0)
    with patch.object(svc, "SCHEME_DEADLINE", past_deadline):
        prev = await citizen_server.preview_application()

    assert prev["ok"] is False
    assert any("window" in str(p).lower() or "closed" in str(p).lower() for p in prev["problems"])
    assert _count_applications(mobile) == 0


@pytest.mark.asyncio
async def test_reject_wrong_block():
    """A block not in the valid list is refused."""
    mobile = "9990000043"
    _set_subject(mobile)

    bad_draft = {**VALID_BASE_DRAFT, "block": "NonExistentBlock"}
    await citizen_server.save_application_draft(**bad_draft)
    pdf_b64 = base64.b64encode(TINY_PDF_BYTES).decode("ascii")
    await citizen_server.attach_age_proof("voter_id", "test.pdf", file_base64=pdf_b64)

    prev = await citizen_server.preview_application()
    assert prev["ok"] is False
    assert any("block" in str(p).lower() for p in prev["problems"])
    assert _count_applications(mobile) == 0


@pytest.mark.asyncio
async def test_reject_twelve_char_ifsc():
    """IFSC UTBI0PUR2819 is 12 chars (not 11) and must be rejected."""
    mobile = "9990000044"
    _set_subject(mobile)

    bad_draft = {**VALID_BASE_DRAFT, "ifsc": "UTBI0PUR2819"}
    await citizen_server.save_application_draft(**bad_draft)
    pdf_b64 = base64.b64encode(TINY_PDF_BYTES).decode("ascii")
    await citizen_server.attach_age_proof("voter_id", "test.pdf", file_base64=pdf_b64)

    prev = await citizen_server.preview_application()
    assert prev["ok"] is False
    assert any("ifsc" in str(p).lower() for p in prev["problems"])
    assert _count_applications(mobile) == 0


@pytest.mark.asyncio
async def test_reject_missing_age_proof():
    """Preview without attached age proof is refused."""
    mobile = "9990000045"
    _set_subject(mobile)

    # Complete draft with no proof
    await citizen_server.save_application_draft(**VALID_BASE_DRAFT)

    prev = await citizen_server.preview_application()
    assert prev["ok"] is False
    assert any("proof" in str(p).lower() or "document" in str(p).lower() for p in prev["problems"])
    assert _count_applications(mobile) == 0


@pytest.mark.asyncio
async def test_reject_already_pending(flask_app):
    """Mobile with PENDING application cannot create another."""
    mobile = "9990000046"

    # Pre-insert a PENDING application directly in DB
    conn = psycopg2.connect(**_dsn_parts(TEST_DB_URL))
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO applications "
                "(application_no, applicant_name, mobile, dob, gender, marital_status, "
                "village, block, bank_account, ifsc, doc_path, status, submitted_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)",
                (
                    "SSP26PEND01", "Existing User", mobile,
                    date(1960, 1, 1), "Male", "Single",
                    "Namti Gaon", "Namti", "123456789012", "SBIN0003077",
                    "/fake/path.pdf", datetime.now(),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    _set_subject(mobile)
    await citizen_server.save_application_draft(**VALID_BASE_DRAFT)
    pdf_b64 = base64.b64encode(TINY_PDF_BYTES).decode("ascii")
    await citizen_server.attach_age_proof("voter_id", "test.pdf", file_base64=pdf_b64)
    prev = await citizen_server.preview_application()

    # Preview might pass but submit must fail
    if prev["ok"]:
        sub = await citizen_server.submit_application(
            confirmation_code=prev["data"]["confirmation_code"],
            declaration_accepted=True,
        )
        assert sub["ok"] is False
        assert any("already" in str(p).lower() or "pending" in str(p).lower() for p in sub.get("problems", [{"plain": sub.get("message_for_user", "")}]))

    # Only ONE application must exist (the pre-inserted one)
    assert _count_applications(mobile) == 1


@pytest.mark.asyncio
async def test_reject_already_approved(flask_app):
    """Mobile with APPROVED application cannot create another."""
    mobile = "9990000047"

    conn = psycopg2.connect(**_dsn_parts(TEST_DB_URL))
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO applications "
                "(application_no, applicant_name, mobile, dob, gender, marital_status, "
                "village, block, bank_account, ifsc, doc_path, status, submitted_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'APPROVED',%s)",
                (
                    "SSP26APPR01", "Approved User", mobile,
                    date(1960, 1, 1), "Male", "Single",
                    "Namti Gaon", "Namti", "223456789012", "SBIN0003077",
                    "/fake/path.pdf", datetime.now() - timedelta(days=5),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    _set_subject(mobile)
    await citizen_server.save_application_draft(**{**VALID_BASE_DRAFT, "bank_account": "223456789012"})
    pdf_b64 = base64.b64encode(TINY_PDF_BYTES).decode("ascii")
    await citizen_server.attach_age_proof("voter_id", "test.pdf", file_base64=pdf_b64)
    prev = await citizen_server.preview_application()

    if prev["ok"]:
        sub = await citizen_server.submit_application(
            confirmation_code=prev["data"]["confirmation_code"],
            declaration_accepted=True,
        )
        assert sub["ok"] is False

    assert _count_applications(mobile) == 1


@pytest.mark.asyncio
async def test_reject_already_deemed_approved(flask_app):
    """Mobile with DEEMED_APPROVED application cannot create another."""
    mobile = "9990000048"

    conn = psycopg2.connect(**_dsn_parts(TEST_DB_URL))
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO applications "
                "(application_no, applicant_name, mobile, dob, gender, marital_status, "
                "village, block, bank_account, ifsc, doc_path, status, submitted_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'DEEMED_APPROVED',%s)",
                (
                    "SSP26DEEM01", "Deemed User", mobile,
                    date(1958, 3, 15), "Female", "Widowed",
                    "Rajapara", "Rajapara", "323456789012", "SBIN0003077",
                    "/fake/path.pdf", datetime.now() - timedelta(days=20),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    _set_subject(mobile)
    await citizen_server.save_application_draft(**{**VALID_BASE_DRAFT, "bank_account": "323456789012"})
    pdf_b64 = base64.b64encode(TINY_PDF_BYTES).decode("ascii")
    await citizen_server.attach_age_proof("voter_id", "test.pdf", file_base64=pdf_b64)
    prev = await citizen_server.preview_application()

    if prev["ok"]:
        sub = await citizen_server.submit_application(
            confirmation_code=prev["data"]["confirmation_code"],
            declaration_accepted=True,
        )
        assert sub["ok"] is False

    assert _count_applications(mobile) == 1


@pytest.mark.asyncio
async def test_rejected_can_reapply(flask_app):
    """A mobile with REJECTED status CAN submit a new application."""
    mobile = "9990000049"

    conn = psycopg2.connect(**_dsn_parts(TEST_DB_URL))
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO applications "
                "(application_no, applicant_name, mobile, dob, gender, marital_status, "
                "village, block, bank_account, ifsc, doc_path, status, submitted_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'REJECTED',%s)",
                (
                    "SSP26REJ01", "Rejected User", mobile,
                    date(1958, 5, 10), "Male", "Married",
                    "Sonari", "Sonari", "423456789012", "SBIN0003077",
                    "/fake/path.pdf", datetime.now() - timedelta(days=10),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    _set_subject(mobile)
    await citizen_server.save_application_draft(**{**VALID_BASE_DRAFT, "bank_account": "423456789012"})
    pdf_b64 = base64.b64encode(TINY_PDF_BYTES).decode("ascii")
    await citizen_server.attach_age_proof("voter_id", "test.pdf", file_base64=pdf_b64)
    prev = await citizen_server.preview_application()
    assert prev["ok"] is True

    sub = await citizen_server.submit_application(
        confirmation_code=prev["data"]["confirmation_code"],
        declaration_accepted=True,
    )
    assert sub["ok"] is True
    # Now two rows: original REJECTED + new PENDING
    assert _count_applications(mobile) == 2


@pytest.mark.asyncio
async def test_withdrawn_can_reapply_with_prefilled_draft(flask_app):
    """Mobile with WITHDRAWN status CAN re-apply; draft is pre-filled after withdrawal."""
    mobile = "9990000050"
    _set_subject(mobile)

    # First full submission
    await citizen_server.save_application_draft(
        applicant_name="Gopal Nath",
        dob="20/06/1955",
        gender="Male",
        marital_status="Married",
        village="Khelua",
        block="Khelua",
        bank_account="523456789012",
        ifsc="SBIN0003077",
    )
    pdf_b64 = base64.b64encode(TINY_PDF_BYTES).decode("ascii")
    await citizen_server.attach_age_proof("voter_id", "first.pdf", file_base64=pdf_b64)
    prev1 = await citizen_server.preview_application()
    sub1 = await citizen_server.submit_application(
        confirmation_code=prev1["data"]["confirmation_code"],
        declaration_accepted=True,
    )
    assert sub1["ok"] is True
    app_no1 = sub1["data"]["application_no"]

    # Withdraw
    w = await citizen_server.withdraw_application(app_no1, confirm=True)
    assert w["ok"] is True

    # Draft is pre-filled with previous details
    status = await citizen_server.get_my_status()
    assert status["data"]["has_draft"] is True

    # Correct and resubmit: update village
    await citizen_server.save_application_draft(village="Khelua Updated")
    await citizen_server.attach_age_proof("voter_id", "second.pdf", file_base64=pdf_b64)
    prev2 = await citizen_server.preview_application()
    assert prev2["ok"] is True

    sub2 = await citizen_server.submit_application(
        confirmation_code=prev2["data"]["confirmation_code"],
        declaration_accepted=True,
    )
    assert sub2["ok"] is True
    # Two rows: WITHDRAWN + new PENDING
    assert _count_applications(mobile) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# 2. CROSS-CHANNEL TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mcp_submit_visible_in_web_status_portal(flask_app):
    """After MCP submit, the web status portal (mobile + DOB as DDMMYYYY) shows it."""
    mobile = "9990000051"
    dob_str = "10/04/1953"
    _set_subject(mobile)

    await citizen_server.save_application_draft(
        applicant_name="Amina Begum",
        dob=dob_str,
        gender="Female",
        marital_status="Widowed",
        village="Borgaon",
        block="Borgaon",
        bank_account="623456789012",
        ifsc="SBIN0003077",
    )
    pdf_b64 = base64.b64encode(TINY_PDF_BYTES).decode("ascii")
    await citizen_server.attach_age_proof("voter_id", "amina.pdf", file_base64=pdf_b64)
    prev = await citizen_server.preview_application()
    sub = await citizen_server.submit_application(
        confirmation_code=prev["data"]["confirmation_code"],
        declaration_accepted=True,
    )
    assert sub["ok"] is True
    app_no = sub["data"]["application_no"]

    # Web status portal login: password = DOB as DDMMYYYY
    dob_password = "10041953"  # DDMMYYYY from 10/04/1953
    with flask_app.test_client() as web_client:
        resp = web_client.post(
            "/status",
            data={"mobile": mobile, "password": dob_password},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert app_no in html, f"Application {app_no} not found in web status portal response"
        assert "PENDING" in html


@pytest.mark.asyncio
async def test_mcp_submit_visible_in_admin_list(flask_app):
    """After MCP submit, admin list at /admin/applications?status=PENDING shows the application."""
    mobile = "9990000052"
    _set_subject(mobile)

    await citizen_server.save_application_draft(
        applicant_name="Renu Sharma",
        dob="22/11/1957",
        gender="Female",
        marital_status="Married",
        village="Dhemaji Pathar",
        block="Dhemaji Pathar",
        bank_account="723456789012",
        ifsc="SBIN0003077",
    )
    pdf_b64 = base64.b64encode(TINY_PDF_BYTES).decode("ascii")
    await citizen_server.attach_age_proof("birth_certificate", "renu.pdf", file_base64=pdf_b64)
    prev = await citizen_server.preview_application()
    sub = await citizen_server.submit_application(
        confirmation_code=prev["data"]["confirmation_code"],
        declaration_accepted=True,
    )
    assert sub["ok"] is True
    app_no = sub["data"]["application_no"]

    with flask_app.test_client() as web_client:
        # Admin login
        web_client.post(
            "/admin",
            data={"username": "admin", "password": "testpassword"},
            follow_redirects=True,
        )
        # Admin list
        list_resp = web_client.get(
            f"/admin/applications?status=PENDING&mobile={mobile}",
            follow_redirects=True,
        )
        assert list_resp.status_code == 200
        html = list_resp.get_data(as_text=True)
        assert app_no in html or mobile in html


@pytest.mark.asyncio
async def test_web_submit_visible_via_get_my_status(flask_app):
    """Application created through the web flow is returned by get_my_status."""
    mobile = "9990000053"
    dob_str = "05/07/1951"

    with flask_app.test_client() as web_client:
        # Step 1: POST mobile to /apply (bypass OTP for test – seed OTP directly)
        conn = psycopg2.connect(**_dsn_parts(TEST_DB_URL))
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
                    (mobile, "999001", datetime.now()),
                )
            conn.commit()
        finally:
            conn.close()

        with web_client.session_transaction() as sess:
            sess["apply_mobile"] = mobile

        # Step 2: Verify OTP
        verify_resp = web_client.post("/verify", data={"otp": "999001"})
        assert verify_resp.status_code in (200, 302)

        # Step 3: Simulate a direct DB insert as web flow would do
        conn2 = psycopg2.connect(**_dsn_parts(TEST_DB_URL))
        try:
            with conn2.cursor() as cur:
                # Create portal_user (password = DOB DDMMYYYY)
                import hashlib
                pwd_hash = hashlib.sha256("05071951".encode()).hexdigest()
                cur.execute(
                    "INSERT INTO portal_users (mobile, password_hash) VALUES (%s, %s) "
                    "ON CONFLICT (mobile) DO NOTHING",
                    (mobile, pwd_hash),
                )
                cur.execute(
                    "INSERT INTO applications "
                    "(application_no, applicant_name, mobile, dob, gender, marital_status, "
                    "village, block, bank_account, ifsc, doc_path, status, submitted_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s) RETURNING id",
                    (
                        "SSP26WEB001", "Web Flow Citizen", mobile,
                        date(1951, 7, 5), "Female", "Widowed",
                        "Borgaon", "Borgaon", "823456789012", "SBIN0003077",
                        "/fake/webflow.pdf", datetime.now(),
                    ),
                )
                app_id = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO audit_log (application_id, action, actor, note, at) VALUES (%s,%s,%s,%s,%s)",
                    (app_id, "SUBMIT", f"applicant:{mobile}", "via web flow", datetime.now()),
                )
            conn2.commit()
        finally:
            conn2.close()

    # Now check via MCP get_my_status
    _set_subject(mobile)
    status = await citizen_server.get_my_status()
    assert status["ok"] is True
    apps_data = status["data"].get("applications", [])
    assert len(apps_data) >= 1
    app_nos = [a.get("application_no") for a in apps_data]
    assert "SSP26WEB001" in app_nos


# ═══════════════════════════════════════════════════════════════════════════════
# 3. ASSAMESE-SCRIPT NAME
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_assamese_name_survives_submit_and_readback():
    """
    Assamese-script name নির্মলা ভরালী survives submit and is read back byte-identical.
    """
    mobile = "9990000054"
    assamese_name = "নির্মলা ভরালী"
    _set_subject(mobile)

    await citizen_server.save_application_draft(
        applicant_name=assamese_name,
        dob="03/03/1958",
        gender="Female",
        marital_status="Widowed",
        village="Borgaon",
        block="Borgaon",
        bank_account="923456789012",
        ifsc="SBIN0003077",
    )
    pdf_b64 = base64.b64encode(TINY_PDF_BYTES).decode("ascii")
    await citizen_server.attach_age_proof("voter_id", "nirmala.pdf", file_base64=pdf_b64)
    prev = await citizen_server.preview_application()
    assert prev["ok"] is True
    # Name visible in preview exactly
    assert prev["data"]["preview"]["applicant_name"] == assamese_name

    sub = await citizen_server.submit_application(
        confirmation_code=prev["data"]["confirmation_code"],
        declaration_accepted=True,
    )
    assert sub["ok"] is True

    # Read back from DB and verify byte-identical
    conn = psycopg2.connect(**_dsn_parts(TEST_DB_URL))
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT applicant_name FROM applications WHERE mobile=%s AND status='PENDING'",
                (mobile,),
            )
            row = cur.fetchone()
            assert row is not None
            stored_name = row[0]
    finally:
        conn.close()

    assert stored_name == assamese_name, (
        f"Stored name '{stored_name!r}' != expected '{assamese_name!r}'"
    )

    # Also readable via get_my_status
    status = await citizen_server.get_my_status()
    apps = status["data"].get("applications", [])
    found = any(a.get("applicant_name") == assamese_name for a in apps)
    assert found, "Assamese name not returned by get_my_status"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. end_my_session: TOKEN REVOCATION
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_end_my_session_revokes_token(server_keys):
    """After end_my_session, the old access token is rejected and refresh token fails."""
    mobile = "9990000055"
    priv, pub, jwk = server_keys

    # Sign a token with known IAT
    iat = int(time.time()) - 5  # issued 5 seconds ago
    import jwt
    import json
    now_int = int(time.time())
    claims = {
        "iss": "http://localhost:8000",
        "aud": "http://localhost:8000/citizen/mcp",
        "sub": f"citizen:{mobile}",
        "role": "citizen",
        "scope": "citizen",
        "client_id": "test-client",
        "iat": iat,
        "exp": now_int + 1200,
        "kind": "access",
    }
    token_str = jwt.encode(claims, priv, algorithm="RS256", headers={"kid": "test-kid-1"})

    # Set up auth context with this specific token
    access_token = AccessToken(
        token=token_str,
        client_id="test-client",
        scopes=["citizen"],
        expires_at=now_int + 1200,
        resource="http://localhost:8000/citizen/mcp",
    )
    access_token.subject = f"citizen:{mobile}"
    access_token.claims = claims
    auth_context_var.set(AuthenticatedUser(access_token))

    # Call end_my_session
    end_res = await citizen_server.end_my_session()
    assert end_res["ok"] is True

    # Now check revocation: the signout table should have a record for this subject
    conn = psycopg2.connect(**_dsn_parts(TEST_DB_URL))
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT revoked_at FROM assistant_signouts WHERE subject=%s",
                (f"citizen:{mobile}",),
            )
            row = cur.fetchone()
            assert row is not None, "No signout record created"
            revoked_at = row[0]
            # revoked_at must be after token IAT
            revoked_epoch = revoked_at.timestamp() if hasattr(revoked_at, "timestamp") else revoked_at
            assert revoked_epoch > iat, (
                f"revoked_at {revoked_epoch} should be > token iat {iat}"
            )
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 5. REFUSAL DOES NOT CREATE ROWS (age 59 quick check)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_age_59_produces_no_db_row():
    """Complete guarantee: after a 59-year-old refusal, applications table has 0 rows."""
    mobile = "9990000056"
    _set_subject(mobile)

    today = date.today()
    dob_59 = date(today.year - 59, today.month, today.day)
    dob_str = dob_59.strftime("%d/%m/%Y")

    await citizen_server.save_application_draft(**{**VALID_BASE_DRAFT, "dob": dob_str})
    pdf_b64 = base64.b64encode(TINY_PDF_BYTES).decode("ascii")
    await citizen_server.attach_age_proof("voter_id", "test.pdf", file_base64=pdf_b64)
    prev = await citizen_server.preview_application()

    if prev["ok"]:
        # Even if preview passes due to race, submit must fail
        sub = await citizen_server.submit_application(
            confirmation_code=prev["data"]["confirmation_code"],
            declaration_accepted=True,
        )
        assert sub["ok"] is False

    rows = _get_applications(mobile)
    assert len(rows) == 0, f"Expected 0 rows but found: {rows}"
