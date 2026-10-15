"""Golden tests for existing portal routes (Section A).

These tests drive the web form flow end-to-end using Flask's test client
against a real Postgres (sewasetu_test, schema only, no seed).

They MUST pass on the UNMODIFIED app.py and MUST continue passing after
the service-layer refactor. Do not change their assertions unless a test
was genuinely wrong.
"""
import psycopg2
import pytest

from .conftest import (
    TEST_DB_URL, _dsn_parts,
    do_apply, do_otp, fill_form, upload_doc, submit_declaration,
)


# ── Helpers ────────────────────────────────────────────────────────────────

def _db():
    return psycopg2.connect(**_dsn_parts(TEST_DB_URL))


def _app_count(mobile):
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM applications WHERE mobile=%s", (mobile,))
    n = cur.fetchone()[0]
    cur.close(); conn.close()
    return n


def _latest_app(mobile):
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, application_no, status FROM applications "
        "WHERE mobile=%s ORDER BY submitted_at DESC LIMIT 1", (mobile,)
    )
    row = cur.fetchone()
    cur.close(); conn.close()
    return row


def _audit_rows(app_id):
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "SELECT action, actor FROM audit_log WHERE application_id=%s ORDER BY at",
        (app_id,),
    )
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


# ── Test: full happy path ──────────────────────────────────────────────────

class TestHappyPath:
    """apply → OTP → form → upload → declaration → submit creates one PENDING row."""

    def test_full_flow(self, client):
        mobile = "9000000001"
        resp = do_apply(client, mobile)
        assert resp.status_code == 302  # redirect to /verify

        resp = do_otp(client, mobile)
        assert resp.status_code == 302  # redirect to /form/1

        fill_form(client)

        resp = upload_doc(client)
        assert resp.status_code == 302  # redirect to /declaration

        resp = submit_declaration(client)
        assert resp.status_code == 200  # confirmation page

        row = _latest_app(mobile)
        assert row is not None, "No application created"
        assert row[2] == "PENDING"
        assert row[1].startswith("SSP"), "Application number has wrong prefix"

    def test_portal_user_created(self, client):
        mobile = "9000000002"
        do_apply(client, mobile)
        do_otp(client, mobile)
        fill_form(client)
        upload_doc(client)
        submit_declaration(client)

        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM portal_users WHERE mobile=%s", (mobile,))
        assert cur.fetchone(), "portal_users row not created"
        cur.close(); conn.close()

    def test_submit_audit_actor_format(self, client):
        """SUBMIT audit actor must be exactly 'applicant:<mobile>'."""
        mobile = "9000000003"
        do_apply(client, mobile)
        do_otp(client, mobile)
        fill_form(client)
        upload_doc(client)
        submit_declaration(client)

        row = _latest_app(mobile)
        audit = _audit_rows(row[0])
        assert audit, "No audit rows"
        assert audit[0][0] == "SUBMIT"
        assert audit[0][1] == "applicant:%s" % mobile


# ── Test: duplicate blocked ────────────────────────────────────────────────

class TestDuplicateBlocked:
    """Second submit for the same mobile is refused."""

    def _full_submit(self, client, mobile):
        do_apply(client, mobile)
        do_otp(client, mobile)
        fill_form(client)
        upload_doc(client)
        submit_declaration(client)

    def test_second_submit_refused(self, client):
        mobile = "9000000010"
        self._full_submit(client, mobile)
        assert _app_count(mobile) == 1

        # Try again — should be blocked
        do_apply(client, mobile)
        do_otp(client, mobile)
        fill_form(client)
        upload_doc(client)
        resp = submit_declaration(client)

        # The route flashes an error and redirects to index
        assert resp.status_code in (302, 200)
        assert _app_count(mobile) == 1, "Duplicate application was created"


# ── Test: withdraw then re-apply ───────────────────────────────────────────

class TestWithdrawReapply:
    """Withdraw PENDING, then submit a fresh application."""

    def _full_submit(self, client, mobile):
        do_apply(client, mobile)
        do_otp(client, mobile)
        fill_form(client)
        upload_doc(client)
        submit_declaration(client)
        return _latest_app(mobile)

    def test_withdraw_then_reapply(self, client):
        mobile = "9000000020"
        row = self._full_submit(client, mobile)
        app_id = row[0]
        assert row[2] == "PENDING"

        # Log in to status portal
        with client.session_transaction() as sess:
            sess["logged_in"] = True
            sess["portal_mobile"] = mobile

        resp = client.post(
            "/application/%d/withdraw" % app_id, follow_redirects=False
        )
        assert resp.status_code == 302

        # Status should now be WITHDRAWN
        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT status FROM applications WHERE id=%s", (app_id,))
        status = cur.fetchone()[0]
        cur.close(); conn.close()
        assert status == "WITHDRAWN"

        # Re-apply
        row2 = self._full_submit(client, mobile)
        assert row2[2] == "PENDING"
        assert row2[0] != app_id

    def test_withdraw_audit_actor(self, client):
        mobile = "9000000021"
        row = self._full_submit(client, mobile)
        app_id = row[0]

        with client.session_transaction() as sess:
            sess["logged_in"] = True
            sess["portal_mobile"] = mobile

        client.post("/application/%d/withdraw" % app_id, follow_redirects=False)

        audit = _audit_rows(app_id)
        actions = [a[0] for a in audit]
        actors = {a[0]: a[1] for a in audit}
        assert "WITHDRAW" in actions
        assert actors["WITHDRAW"] == "applicant:%s" % mobile


# ── Test: admin approve / reject only act on PENDING ──────────────────────

class TestAdminDecisions:

    def _full_submit(self, client, mobile):
        do_apply(client, mobile)
        do_otp(client, mobile)
        fill_form(client)
        upload_doc(client)
        submit_declaration(client)
        return _latest_app(mobile)

    def _admin_session(self, client):
        with client.session_transaction() as sess:
            sess["logged_in"] = True
            sess["admin"] = True
            sess["admin_user"] = "admin"

    def test_admin_approve_pending(self, client):
        mobile = "9000000030"
        row = self._full_submit(client, mobile)
        app_id = row[0]
        self._admin_session(client)

        resp = client.post(
            "/admin/approve/%d" % app_id,
            data={"note": "all good"},
            follow_redirects=False,
        )
        assert resp.status_code == 302

        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT status, decided_by FROM applications WHERE id=%s", (app_id,))
        status, decided_by = cur.fetchone()
        cur.close(); conn.close()
        assert status == "APPROVED"
        assert decided_by == "admin"

    def test_admin_reject_pending(self, client):
        mobile = "9000000031"
        row = self._full_submit(client, mobile)
        app_id = row[0]
        self._admin_session(client)

        client.post(
            "/admin/reject/%d" % app_id,
            data={"note": "ineligible"},
            follow_redirects=False,
        )
        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT status FROM applications WHERE id=%s", (app_id,))
        assert cur.fetchone()[0] == "REJECTED"
        cur.close(); conn.close()

    def test_admin_cannot_approve_already_approved(self, client):
        mobile = "9000000032"
        row = self._full_submit(client, mobile)
        app_id = row[0]
        self._admin_session(client)

        # Approve once
        client.post("/admin/approve/%d" % app_id, data={"note": ""})

        # Try to reject now-approved application — should be a no-op
        client.post("/admin/reject/%d" % app_id, data={"note": "late reject"})
        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT status FROM applications WHERE id=%s", (app_id,))
        assert cur.fetchone()[0] == "APPROVED", "Approved application was overwritten"
        cur.close(); conn.close()

    def test_approve_audit_actor_format(self, client):
        """Approve audit actor must be 'admin:<username>'."""
        mobile = "9000000033"
        row = self._full_submit(client, mobile)
        app_id = row[0]
        self._admin_session(client)

        client.post("/admin/approve/%d" % app_id, data={"note": "ok"})
        audit = _audit_rows(app_id)
        by_action = {a[0]: a[1] for a in audit}
        assert "APPROVED" in by_action
        assert by_action["APPROVED"] == "admin:admin"

    def test_reject_audit_actor_format(self, client):
        mobile = "9000000034"
        row = self._full_submit(client, mobile)
        app_id = row[0]
        self._admin_session(client)

        client.post("/admin/reject/%d" % app_id, data={"note": "missing doc"})
        audit = _audit_rows(app_id)
        by_action = {a[0]: a[1] for a in audit}
        assert "REJECTED" in by_action
        assert by_action["REJECTED"] == "admin:admin"

    def test_decide_decided_by_is_username(self, client):
        """decided_by column must hold the username, not 'APPLICANT' or other."""
        mobile = "9000000035"
        row = self._full_submit(client, mobile)
        app_id = row[0]
        self._admin_session(client)
        client.post("/admin/approve/%d" % app_id, data={"note": ""})

        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT decided_by FROM applications WHERE id=%s", (app_id,))
        assert cur.fetchone()[0] == "admin"
        cur.close(); conn.close()
