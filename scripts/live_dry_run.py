#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/live_dry_run.py — Live dry-run executing 1 citizen submission and 1 officer decision,
printing DB records, running reset_marker_data, and asserting clean DB counts.
"""

import base64
import json
import os
import sys
import psycopg2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.oauth_client import OAuthClient

HOST = os.environ.get("HOST", "https://sewasetu-hv.koreacentral.cloudapp.azure.com").rstrip("/")
SMSGW = os.environ.get("SMSGW_URL", f"{HOST}/__gateway")
ADMIN_USER = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_PASS:
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("ADMIN_PASSWORD="):
                    ADMIN_PASS = line.split("=", 1)[1].strip().strip("'\"")
DB_URL = os.environ.get("DATABASE_URL", "postgresql://sewasetu:sewasetu123@127.0.0.1:5432/sewasetu")
TEST_MOBILE = "9990000088"

def main():
    print("=" * 60)
    print("LIVE DRY RUN: 1 Citizen Application + 1 Officer Decision")
    print(f"Target Host: {HOST}")
    print(f"Marker Mobile: {TEST_MOBILE}")
    print("=" * 60)

    client = OAuthClient(
        portal_base=HOST,
        canonical_url=f"{HOST}/citizen/mcp",
        smsgw_base=SMSGW,
    )

    # 1. Citizen Flow
    print("\n--- Step 1: Citizen Authorization & Submission ---")
    c_tok = client.obtain_citizen_token(TEST_MOBILE)
    print(f"Obtained citizen token for {TEST_MOBILE}")

    # Draft
    draft_args = {
        "applicant_name": "Biren Phukan",
        "dob": "01/01/1955",
        "gender": "male",
        "village": "Namrup",
        "block": "Sonari",
        "bank_account": "123456789012",
        "ifsc": "SBIN0001234",
    }
    r = client.call_tool(c_tok.access_token, "save_application_draft", draft_args, mcp_url=f"{HOST}/citizen/mcp")
    res1 = json.loads(r["result"]["content"][0]["text"])
    print("save_application_draft result:", res1.get("message_for_user", res1))

    # Attach proof
    dummy_pdf = base64.b64encode(b"%PDF-1.4 dummy age proof content for dry run").decode("utf-8")
    proof_args = {
        "document_type": "voter_id",
        "filename": "voter_card.pdf",
        "file_base64": dummy_pdf,
    }
    r = client.call_tool(c_tok.access_token, "attach_age_proof", proof_args, mcp_url=f"{HOST}/citizen/mcp")
    res2 = json.loads(r["result"]["content"][0]["text"])
    print("attach_age_proof result:", res2.get("message_for_user", res2))

    # Preview application
    r = client.call_tool(c_tok.access_token, "preview_application", {}, mcp_url=f"{HOST}/citizen/mcp")
    res3 = json.loads(r["result"]["content"][0]["text"])
    print("preview_application result:", res3.get("message_for_user", res3))
    conf_code = res3.get("data", {}).get("confirmation_code")
    print(f"Obtained confirmation code: {conf_code}")

    # Submit
    submit_args = {
        "confirmation_code": conf_code,
        "declaration_accepted": True,
    }
    r = client.call_tool(c_tok.access_token, "submit_application", submit_args, mcp_url=f"{HOST}/citizen/mcp")
    sub_data = json.loads(r["result"]["content"][0]["text"])
    app_no = sub_data.get("data", {}).get("application_no") or sub_data.get("data", {}).get("application_number")
    app_id = sub_data.get("data", {}).get("id")
    print(f"Application submitted successfully! Application Number: {app_no} (ID: {app_id})")
    if not app_no:
        print("ERROR: No application number returned:", sub_data)
        sys.exit(1)

    # 2. Officer Flow
    print("\n--- Step 2: Officer Decision ---")
    o_tok = client.obtain_officer_token(ADMIN_USER, ADMIN_PASS, canonical_url=f"{HOST}/officer/mcp")
    print(f"Obtained officer token for {ADMIN_USER}")

    decide_args = {
        "application_no": app_no,
        "decision": "approved",
        "note": "Dry-run verified: age 71 with valid document and bank details",
        "confirm": True,
    }
    r = client.call_tool(o_tok.access_token, "decide_application", decide_args, mcp_url=f"{HOST}/officer/mcp")
    dec_data = json.loads(r["result"]["content"][0]["text"])
    print("decide_application result:", dec_data.get("message_for_user", dec_data))

    # 3. Print DB Rows
    print("\n--- Step 3: Query Database Rows Before Reset ---")
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute("SELECT id, application_no, applicant_name, mobile, dob, status, submitted_at, decided_at, decided_by FROM applications WHERE application_no = %s;", (app_no,))
    app_row = cur.fetchone()
    print("Application Record in DB:")
    print(f"  ID:           {app_row[0]}")
    print(f"  App No:       {app_row[1]}")
    print(f"  Name:         {app_row[2]}")
    print(f"  Mobile:       {app_row[3]}")
    print(f"  DOB:          {app_row[4]}")
    print(f"  Status:       {app_row[5]}")
    print(f"  Submitted At: {app_row[6]}")
    print(f"  Decided At:   {app_row[7]}")
    print(f"  Decided By:   {app_row[8]}")

    cur.execute("SELECT id, application_id, action, actor, note, at FROM audit_log WHERE application_id = %s ORDER BY id ASC;", (app_row[0],))
    audit_rows = cur.fetchall()
    print("\nAudit Log Entries in DB:")
    for a in audit_rows:
        print(f"  [{a[5]}] Action={a[2]} Actor={a[3]} Note='{a[4]}'")

    cur.execute("SELECT count(*) FROM applications;")
    total_before = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM applications WHERE mobile LIKE '99900000%';")
    marker_before = cur.fetchone()[0]
    print(f"\nTotal applications before reset: {total_before}")
    print(f"Marker applications before reset: {marker_before}")

    # 4. Run Reset
    print("\n--- Step 4: Running Reset of Marker Data ---")
    import subprocess
    cmd = [sys.executable, "scripts/reset_marker_data.py", "--all", "--db-url", DB_URL]
    res = subprocess.run(cmd, capture_output=True, text=True)
    print(res.stdout)
    if res.returncode != 0:
        print("Reset failed:", res.stderr)
        sys.exit(1)

    # 5. Assert Final DB Counts
    print("\n--- Step 5: Assert Final DB Counts ---")
    cur.execute("SELECT count(*) FROM applications;")
    total_after = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM applications WHERE mobile LIKE '99900000%';")
    marker_after = cur.fetchone()[0]

    print(f"Final count(applications): {total_after}")
    print(f"Final count(marker rows):  {marker_after}")

    assert total_after == 100000, f"Expected exactly 100,000 applications, got {total_after}"
    assert marker_after == 0, f"Expected exactly 0 marker rows, got {marker_after}"

    print("\nSUCCESS: count(applications) = 100000 and marker rows = 0 verified!")
    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
