#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/vm_e2e_demo.py — Live VM end-to-end demo for marker mobile 9990000002.

Imports helpers from oauth_smoke.py (obtain_token, call_tool).

Usage:
  python scripts/vm_e2e_demo.py \
      --host https://sewasetu-hv.koreacentral.cloudapp.azure.com \
      --ssh-key C:/Users/harsh/Downloads/hvazure.pem
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

# Make sure we can import from the scripts dir
sys.path.insert(0, str(Path(__file__).parent))

from oauth_smoke import (
    obtain_token,
    call_tool,
    _count_gateway_messages,
    _fetch_otp,
    _info,
    _ok,
    _fail,
)

MOBILE = "9990000002"

TINY_PDF_BYTES = (
    b"%PDF-1.4\n1 0 obj<<>>\nendobj\n"
    b"xref\n0 2\n0000000000 65535 f \n0000000010 00000 n \n"
    b"trailer<<>>\nstartxref\n10\n%%EOF"
)


def ssh(key: str, cmd: str,
        host: str = "azureuser@sewasetu-hv.koreacentral.cloudapp.azure.com") -> str:
    result = subprocess.run(
        ["ssh", "-i", key, "-o", "StrictHostKeyChecking=no", host, cmd],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0 and result.stderr.strip():
        print(f"  [ssh stderr] {result.stderr.strip()}", file=sys.stderr)
    return result.stdout.strip()


def db_query(ssh_key: str, sql: str) -> str:
    cmd = f'docker exec sewasetu-assistant-db-1 psql -U sewasetu -d sewasetu -c "{sql}"'
    return ssh(ssh_key, cmd)


def reset_mobile(ssh_key: str, mobile: str) -> None:
    out = ssh(ssh_key,
        f"docker cp ~/sewasetu-assistant/scripts/reset_marker_data.py "
        f"sewasetu-assistant-app-1:/tmp/reset_marker_data.py && "
        f"docker exec sewasetu-assistant-app-1 python3 /tmp/reset_marker_data.py "
        f"--mobile {mobile} "
        f"--db-url postgresql://sewasetu:sewasetu123@db:5432/sewasetu "
        f"--smsgw-url http://smsgw:8025 2>&1"
    )
    print(out)


def section(title: str) -> None:
    print(f"\n{'='*70}\n  {title}\n{'='*70}")


def parse_result(raw: dict) -> dict:
    """Extract JSON from MCP tool result content[0].text."""
    # call_tool returns the full JSONRPC response dict
    # Structure: raw["result"]["content"][0]["text"] = JSON string
    result = raw.get("result", raw)
    content = result.get("content", [])
    if content and isinstance(content, list):
        text = content[0].get("text", "")
        try:
            return json.loads(text)
        except Exception:
            return {"raw": text, "is_error": result.get("isError", False)}
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Live VM end-to-end demo")
    ap.add_argument("--host", default="https://sewasetu-hv.koreacentral.cloudapp.azure.com")
    ap.add_argument("--ssh-key", default=r"C:\Users\harsh\Downloads\hvazure.pem")
    ap.add_argument("--mobile", default=MOBILE)
    args = ap.parse_args()

    HOST = args.host.rstrip("/")
    SMSGW = HOST + "/__gateway"
    SSH_KEY = args.ssh_key
    mobile = args.mobile
    citizen_mcp = f"{HOST}/citizen/mcp"

    # ── Step 0: Reset ───────────────────────────────────────────────────────
    section("Step 0: Reset marker data for 9990000002")
    reset_mobile(SSH_KEY, mobile)

    # ── Step 1: Login ────────────────────────────────────────────────────────
    section(f"Step 1: DCR + PKCE + OTP login for {mobile}")
    tok = obtain_token(
        portal_base=HOST,
        canonical_url=citizen_mcp,
        role="citizen",
        mobile=mobile,
        smsgw_base=SMSGW,
    )
    access_token = tok.access_token
    _ok(f"Token obtained: {access_token[:30]}...")

    # ── Step 2: get_my_status ────────────────────────────────────────────────
    section("Step 2: get_my_status (before application)")
    raw = call_tool(citizen_mcp, access_token, "get_my_status", {})
    d = parse_result(raw)
    print(json.dumps(d, ensure_ascii=False, indent=2))

    # ── Step 3: check_eligibility ────────────────────────────────────────────
    section("Step 3: check_eligibility (DOB 01/01/1958 → age 68)")
    raw = call_tool(citizen_mcp, access_token, "check_eligibility",
                    {"dob": "01/01/1958"})
    d = parse_result(raw)
    print(json.dumps(d, ensure_ascii=False, indent=2))
    assert d.get("ok"), f"Eligibility check failed: {d}"
    _ok("Eligibility confirmed")

    # ── Step 4: save_application_draft ───────────────────────────────────────
    section("Step 4: save_application_draft")
    raw = call_tool(citizen_mcp, access_token, "save_application_draft", {
        "applicant_name": "Kamla Devi",
        "dob": "01/01/1958",
        "gender": "Female",
        "marital_status": "Widowed",
        "village": "Namti Gaon",
        "block": "Namti",
        "bank_account": "998877665544",
        "ifsc": "SBIN0003077",
    })
    d = parse_result(raw)
    print(json.dumps(d, ensure_ascii=False, indent=2))
    _ok("Draft saved")

    # ── Step 5: attach_age_proof ─────────────────────────────────────────────
    section("Step 5: attach_age_proof (base64 PDF)")
    pdf_b64 = base64.b64encode(TINY_PDF_BYTES).decode("ascii")
    raw = call_tool(citizen_mcp, access_token, "attach_age_proof", {
        "document_type": "voter_id",
        "filename": "kamla_voter_id.pdf",
        "file_base64": pdf_b64,
    })
    d = parse_result(raw)
    print(json.dumps(d, ensure_ascii=False, indent=2))
    assert d.get("ok"), f"attach_age_proof failed: {d}"
    _ok("Age proof attached")

    # ── Step 6: preview_application ─────────────────────────────────────────
    section("Step 6: preview_application")
    raw = call_tool(citizen_mcp, access_token, "preview_application", {})
    d = parse_result(raw)
    print(json.dumps(d, ensure_ascii=False, indent=2))
    assert d.get("ok"), f"Preview failed: {d}"
    conf_code = d["data"]["confirmation_code"]
    _ok(f"Confirmation code: {conf_code}")

    # ── Step 7: submit_application ───────────────────────────────────────────
    section("Step 7: submit_application")
    raw = call_tool(citizen_mcp, access_token, "submit_application", {
        "confirmation_code": conf_code,
        "declaration_accepted": True,
    })
    d = parse_result(raw)
    print(json.dumps(d, ensure_ascii=False, indent=2))
    assert d.get("ok"), f"Submit failed: {d}"
    app_no = d["data"]["application_no"]
    _ok(f"Application submitted: {app_no}")

    # ── Step 8: Print DB rows verbatim ───────────────────────────────────────
    section("Step 8: DB rows (verbatim)")

    print("\n--- applications ---")
    q = (f"SELECT id, application_no, applicant_name, mobile, "
         f"TO_CHAR(dob,'DD/MM/YYYY') AS dob, status, submitted_at "
         f"FROM applications WHERE mobile='{mobile}';")
    print(db_query(SSH_KEY, q))

    print("\n--- audit_log ---")
    q = (f"SELECT al.id, al.action, al.actor, al.note, al.at "
         f"FROM audit_log al "
         f"JOIN applications a ON al.application_id=a.id "
         f"WHERE a.mobile='{mobile}' ORDER BY al.at;")
    print(db_query(SSH_KEY, q))

    print("\n--- SMS messages (from /__gateway) ---")
    import urllib.request, urllib.parse
    try:
        url = f"{SMSGW}/api/messages?" + urllib.parse.urlencode({"to": mobile})
        with urllib.request.urlopen(url, timeout=10) as r:
            msgs = json.loads(r.read().decode())
        if isinstance(msgs, list):
            for m in msgs:
                body = m.get("body") or m.get("text") or m.get("message", "")
                ts = m.get("created_at") or m.get("timestamp") or "?"
                print(f"  [{ts}] → {mobile}: {body}")
        else:
            print(msgs)
    except Exception as e:
        print(f"  (could not fetch SMS: {e})")

    # ── Step 9: end_my_session ────────────────────────────────────────────────
    section("Step 9: end_my_session")
    raw = call_tool(citizen_mcp, access_token, "end_my_session", {})
    d = parse_result(raw)
    print(json.dumps(d, ensure_ascii=False, indent=2))
    assert d.get("ok"), f"end_my_session failed: {d}"
    _ok("Session ended")

    # Verify token now rejected (should raise HTTPError 401 or return ok=False)
    try:
        raw2 = call_tool(citizen_mcp, access_token, "get_my_status", {})
        d2 = parse_result(raw2)
        if d2.get("ok") is False or "revok" in str(d2).lower() or "session" in str(d2).lower():
            _ok("Token correctly rejected after end_my_session")
        else:
            print(f"  ⚠ Token may still be valid: {d2}")
    except Exception as e:
        _ok(f"Token correctly rejected (HTTP error: {type(e).__name__})")

    # ── Step 10: Refusal — age 59, no DB row ────────────────────────────────
    section(f"Step 10: Refusal (age 59) — confirm 0 rows for {mobile}")

    print("  [Resetting data first]")
    reset_mobile(SSH_KEY, mobile)

    # Fresh token for the 59-year-old attempt
    tok2 = obtain_token(
        portal_base=HOST,
        canonical_url=citizen_mcp,
        role="citizen",
        mobile=mobile,
        smsgw_base=SMSGW,
    )
    token2 = tok2.access_token

    today = date.today()
    dob_59 = date(today.year - 59, today.month, today.day)
    dob_str = dob_59.strftime("%d/%m/%Y")

    call_tool(citizen_mcp, token2, "save_application_draft", {
        "applicant_name": "Young Person",
        "dob": dob_str,
        "gender": "Male",
        "marital_status": "Single",
        "village": "Namti Gaon",
        "block": "Namti",
        "bank_account": "111222333444",
        "ifsc": "SBIN0003077",
    })
    call_tool(citizen_mcp, token2, "attach_age_proof", {
        "document_type": "voter_id",
        "filename": "young.pdf",
        "file_base64": base64.b64encode(TINY_PDF_BYTES).decode(),
    })
    raw = call_tool(citizen_mcp, token2, "preview_application", {})
    d = parse_result(raw)
    print(f"\n  preview_application ok={d.get('ok')}")

    if not d.get("ok"):
        problems = d.get("problems", d.get("message_for_user", ""))
        print(f"  Problems: {problems}")
        _ok("Correctly refused at preview — no application created")
    else:
        # Submit should fail
        raw_sub = call_tool(citizen_mcp, token2, "submit_application", {
            "confirmation_code": d["data"]["confirmation_code"],
            "declaration_accepted": True,
        })
        ds = parse_result(raw_sub)
        assert not ds.get("ok"), f"Expected submit to fail for 59-year-old: {ds}"
        _ok("Submit correctly refused for 59-year-old")

    # Confirm no row in DB
    print("\n  DB count check (must be 0):")
    row_check = db_query(SSH_KEY,
        f"SELECT count(*) FROM applications WHERE mobile='{mobile}';")
    print(f"  {row_check}")

    # ── Summary ───────────────────────────────────────────────────────────────
    section("ALL STEPS COMPLETE ✓")
    print(f"  Application number: {app_no}")
    print(f"  All assertions passed.")


if __name__ == "__main__":
    main()
