#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/officer_decision_flow.py — End-to-end officer decision workflow on live VM.

Exercises the full officer assistant workflow via the officer MCP server:
  1. OAuth 2.1 PKCE + DCR authorization for officer:admin
  2. Diagnostic whoami verification
  3. queue_overview inspection
  4. list_applications (oldest pending)
  5. Inspect & verify SSP26990060 -> decide APPROVE
  6. Verify SSP26990061 -> decide APPROVE
  7. Verify SSP26990067 (bad IFSC) -> decide REJECT with clear citizen-readable reason
  8. Test idempotency on SSP26990060
  9. my_recent_decisions audit check
 10. Print resulting applications and audit_log rows verbatim from PostgreSQL.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import psycopg2

# Add repository root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.oauth_smoke import (
    obtain_token,
    call_tool,
)


def run_officer_flow(
    host: str,
    admin_user: str,
    admin_pass: str,
    db_url: str,
) -> bool:
    host = host.rstrip("/")
    officer_mcp = f"{host}/officer/mcp"

    print("=" * 70)
    print("  OFFICER MCP SERVER DECISION FLOW")
    print(f"  Target: {officer_mcp}")
    print("=" * 70)

    # 1. OAuth login for officer
    print("\n[Step 1] Authenticating officer via OAuth 2.1 PKCE + DCR...")
    tok = obtain_token(
        portal_base=host,
        canonical_url=officer_mcp,
        role="officer",
        admin_username=admin_user,
        admin_password=admin_pass,
    )
    access_token = tok.access_token
    print(f"  ✓ Obtained access token (expires in {tok.expires_in}s)")

    def _call(name: str, args: dict | None = None) -> dict:
        raw = call_tool(officer_mcp, access_token, name, args or {})
        # Streamable HTTP MCP returns content array with JSON text
        content = raw.get("result", {}).get("content", [])
        if content and content[0].get("type") == "text":
            try:
                return json.loads(content[0]["text"])
            except Exception:
                return {"text": content[0]["text"]}
        return raw

    # 2. whoami
    print("\n[Step 2] Calling whoami...")
    who = _call("whoami")
    print(f"  whoami response: {who}")

    # 3. queue_overview
    print("\n[Step 3] Calling queue_overview...")
    ov = _call("queue_overview")
    print(f"  queue_overview: ok={ov.get('ok')}, message='{ov.get('message_for_user')}'")
    print(f"  Data: {json.dumps(ov.get('data', {}), indent=2)}")

    # 4. list_applications
    print("\n[Step 4] Calling list_applications(status='PENDING', sort='oldest', limit=5)...")
    app_list = _call("list_applications", {"status": "PENDING", "sort": "oldest", "limit": 5})
    apps = app_list.get("data", {}).get("applications", [])
    print(f"  Found {len(apps)} pending applications. Next cursor: {app_list.get('data', {}).get('next_cursor')}")
    for a in apps:
        print(f"   - AppNo: {a['application_no']}, Name: {a['applicant_name']}, Block: {a['block']}, DaysPending: {a['days_pending']}, Flags: {a['flags']}")

    # 5. Review & Approve SSP26990060 (clean pending)
    print("\n[Step 5] Adjudicating SSP26990060 (Clean application)...")
    get_res = _call("get_application", {"application_no": "SSP26990060"})
    app_detail = get_res.get("data", {}).get("application", {})
    print(f"  get_application: Applicant={app_detail.get('applicant_name')}, BankAcct={app_detail.get('bank_account')}, IFSC={app_detail.get('ifsc')}")
    
    ver_res = _call("verify_application", {"application_no": "SSP26990060"})
    print(f"  verify_application: flags={ver_res.get('data', {}).get('flags')}, recommendation={ver_res.get('data', {}).get('recommendation')}")

    dec_res1 = _call(
        "decide_application",
        {
            "application_no": "SSP26990060",
            "decision": "APPROVED",
            "note": "Age proof and bank details verified successfully. Applicant meets all statutory criteria.",
            "confirm": True,
        },
    )
    print(f"  decide_application result: ok={dec_res1.get('ok')}, message='{dec_res1.get('message_for_user')}'")

    # 6. Review & Approve SSP26990061 (clean pending)
    print("\n[Step 6] Adjudicating SSP26990061 (Clean application)...")
    ver_res2 = _call("verify_application", {"application_no": "SSP26990061"})
    print(f"  verify_application: flags={ver_res2.get('data', {}).get('flags')}, recommendation={ver_res2.get('data', {}).get('recommendation')}")

    dec_res2 = _call(
        "decide_application",
        {
            "application_no": "SSP26990061",
            "decision": "APPROVED",
            "note": "Voter ID age proof verified and bank account confirmed.",
            "confirm": True,
        },
    )
    print(f"  decide_application result: ok={dec_res2.get('ok')}, message='{dec_res2.get('message_for_user')}'")

    # 7. Review & Reject SSP26990067 (bad IFSC)
    print("\n[Step 7] Adjudicating SSP26990067 (Bad IFSC)...")
    ver_res3 = _call("verify_application", {"application_no": "SSP26990067"})
    print(f"  verify_application: flags={ver_res3.get('data', {}).get('flags')}, note='{ver_res3.get('message_for_user')}'")

    dec_res3 = _call(
        "decide_application",
        {
            "application_no": "SSP26990067",
            "decision": "REJECTED",
            "note": "The provided bank IFSC code UTBI0PUR2819 is invalid (must be 11 characters). Please obtain the correct IFSC from your bank branch and reapply.",
            "confirm": True,
        },
    )
    print(f"  decide_application result: ok={dec_res3.get('ok')}, message='{dec_res3.get('message_for_user')}'")

    # 8. Test Idempotency on SSP26990060
    print("\n[Step 8] Testing idempotency by deciding SSP26990060 again...")
    dec_again = _call(
        "decide_application",
        {
            "application_no": "SSP26990060",
            "decision": "APPROVED",
            "note": "Attempting second approval",
            "confirm": True,
        },
    )
    print(f"  decide_application (duplicate) result: ok={dec_again.get('ok')}, message='{dec_again.get('message_for_user')}'")

    # 9. my_recent_decisions
    print("\n[Step 9] Calling my_recent_decisions...")
    my_dec = _call("my_recent_decisions", {"limit": 5})
    decisions = my_dec.get("data", {}).get("decisions", [])
    print(f"  Recorded {len(decisions)} recent decisions for {tok.client_id}:")
    for d in decisions:
        print(f"   - AppNo: {d['application_no']}, Decision: {d['decision']}, At: {d['at']}, Note: '{d['note']}'")

    # 10. Print DB rows verbatim
    print("\n" + "=" * 70)
    print("  DATABASE VERBATIM ROWS: APPLICATIONS & AUDIT_LOG")
    print("=" * 70)
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    print("\n--- APPLICATIONS (Marker Mobiles 9990000060 - 9990000072) ---")
    cur.execute(
        """
        SELECT id, application_no, applicant_name, mobile, status, decided_at, decided_by
        FROM applications
        WHERE mobile >= '9990000060' AND mobile <= '9990000072'
        ORDER BY application_no ASC
        """
    )
    app_rows = cur.fetchall()
    app_headers = ["id", "application_no", "applicant_name", "mobile", "status", "decided_at", "decided_by"]
    print(f"{' | '.join(app_headers)}")
    print("-" * 90)
    for row in app_rows:
        print(f"{row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} | {row[5]} | {row[6]}")

    print("\n--- AUDIT_LOG (Decisions & Submissions for Marker Mobiles) ---")
    cur.execute(
        """
        SELECT l.id, l.application_id, a.application_no, l.action, l.actor, l.note, l.at
        FROM audit_log l
        JOIN applications a ON l.application_id = a.id
        WHERE a.mobile >= '9990000060' AND a.mobile <= '9990000072'
        ORDER BY l.at ASC, l.id ASC
        """
    )
    audit_rows = cur.fetchall()
    audit_headers = ["id", "app_id", "application_no", "action", "actor", "note", "at"]
    print(f"{' | '.join(audit_headers)}")
    print("-" * 110)
    for row in audit_rows:
        print(f"{row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} | {row[5]} | {row[6]}")

    cur.close()
    conn.close()

    print("\n" + "=" * 70)
    print("  OFFICER DECISION FLOW COMPLETE")
    print("=" * 70)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Run officer decision flow.")
    parser.add_argument(
        "--host",
        default=os.environ.get("PUBLIC_BASE_URL", "https://sewasetu-hv.koreacentral.cloudapp.azure.com"),
        help="Public URL of portal",
    )
    parser.add_argument("--admin-user", default=os.environ.get("ADMIN_USERNAME", "admin"))
    parser.add_argument("--admin-pass", default=os.environ.get("ADMIN_PASSWORD", "admin123"))
    parser.add_argument(
        "--db-url",
        default=os.environ.get(
            "DATABASE_URL", "postgresql://sewasetu:sewasetu123@localhost:5432/sewasetu"
        ),
    )
    args = parser.parse_args()

    success = run_officer_flow(
        host=args.host,
        admin_user=args.admin_user,
        admin_pass=args.admin_pass,
        db_url=args.db_url,
    )
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
