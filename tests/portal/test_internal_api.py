"""Tests for the internal API (sections E, G — parity, auth, race, decisions, visibility).

These tests call /internal/api/v1/* directly with psycopg2 + Flask test client.
"""
import json
import time
import threading
import psycopg2
import pytest

from .conftest import (
    TEST_DB_URL, _dsn_parts,
    do_apply, do_otp, fill_form, upload_doc, submit_declaration, make_fake_doc,
)

INTERNAL_KEY = "test-internal-key-1234"
CITIZEN_MOBILE = "9100000001"
CITIZEN_SUBJECT = "citizen:" + CITIZEN_MOBILE
OFFICER_SUBJECT = "officer:admin"


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def patch_internal_key(flask_app, monkeypatch):
    """Make INTERNAL_API_KEY available in the internal_api module."""
    import internal_api
    monkeypatch.setattr(internal_api, "INTERNAL_API_KEY", INTERNAL_KEY)


def _citizen_headers(mobile=CITIZEN_MOBILE):
    return {
        "X-Internal-Key": INTERNAL_KEY,
        "X-Acting-Subject": "citizen:" + mobile,
        "Content-Type": "application/json",
    }


def _officer_headers():
    return {
        "X-Internal-Key": INTERNAL_KEY,
        "X-Acting-Subject": OFFICER_SUBJECT,
        "Content-Type": "application/json",
    }


def _db():
    return psycopg2.connect(**_dsn_parts(TEST_DB_URL))


def _create_pending_app(mobile, app_name="Sunita Devi"):
    """Directly insert a PENDING application for a mobile."""
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO applications
           (application_no, applicant_name, mobile, dob, gender, marital_status,
            husband_name, husband_employer, village, block, bank_account, ifsc,
            doc_path, status, submitted_at)
           VALUES (%s,%s,%s,'1955-01-15','Female','Married','Ramesh','',
                   'Lakhanpur','Sonari','123456789012','SBIN0003077',
                   '/fake/doc.pdf','PENDING',now())
           RETURNING id, application_no""",
        ("SSP26%06d" % (hash(mobile) % 999999), app_name, mobile),
    )
    app_id, app_no = cur.fetchone()
    cur.execute(
        "INSERT INTO portal_users (mobile, password_hash) VALUES (%s,%s) "
        "ON CONFLICT DO NOTHING",
        (mobile, "fake"),
    )
    cur.execute(
        "INSERT INTO audit_log (application_id, action, actor, note, at) "
        "VALUES (%s,'SUBMIT',%s,'',now())",
        (app_id, "applicant:%s" % mobile),
    )
    conn.commit()
    cur.close(); conn.close()
    return app_id, app_no


# ── Section E: Auth tests ─────────────────────────────────────────────────

class TestAuth:
    def test_missing_key_returns_403(self, client):
        resp = client.get("/internal/api/v1/scheme",
                          headers={"X-Acting-Subject": CITIZEN_SUBJECT})
        assert resp.status_code == 403

    def test_wrong_key_returns_403(self, client):
        resp = client.get("/internal/api/v1/scheme",
                          headers={"X-Internal-Key": "wrong",
                                   "X-Acting-Subject": CITIZEN_SUBJECT})
        assert resp.status_code == 403

    def test_citizen_on_officer_route_returns_403(self, client):
        resp = client.get("/internal/api/v1/officer/overview",
                          headers=_citizen_headers())
        assert resp.status_code == 403

    def test_officer_on_citizen_route_returns_403(self, client):
        resp = client.get("/internal/api/v1/me/status",
                          headers=_officer_headers())
        assert resp.status_code == 403

    def test_bad_subject_format_returns_403(self, client):
        resp = client.get("/internal/api/v1/scheme",
                          headers={"X-Internal-Key": INTERNAL_KEY,
                                   "X-Acting-Subject": "admin:someone"})
        assert resp.status_code == 403

    def test_valid_citizen_can_access_scheme(self, client):
        resp = client.get("/internal/api/v1/scheme", headers=_citizen_headers())
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_valid_officer_can_access_overview(self, client):
        resp = client.get("/internal/api/v1/officer/overview", headers=_officer_headers())
        assert resp.status_code == 200


# ── Section E: Scheme facts ───────────────────────────────────────────────

class TestSchemeFacts:
    def test_scheme_facts_structure(self, client):
        resp = client.get("/internal/api/v1/scheme", headers=_citizen_headers())
        data = resp.get_json()["data"]
        assert "amount_rs_per_month" in data
        assert data["amount_rs_per_month"] == 250
        assert "min_age" in data
        assert data["min_age"] == 60
        assert "sla_days" in data
        assert "valid_blocks" in data
        assert "accepted_documents" in data


# ── Section E: Parity tests ───────────────────────────────────────────────

class TestParity:
    """Same inputs through web form and API yield identical error lists."""

    BAD_INPUTS = [
        # (description, data_overrides, doc_path, expected_error_fragment)
        ("bad IFSC", {"ifsc": "BADIFSC123"}, "/fake/doc.pdf", "IFSC must be 11"),
        ("short bank account", {"bank_account": "12345"}, "/fake/doc.pdf", "bank account number"),
        ("bad block", {"block": "UnknownBlock"}, "/fake/doc.pdf", "block must be one of"),
        ("missing name", {"applicant_name": ""}, "/fake/doc.pdf", "full name is required"),
        ("missing village", {"village": ""}, "/fake/doc.pdf", "village is required"),
        ("no document", {}, "", "age proof document is required"),
        # F-rules
        ("future DOB", {"dob": "01/01/2050"}, "/fake/doc.pdf", "cannot be in the future"),
        ("too old DOB", {"dob": "01/01/1900"}, "/fake/doc.pdf", "implies an age over 110"),
        ("name too short", {"applicant_name": "A"}, "/fake/doc.pdf", "at least 2 characters"),
        ("name no letters", {"applicant_name": "123"}, "/fake/doc.pdf", "at least one letter"),
    ]

    def _base_data(self):
        return {
            "applicant_name": "Sunita Devi",
            "dob": "01/01/1955",
            "gender": "Female",
            "marital_status": "Married",
            "husband_name": "Ramesh",
            "village": "Lakhanpur",
            "block": "Sonari",
            "bank_account": "123456789012",
            "ifsc": "SBIN0003077",
        }

    def test_validate_matches_for_bad_ifsc(self, client):
        from services.applications import validate_application
        data = self._base_data()
        data["ifsc"] = "BADIFSC123"
        errors, _ = validate_application(data, "/fake/doc.pdf")
        assert any("IFSC" in e for e in errors)

    def test_api_and_service_agree_on_bad_ifsc(self, client):
        """PUT /me/draft should surface the same IFSC error as validate_application."""
        from services.applications import validate_application
        data = self._base_data()
        data["ifsc"] = "BADIFSC123"

        # Service
        svc_errors, _ = validate_application(data, "/fake/doc.pdf")
        svc_ifsc_errors = [e for e in svc_errors if "IFSC" in e]

        # API draft endpoint
        mobile = "9100001001"
        resp = client.put(
            "/internal/api/v1/me/draft",
            headers=_citizen_headers(mobile),
            data=json.dumps(data),
        )
        assert resp.status_code == 200
        api_problems = resp.get_json()["data"]["problems"]
        api_ifsc_errors = [p["plain"] for p in api_problems if "IFSC" in p["plain"]]

        assert svc_ifsc_errors, "Service should flag bad IFSC"
        assert api_ifsc_errors, "API should flag bad IFSC"

    def test_59_year_old_blocked(self, client):
        """Applicant who is 59 today should be rejected."""
        from services.applications import validate_application
        import datetime
        today = datetime.date.today()
        dob_59 = today.replace(year=today.year - 59)
        dob_str = dob_59.strftime("%d/%m/%Y")
        data = self._base_data()
        data["dob"] = dob_str
        errors, _ = validate_application(data, "/fake/doc.pdf")
        assert any("60" in e for e in errors)

    def test_assamese_name_passes_validation(self, client):
        """Assamese script name 'নির্মলা ভরালী' must pass validation."""
        from services.applications import validate_application
        data = self._base_data()
        data["applicant_name"] = "নির্মলা ভরালী"
        errors, cleaned = validate_application(data, "/fake/doc.pdf")
        name_errors = [e for e in errors if "name" in e.lower()]
        assert not name_errors, "Assamese name should pass: %s" % name_errors
        assert cleaned["applicant_name"] == "নির্মলা ভরালী"

    @pytest.mark.parametrize("desc,overrides,doc_path,fragment", BAD_INPUTS)
    def test_bad_inputs(self, client, desc, overrides, doc_path, fragment):
        from services.applications import validate_application
        data = self._base_data()
        data.update(overrides)
        errors, _ = validate_application(data, doc_path)
        assert any(fragment in e for e in errors), (
            "%s: expected '%s' in errors %s" % (desc, fragment, errors)
        )


# ── Section E: Race condition test ────────────────────────────────────────

class TestRace:
    """10 concurrent submissions for one mobile → exactly 1 active row."""

    def test_concurrent_submissions(self, client):
        mobile = "9100002001"
        results = []
        errors = []

        def submit():
            try:
                from services.applications import submit_application
                conn = psycopg2.connect(**_dsn_parts(TEST_DB_URL))
                cur = conn.cursor()
                data = {
                    "applicant_name": "Race Test",
                    "dob": "01/01/1955",
                    "gender": "Female",
                    "marital_status": "Married",
                    "husband_name": "H",
                    "village": "V",
                    "block": "Sonari",
                    "bank_account": "123456789012",
                    "ifsc": "SBIN0003077",
                }
                result = submit_application(cur, mobile, data, "/fake/doc.pdf")
                if result["ok"]:
                    conn.commit()
                    results.append("ok")
                else:
                    conn.rollback()
                    results.append("err")
                cur.close(); conn.close()
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=submit) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, "Threads raised exceptions: %s" % errors

        conn = _db()
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*) FROM applications WHERE mobile=%s "
            "AND status IN ('PENDING','APPROVED','DEEMED_APPROVED')",
            (mobile,),
        )
        count = cur.fetchone()[0]
        cur.close(); conn.close()

        assert count == 1, "Expected 1 active row, got %d" % count
        ok_count = results.count("ok")
        assert ok_count == 1, "Expected exactly 1 success, got %d" % ok_count


# ── Section E: Decision tests ─────────────────────────────────────────────

class TestDecisions:
    def test_only_pending_can_be_decided(self, client):
        mobile = "9100003001"
        app_id, app_no = _create_pending_app(mobile)

        # Approve it first
        resp = client.post(
            "/internal/api/v1/officer/applications/%s/decision" % app_no,
            headers=_officer_headers(),
            data=json.dumps({"decision": "approved", "note": "ok", "confirm": True}),
        )
        assert resp.status_code == 200

        # Try to approve again
        resp2 = client.post(
            "/internal/api/v1/officer/applications/%s/decision" % app_no,
            headers=_officer_headers(),
            data=json.dumps({"decision": "approved", "note": "again", "confirm": True}),
        )
        data = resp2.get_json()
        assert data["ok"] is False
        assert "pending" in data["message_for_user"].lower()

    def test_audit_actor_is_admin_username(self, client):
        mobile = "9100003002"
        app_id, app_no = _create_pending_app(mobile)

        client.post(
            "/internal/api/v1/officer/applications/%s/decision" % app_no,
            headers=_officer_headers(),
            data=json.dumps({"decision": "approved", "note": "all ok", "confirm": True}),
        )

        conn = _db()
        cur = conn.cursor()
        cur.execute(
            "SELECT actor FROM audit_log WHERE application_id=%s AND action='APPROVED'",
            (app_id,),
        )
        actor = cur.fetchone()[0]
        cur.close(); conn.close()
        assert actor == "admin:admin"

    def test_decided_by_is_username(self, client):
        mobile = "9100003003"
        app_id, app_no = _create_pending_app(mobile)

        client.post(
            "/internal/api/v1/officer/applications/%s/decision" % app_no,
            headers=_officer_headers(),
            data=json.dumps({"decision": "approved", "note": "ok", "confirm": True}),
        )

        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT decided_by FROM applications WHERE id=%s", (app_id,))
        decided_by = cur.fetchone()[0]
        cur.close(); conn.close()
        assert decided_by == "admin"

    def test_note_carries_officer_assistant_provenance(self, client):
        mobile = "9100003004"
        app_id, app_no = _create_pending_app(mobile)

        client.post(
            "/internal/api/v1/officer/applications/%s/decision" % app_no,
            headers=_officer_headers(),
            data=json.dumps({"decision": "rejected", "note": "missing doc", "confirm": True}),
        )

        conn = _db()
        cur = conn.cursor()
        cur.execute(
            "SELECT note FROM audit_log WHERE application_id=%s AND action='REJECTED'",
            (app_id,),
        )
        note = cur.fetchone()[0]
        cur.close(); conn.close()
        assert "officer assistant" in note.lower()

    def test_note_required_for_approval(self, client):
        mobile = "9100003005"
        _, app_no = _create_pending_app(mobile)

        resp = client.post(
            "/internal/api/v1/officer/applications/%s/decision" % app_no,
            headers=_officer_headers(),
            data=json.dumps({"decision": "approved", "note": "", "confirm": True}),
        )
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_note_required_for_rejection(self, client):
        mobile = "9100003006"
        _, app_no = _create_pending_app(mobile)

        resp = client.post(
            "/internal/api/v1/officer/applications/%s/decision" % app_no,
            headers=_officer_headers(),
            data=json.dumps({"decision": "rejected", "note": "", "confirm": True}),
        )
        assert resp.status_code == 400


# ── Section E: Assamese round-trip ────────────────────────────────────────

class TestAssamese:
    def test_assamese_name_roundtrip(self, client):
        """Assamese name must survive DB insert and API retrieval unchanged."""
        assamese_name = "নির্মলা ভরালী"
        mobile = "9100004001"
        app_id, app_no = _create_pending_app(mobile, app_name=assamese_name)

        resp = client.get(
            "/internal/api/v1/officer/applications/%s" % app_no,
            headers=_officer_headers(),
        )
        assert resp.status_code == 200
        returned_name = resp.get_json()["data"]["application"]["applicant_name"]
        assert returned_name == assamese_name


# ── Section G: Cross-channel visibility ──────────────────────────────────

class TestCrossChannelVisibility:
    def _submit_via_api(self, client, mobile):
        """Create an application directly via service layer."""
        from services.applications import submit_application
        conn = psycopg2.connect(**_dsn_parts(TEST_DB_URL))
        cur = conn.cursor()
        data = {
            "applicant_name": "Cross Channel Test",
            "dob": "15/06/1950",
            "gender": "Female",
            "marital_status": "Widowed",
            "husband_name": "",
            "village": "Namtipur",
            "block": "Namti",
            "bank_account": "987654321098",
            "ifsc": "SBIN0001234",
        }
        result = submit_application(cur, mobile, data, "/fake/proof.pdf",
                                    actor_note="via citizen assistant")
        conn.commit()
        cur.close(); conn.close()
        return result

    def test_api_apply_visible_on_web_status(self, client):
        """Apply via API → web /status shows the application."""
        mobile = "9200001001"
        result = self._submit_via_api(client, mobile)
        assert result["ok"], result

        # Verify portal_users row created
        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM portal_users WHERE mobile=%s", (mobile,))
        assert cur.fetchone(), "portal_users row missing"

        # Verify applications row visible in admin list
        cur.execute(
            "SELECT application_no FROM applications WHERE mobile=%s AND status='PENDING'",
            (mobile,),
        )
        assert cur.fetchone()[0] == result["application_no"]
        cur.close(); conn.close()

    def test_web_apply_visible_via_api(self, client):
        """Apply via web form → API me/status shows the application."""
        mobile = "9200002001"
        do_apply(client, mobile)
        do_otp(client, mobile)
        fill_form(client)
        upload_doc(client)
        submit_declaration(client)

        resp = client.get("/internal/api/v1/me/status",
                          headers=_citizen_headers(mobile))
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert len(data["applications"]) >= 1
        statuses = [a["status"] for a in data["applications"]]
        assert "PENDING" in statuses

    def test_api_withdraw_visible_on_web(self, client):
        """Withdraw via API → web status shows WITHDRAWN."""
        mobile = "9200003001"
        result = self._submit_via_api(client, mobile)
        app_no = result["application_no"]

        # Withdraw via API
        resp = client.post(
            "/internal/api/v1/me/applications/%s/withdraw" % app_no,
            headers=_citizen_headers(mobile),
            data=json.dumps({"confirm": True}),
        )
        assert resp.get_json()["ok"] is True

        # Check DB status
        conn = _db()
        cur = conn.cursor()
        cur.execute(
            "SELECT status FROM applications WHERE application_no=%s", (app_no,)
        )
        assert cur.fetchone()[0] == "WITHDRAWN"
        cur.close(); conn.close()

    def test_web_withdraw_visible_via_api(self, client):
        """Withdraw via web → API me/status shows WITHDRAWN."""
        mobile = "9200004001"
        do_apply(client, mobile)
        do_otp(client, mobile)
        fill_form(client)
        upload_doc(client)
        submit_declaration(client)

        conn = _db()
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM applications WHERE mobile=%s ORDER BY submitted_at DESC LIMIT 1",
            (mobile,),
        )
        app_id = cur.fetchone()[0]
        cur.close(); conn.close()

        # Withdraw via web
        with client.session_transaction() as sess:
            sess["logged_in"] = True
            sess["portal_mobile"] = mobile
        client.post("/application/%d/withdraw" % app_id)

        resp = client.get("/internal/api/v1/me/status",
                          headers=_citizen_headers(mobile))
        apps = resp.get_json()["data"]["applications"]
        statuses = [a["status"] for a in apps]
        assert "WITHDRAWN" in statuses

    def test_officer_decision_via_api_visible_on_web(self, client):
        """Officer approval via API → visible on web admin view."""
        mobile = "9200005001"
        result = self._submit_via_api(client, mobile)
        app_no = result["application_no"]

        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM applications WHERE application_no=%s", (app_no,))
        app_id = cur.fetchone()[0]
        cur.close(); conn.close()

        # Officer approves via API
        client.post(
            "/internal/api/v1/officer/applications/%s/decision" % app_no,
            headers=_officer_headers(),
            data=json.dumps({"decision": "approved", "note": "eligible", "confirm": True}),
        )

        # Verify on web admin view endpoint
        with client.session_transaction() as sess:
            sess["admin"] = True
            sess["admin_user"] = "admin"
        resp = client.get("/admin/application/%d" % app_id)
        assert resp.status_code == 200
        assert b"APPROVED" in resp.data


# ── Section H: Session end ────────────────────────────────────────────────

class TestSessionEnd:
    def test_session_end_records_revoked_at(self, client):
        mobile = "9300001001"
        resp = client.post("/internal/api/v1/me/session/end",
                           headers=_citizen_headers(mobile))
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

        conn = _db()
        cur = conn.cursor()
        cur.execute(
            "SELECT revoked_at FROM assistant_signouts WHERE subject=%s",
            ("citizen:" + mobile,),
        )
        row = cur.fetchone()
        cur.close(); conn.close()
        assert row is not None
        assert row[0] is not None

    def test_session_state_returns_revoked_at(self, client):
        mobile = "9300002001"
        # End session first
        client.post("/internal/api/v1/me/session/end",
                    headers=_citizen_headers(mobile))

        resp = client.get("/internal/api/v1/me/session-state",
                          headers=_citizen_headers(mobile))
        data = resp.get_json()["data"]
        assert data["revoked_at"] is not None

    def test_session_end_deletes_draft(self, client):
        mobile = "9300003001"
        # Create a draft first
        client.put(
            "/internal/api/v1/me/draft",
            headers=_citizen_headers(mobile),
            data=json.dumps({"applicant_name": "Test"}),
        )
        # End session
        client.post("/internal/api/v1/me/session/end",
                    headers=_citizen_headers(mobile))

        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM assistant_drafts WHERE mobile=%s", (mobile,))
        assert cur.fetchone() is None, "Draft should be deleted after session end"
        cur.close(); conn.close()
