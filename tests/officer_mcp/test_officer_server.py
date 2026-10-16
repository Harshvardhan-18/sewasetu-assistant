# -*- coding: utf-8 -*-
"""Comprehensive tests for the Officer MCP Server (Prompts 8 & 9)."""

import os
import json
import time
import pytest
import psycopg2
from starlette.testclient import TestClient

import officer_mcp.server as officer_server
import citizen_mcp.server as citizen_server
from tests.officer_mcp.conftest import make_token
from tests.portal.conftest import TEST_DB_URL, _dsn_parts
from scripts.seed_officer_fixtures import seed_fixtures
from mcp.server.auth.middleware.auth_context import auth_context_var, AuthenticatedUser
from mcp.server.auth.provider import AccessToken


def _set_officer_context(username: str = "admin"):
    token = AccessToken(
        token="test-officer-token",
        client_id="test-client",
        scopes=["officer"],
        expires_at=int(time.time()) + 300,
        resource="http://localhost:8000/officer/mcp",
    )
    token.subject = f"officer:{username}"
    token.claims = {"sub": f"officer:{username}", "role": "officer"}
    auth_context_var.set(AuthenticatedUser(token))


def _set_citizen_context(mobile: str = "9990000067"):
    token = AccessToken(
        token="test-citizen-token",
        client_id="test-client",
        scopes=["citizen"],
        expires_at=int(time.time()) + 300,
        resource="http://localhost:8000/citizen/mcp",
    )
    token.subject = f"citizen:{mobile}"
    token.claims = {"sub": f"citizen:{mobile}", "role": "citizen"}
    auth_context_var.set(AuthenticatedUser(token))


# ── 1. Tool List Isolation, Descriptions & Instructions ───────────────────────

def test_officer_tools_isolation_and_descriptions():
    tools = officer_server.app.mcp_server._tool_manager._tools
    tool_names = set(tools.keys())

    expected_tools = {
        "whoami",
        "queue_overview",
        "list_applications",
        "search_applications",
        "get_application",
        "verify_application",
        "decide_application",
        "my_recent_decisions",
    }
    assert tool_names == expected_tools, f"Tool mismatch: {tool_names ^ expected_tools}"

    # Citizen tools must NOT be present on officer server
    citizen_tools = {
        "get_scheme_info",
        "check_eligibility",
        "submit_application",
        "withdraw_application",
        "save_application_draft",
        "attach_age_proof",
        "get_upload_link",
    }
    assert not tool_names.intersection(citizen_tools), "Citizen tools found on officer server!"

    # Instructions checks
    instructions = officer_server.OFFICER_INSTRUCTIONS
    assert "02:00 UTC (07:30 IST)" in instructions
    assert "verify_application" in instructions
    assert "acknowledged_flags" in instructions
    assert "untrusted data" in instructions
    assert "one case at a time" in instructions

    # 5-part description structure check for every tool
    for name, tool in tools.items():
        desc = tool.description or ""
        assert "\n" in desc, f"Tool {name} description is not multi-line"
        assert "Use when:" in desc, f"Tool {name} missing 'Use when:'"
        assert "Do not use when:" in desc, f"Tool {name} missing 'Do not use when:'"
        assert "Side effects:" in desc, f"Tool {name} missing 'Side effects:'"
        assert "Ask the person first:" in desc, f"Tool {name} missing 'Ask the person first:'"

    # decide_application must say it changes official portal data and needs clear yes
    decide_desc = tools["decide_application"].description
    assert "changes official portal data" in decide_desc.lower()
    assert "clear 'yes'" in decide_desc.lower()


# ── 2. Scope Isolation & Handshake Styles ───────────────────────────────────

def test_scope_isolation_and_both_handshakes(server_keys):
    priv, pub, jwk = server_keys
    officer_tok = make_token(priv, sub="officer:admin", role="officer")
    citizen_tok = make_token(
        priv,
        sub="citizen:9990000001",
        role="citizen",
        aud="http://localhost:8000/citizen/mcp",
    )

    with TestClient(officer_server.app) as off_client:
        # 1. Citizen token sent to /officer/mcp is rejected (401 if wrong aud, 403 if right aud but citizen scope)
        resp_cit_on_off = off_client.post(
            "/officer/mcp",
            headers={"Authorization": f"Bearer {citizen_tok}", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert resp_cit_on_off.status_code in (401, 403)

        citizen_tok_for_officer_aud = make_token(
            priv,
            sub="citizen:9990000001",
            role="citizen",
            aud="http://localhost:8000/officer/mcp",
        )
        resp_cit_scope = off_client.post(
            "/officer/mcp",
            headers={"Authorization": f"Bearer {citizen_tok_for_officer_aud}", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert resp_cit_scope.status_code in (401, 403)

        # 2. Modern handshake (2026-07-28 with _meta envelope) on /officer/mcp
        modern_headers = {
            "Authorization": f"Bearer {officer_tok}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        modern_init = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "initialize",
            "params": {
                "protocolVersion": "2026-07-28",
                "capabilities": {},
                "clientInfo": {"name": "test-modern-client", "version": "1.0"},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        }
        resp_modern = off_client.post("/officer/mcp", headers=modern_headers, json=modern_init)
        assert resp_modern.status_code == 200

        # 4. Legacy handshake (2025-11-25 with MCP-Protocol-Version header) on /officer/mcp
        legacy_headers = dict(modern_headers)
        legacy_headers["MCP-Protocol-Version"] = "2025-11-25"
        legacy_init = {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test-legacy-client", "version": "1.0"},
            },
        }
        resp_legacy = off_client.post("/officer/mcp", headers=legacy_headers, json=legacy_init)
        assert resp_legacy.status_code == 200


# ── 3. Diagnostic Tool whoami ────────────────────────────────────────────────

def test_officer_whoami():
    _set_officer_context("admin")
    msg = officer_server.whoami()
    assert "officer:admin" in msg


# ── 4. Queue Overview & Block Filter ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_queue_overview_and_block_filter(tmp_path):
    _set_officer_context("admin")
    upload_dir = str(tmp_path / "uploads")
    seed_fixtures(TEST_DB_URL, upload_dir)

    res = await officer_server.queue_overview()
    assert res["ok"] is True
    data = res["data"]
    assert "pending_count" in data
    assert data["pending_count"] >= 10
    assert "oldest_pending" in data
    assert "by_block" in data
    assert "deemed_approvals_total" in data

    # Test optional block filter
    res_block = await officer_server.queue_overview(block="Sonari")
    assert res_block["ok"] is True
    assert res_block["data"]["block_filter"] == "Sonari"


# ── 5. List Applications, Pagination & Masking ───────────────────────────────

@pytest.mark.asyncio
async def test_list_applications_pagination_and_masking(tmp_path):
    _set_officer_context("admin")
    upload_dir = str(tmp_path / "uploads")
    seed_fixtures(TEST_DB_URL, upload_dir)

    # First page: limit 4
    page1 = await officer_server.list_applications(status="PENDING", limit=4)
    assert page1["ok"] is True
    apps1 = page1["data"]["applications"]
    assert len(apps1) == 4
    assert page1["data"]["next_cursor"] is not None

    # Verify bank account is not in the list view (or masked)
    for app in apps1:
        assert "bank_account" not in app

    # Next page
    cursor = page1["data"]["next_cursor"]
    page2 = await officer_server.list_applications(status="PENDING", limit=4, cursor=cursor)
    assert page2["ok"] is True
    apps2 = page2["data"]["applications"]
    assert len(apps2) > 0
    # No duplicate items between page 1 and page 2
    ids1 = {a["id"] for a in apps1}
    ids2 = {a["id"] for a in apps2}
    assert not ids1.intersection(ids2)


# ── 6. Search Applications ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_applications(tmp_path):
    _set_officer_context("admin")
    upload_dir = str(tmp_path / "uploads")
    seed_fixtures(TEST_DB_URL, upload_dir)

    # Search by app_no
    s1 = await officer_server.search_applications(query="SSP26990060")
    assert s1["ok"] is True
    assert len(s1["data"]["applications"]) == 1
    assert s1["data"]["applications"][0]["application_no"] == "SSP26990060"

    # Search by mobile
    s2 = await officer_server.search_applications(query="9990000061")
    assert s2["ok"] is True
    assert len(s2["data"]["applications"]) >= 1

    # Search by applicant name
    s3 = await officer_server.search_applications(query="Ramesh")
    assert s3["ok"] is True
    assert any("Ramesh" in a["applicant_name"] for a in s3["data"]["applications"])


# ── 7. Get Application: Unmasked Account & Audit Trail ────────────────────────

@pytest.mark.asyncio
async def test_get_application_unmasked_account_and_audit(tmp_path):
    _set_officer_context("admin")
    upload_dir = str(tmp_path / "uploads")
    seed_fixtures(TEST_DB_URL, upload_dir)

    res = await officer_server.get_application("SSP26990060")
    assert res["ok"] is True
    app = res["data"]["application"]
    # Bank account is unmasked for case review
    assert app["bank_account"] == "12345678901"
    # Audit trail present
    assert "audit" in app
    assert len(app["audit"]) >= 1
    assert app["audit"][0]["action"] == "SUBMIT"


# ── 8. Verify Application Flags ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_application_flags(tmp_path):
    _set_officer_context("admin")
    upload_dir = str(tmp_path / "uploads")
    seed_fixtures(TEST_DB_URL, upload_dir)

    # 1. Clean pending application: no flags, recommendation APPROVE
    v_clean = await officer_server.verify_application("SSP26990060")
    assert v_clean["ok"] is True
    assert v_clean["data"]["flags"] == []
    assert v_clean["data"]["recommendation"] == "APPROVE"

    # 2. Age 59 at submission: flagged age_at_submission
    v_age = await officer_server.verify_application("SSP26990066")
    assert v_age["ok"] is True
    assert "age_at_submission" in v_age["data"]["flags"]
    assert v_age["data"]["recommendation"] == "REVIEW"

    # 3. Bad IFSC: flagged ifsc_format
    v_ifsc = await officer_server.verify_application("SSP26990067")
    assert v_ifsc["ok"] is True
    assert "ifsc_format" in v_ifsc["data"]["flags"]

    # 4. Same bank account on different mobile: flagged same_account_elsewhere
    v_acct = await officer_server.verify_application("SSP26990068")
    assert v_acct["ok"] is True
    assert "same_account_elsewhere" in v_acct["data"]["flags"]

    # 5. Same name + DOB on different mobile: flagged same_name_dob_elsewhere
    v_name_dob = await officer_server.verify_application("SSP26990072")
    assert v_name_dob["ok"] is True
    assert "same_name_dob_elsewhere" in v_name_dob["data"]["flags"]


# ── 9. Test other_active_application Flag (Dropped Index in Test DB) ─────────

@pytest.mark.asyncio
async def test_verify_other_active_application_dropped_index(tmp_path):
    """
    Requirement 1 from Prompt 9:
    "Do NOT alter the one_active_per_mobile index. No marker-mobile carve-out in the production schema.
     Test the 'other active application on same mobile' flag only in the test database (sewasetu_test)
     with the index dropped inside the test."
    """
    _set_officer_context("admin")
    upload_dir = str(tmp_path / "uploads")
    seed_fixtures(TEST_DB_URL, upload_dir)

    parts = _dsn_parts(TEST_DB_URL)
    conn = psycopg2.connect(**parts)
    try:
        cur = conn.cursor()
        # Drop index inside test DB
        cur.execute("DROP INDEX IF EXISTS one_active_per_mobile")
        conn.commit()

        # Insert a second active PENDING application on 9990000060
        cur.execute(
            """
            INSERT INTO applications (
                application_no, applicant_name, mobile, dob, gender,
                marital_status, village, block, bank_account, ifsc,
                doc_path, status, submitted_at
            ) VALUES (
                'SSP26990060DUPE', 'Bipin Das Duplicate', '9990000060', '1960-01-01', 'MALE',
                'MARRIED', 'Borbari', 'Purvanchal-Central', '99999999999', 'SBIN0000001',
                '/tmp/dummy.pdf', 'PENDING', now()
            ) RETURNING id
            """
        )
        dupe_id = cur.fetchone()[0]
        conn.commit()

        # Run verify_application on original SSP26990060
        v_dupe = await officer_server.verify_application("SSP26990060")
        assert v_dupe["ok"] is True
        assert "other_active_application" in v_dupe["data"]["flags"]

        # Cleanup duplicate row and recreate index
        cur.execute("DELETE FROM applications WHERE id = %s", (dupe_id,))
        conn.commit()
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_mobile
                ON applications(mobile)
                WHERE status IN ('PENDING','APPROVED','DEEMED_APPROVED')
            """
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


# ── 10. Decide Application: Validations & Flag Refusal ───────────────────────

@pytest.mark.asyncio
async def test_decide_application_validations(tmp_path):
    _set_officer_context("admin")
    upload_dir = str(tmp_path / "uploads")
    seed_fixtures(TEST_DB_URL, upload_dir)

    # 1. Refuse if confirm=False
    r1 = await officer_server.decide_application(
        application_no="SSP26990060",
        decision="APPROVED",
        note="Approved",
        confirm=False,
    )
    assert r1["ok"] is False
    assert any("confirm" in str(p).lower() for p in r1["problems"])

    # 2. Refuse if note is empty
    r2 = await officer_server.decide_application(
        application_no="SSP26990060",
        decision="APPROVED",
        note="",
        confirm=True,
    )
    assert r2["ok"] is False
    assert any("note" in str(p).lower() for p in r2["problems"])

    # 3. Flagged application (SSP26990066 has age_at_submission flag):
    # Approval refused unless acknowledged_flags contains 'age_at_submission'
    r3 = await officer_server.decide_application(
        application_no="SSP26990066",
        decision="APPROVED",
        note="Approved anyway without ack",
        confirm=True,
    )
    assert r3["ok"] is False
    assert any("acknowledged_flags" in str(p).lower() or "missing" in str(p).lower() for p in r3["problems"])

    # Partial acknowledged_flags also fails
    r4 = await officer_server.decide_application(
        application_no="SSP26990066",
        decision="APPROVED",
        note="Approved anyway with wrong ack",
        confirm=True,
        acknowledged_flags=["other_flag"],
    )
    assert r4["ok"] is False


# ── 11. Decide Application: Happy Paths & Idempotency ────────────────────────

@pytest.mark.asyncio
async def test_decide_application_happy_paths_and_idempotency(tmp_path):
    _set_officer_context("admin")
    upload_dir = str(tmp_path / "uploads")
    seed_fixtures(TEST_DB_URL, upload_dir)

    # 1. Approve clean application
    res_appr = await officer_server.decide_application(
        application_no="SSP26990060",
        decision="APPROVED",
        note="All documents verified and applicant meets age requirement.",
        confirm=True,
    )
    assert res_appr["ok"] is True
    assert res_appr["data"]["decision"] == "APPROVED"

    # Verify DB state & audit log
    parts = _dsn_parts(TEST_DB_URL)
    conn = psycopg2.connect(**parts)
    cur = conn.cursor()
    cur.execute("SELECT status, decided_by FROM applications WHERE application_no='SSP26990060'")
    st, dec_by = cur.fetchone()
    assert st == "APPROVED"
    assert dec_by == "admin"

    cur.execute(
        "SELECT action, actor, note FROM audit_log WHERE application_id IN "
        "(SELECT id FROM applications WHERE application_no='SSP26990060') "
        "ORDER BY at DESC LIMIT 1"
    )
    act, actor, audit_note = cur.fetchone()
    assert act == "APPROVED"
    assert actor == "admin:admin"
    assert audit_note.startswith("via officer assistant: ")
    cur.close(); conn.close()

    # 2. Idempotency: deciding again returns "already APPROVED by ..." and changes nothing
    res_again = await officer_server.decide_application(
        application_no="SSP26990060",
        decision="APPROVED",
        note="Trying to approve again",
        confirm=True,
    )
    assert res_again["ok"] is False
    assert "already approved" in res_again["message_for_user"].lower()

    # 3. Approve flagged application with acknowledged_flags
    res_flag_appr = await officer_server.decide_application(
        application_no="SSP26990068",
        decision="APPROVED",
        note="Joint bank account verified with spouse.",
        confirm=True,
        acknowledged_flags=["same_account_elsewhere"],
    )
    assert res_flag_appr["ok"] is True

    # 4. Reject application (SSP26990067 with bad IFSC) needs ONLY note and confirm=True
    res_rej = await officer_server.decide_application(
        application_no="SSP26990067",
        decision="REJECTED",
        note="The bank IFSC code UTBI0PUR2819 is invalid. Please visit your branch or resubmit with the correct IFSC.",
        confirm=True,
    )
    assert res_rej["ok"] is True
    assert res_rej["data"]["decision"] == "REJECTED"


# ── 12. Cross-Channel: Citizen Sees Clean Rejection Reason ───────────────────

@pytest.mark.asyncio
async def test_cross_channel_citizen_sees_clean_reason(tmp_path):
    """
    Requirement 4 from Prompt 9:
    "Keep 'via officer assistant: ' in the audit note, but the reason shown to the citizen by
     get_my_status (citizen MCP) must be the officer's wording without the prefix. Add a test."
    """
    _set_officer_context("admin")
    upload_dir = str(tmp_path / "uploads")
    seed_fixtures(TEST_DB_URL, upload_dir)

    officer_wording = "Bank IFSC code is invalid. Please update with your new IFSC."
    res_rej = await officer_server.decide_application(
        application_no="SSP26990067",
        decision="REJECTED",
        note=officer_wording,
        confirm=True,
    )
    assert res_rej["ok"] is True

    # Switch to citizen caller context for 9990000067
    _set_citizen_context("9990000067")
    status_res = await citizen_server.get_my_status()
    assert status_res["ok"] is True
    apps = status_res["data"]["applications"]
    assert len(apps) >= 1
    rejected_app = apps[0]
    assert rejected_app["status"] == "REJECTED"
    # Ensure reason is exactly the officer wording WITHOUT 'via officer assistant: '
    assert rejected_app.get("decision_reason") == officer_wording
    assert "via officer assistant:" not in (rejected_app.get("decision_reason") or "")
    assert status_res["data"]["reason"] == officer_wording


# ── 13. Prompt-Injection Safety in Applicant Data ────────────────────────────

@pytest.mark.asyncio
async def test_prompt_injection_safety_in_applicant_data(tmp_path):
    _set_officer_context("admin")
    upload_dir = str(tmp_path / "uploads")
    seed_fixtures(TEST_DB_URL, upload_dir)

    # Retrieve application with prompt injection text in village
    res = await officer_server.get_application("SSP26990069")
    assert res["ok"] is True
    app = res["data"]["application"]
    # Returned purely as string data in the dictionary
    assert "Ignore previous instructions" in app["village"]

    # Verify application remains PENDING unless explicitly decided with application_no
    parts = _dsn_parts(TEST_DB_URL)
    conn = psycopg2.connect(**parts)
    cur = conn.cursor()
    cur.execute("SELECT status FROM applications WHERE application_no='SSP26990069'")
    st = cur.fetchone()[0]
    assert st == "PENDING"
    cur.close(); conn.close()


# ── 14. My Recent Decisions Tool ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_my_recent_decisions(tmp_path):
    _set_officer_context("admin")
    upload_dir = str(tmp_path / "uploads")
    seed_fixtures(TEST_DB_URL, upload_dir)

    # Decide one application
    await officer_server.decide_application(
        application_no="SSP26990060",
        decision="APPROVED",
        note="Approved clean application.",
        confirm=True,
    )

    rec = await officer_server.my_recent_decisions(limit=10)
    assert rec["ok"] is True
    decisions = rec["data"]["decisions"]
    assert len(decisions) >= 1
    assert any(d["application_no"] == "SSP26990060" for d in decisions)
