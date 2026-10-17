# -*- coding: utf-8 -*-
"""
evals/security.py — Deterministic security checks (S1–S7) with zero LLM cost.

Verifies fundamental security invariants against the live deployment:
  S1: Role separation (citizen token rejected at /officer/mcp, officer token rejected at /citizen/mcp)
  S2: Audience isolation (token with root/wrong aud rejected at both endpoints)
  S3: Token lifecycle (revocation kills refresh; end_my_session invalidates access immediately)
  S4: Tool separation (tools/list on each endpoint contains no tools from the other)
  S5: Edge perimeter (/internal blocked at public gateway)
  S6: SQL injection defense (hostile strings stored literally as untrusted text)
  S7: Identity isolation (citizen tools derive identity strictly from verified token sub)

Usage:
    python -m evals.security
"""

from __future__ import annotations

import os
import sys
import json
import requests
from tools.oauth_client import (
    OAuthClient,
    http_post_json,
    MODERN_PROTOCOL_VERSION,
    MODERN_META,
)
from evals.fixtures import cleanup_persona, get_db_connection

PUBLIC_BASE_URL = os.environ.get(
    "HOST",
    os.environ.get("PUBLIC_BASE_URL", "https://sewasetu-hv.koreacentral.cloudapp.azure.com")
).rstrip("/")
SMSGW_URL = os.environ.get(
    "SMSGW_URL",
    f"{PUBLIC_BASE_URL}/__gateway"
).rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin123")

CITIZEN_URL = f"{PUBLIC_BASE_URL}/citizen/mcp"
OFFICER_URL = f"{PUBLIC_BASE_URL}/officer/mcp"


def log_check(test_id: str, name: str, passed: bool, detail: str = ""):
    status = "[PASS]" if passed else "[FAIL]"
    print(f"  {status} {test_id}: {name}")
    if detail and not passed:
        print(f"         Detail: {detail}")


def run_s1_role_separation(client: OAuthClient) -> tuple[bool, str]:
    """S1: Citizen token rejected at /officer/mcp; officer token rejected at /citizen/mcp."""
    mobile = "9990000075"
    cleanup_persona(mobile)

    # 1. Citizen token sent to /officer/mcp
    tok_citizen = client.obtain_citizen_token(mobile, canonical_url=CITIZEN_URL)
    headers = {
        "Authorization": f"Bearer {tok_citizen.access_token}",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
        "Mcp-Method": "tools/call",
        "Mcp-Name": "queue_overview",
    }
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"_meta": MODERN_META, "name": "queue_overview", "arguments": {}},
    }
    code, _, _ = http_post_json(OFFICER_URL, payload, headers=headers)
    if code not in (401, 403):
        return False, f"Citizen token accepted at /officer/mcp! HTTP {code}"

    # 2. Officer token sent to /citizen/mcp
    tok_officer = client.obtain_officer_token(ADMIN_USER, ADMIN_PASS, canonical_url=OFFICER_URL)
    headers_off = {
        "Authorization": f"Bearer {tok_officer.access_token}",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
        "Mcp-Method": "tools/call",
        "Mcp-Name": "get_scheme_info",
    }
    payload_off = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"_meta": MODERN_META, "name": "get_scheme_info", "arguments": {}},
    }
    code_off, _, _ = http_post_json(CITIZEN_URL, payload_off, headers=headers_off)
    if code_off not in (401, 403):
        return False, f"Officer token accepted at /citizen/mcp! HTTP {code_off}"

    cleanup_persona(mobile)
    return True, "Both cross-role requests rejected with HTTP 401/403"


def run_s2_audience_isolation(client: OAuthClient) -> tuple[bool, str]:
    """S2: Token with root audience (no resource / portal root) is rejected by both MCP servers."""
    mobile = "9990000076"
    cleanup_persona(mobile)

    tok_root = client.obtain_citizen_token(mobile, canonical_url=PUBLIC_BASE_URL)
    for endpoint in (CITIZEN_URL, OFFICER_URL):
        headers = {
            "Authorization": f"Bearer {tok_root.access_token}",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
            "Mcp-Method": "tools/call",
            "Mcp-Name": "whoami",
        }
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"_meta": MODERN_META, "name": "whoami", "arguments": {}},
        }
        code, _, _ = http_post_json(endpoint, payload, headers=headers)
        if code != 401:
            return False, f"Root audience accepted at {endpoint}! HTTP {code}"

    cleanup_persona(mobile)
    return True, "Root audience correctly rejected by both MCP endpoints"


def run_s3_token_lifecycle(client: OAuthClient) -> tuple[bool, str]:
    """S3: Revocation terminates refresh; end_my_session invalidates access immediately."""
    mobile = "9990000077"
    cleanup_persona(mobile)

    # 1. Revoke refresh token
    tok = client.obtain_citizen_token(mobile, canonical_url=CITIZEN_URL)
    client.revoke(tok.refresh_token)
    try:
        client.refresh_token(tok.refresh_token, canonical_url=CITIZEN_URL)
        return False, "Revoked refresh token was successfully used to refresh!"
    except Exception:
        pass  # expected failure

    # 2. Test end_my_session immediate invalidation
    tok2 = client.obtain_citizen_token(mobile, canonical_url=CITIZEN_URL)
    res = client.call_tool(tok2.access_token, "end_my_session", {}, mcp_url=CITIZEN_URL)
    if isinstance(res, dict) and res.get("isError"):
        return False, f"end_my_session tool call failed: {res}"

    # Immediately call another tool with the now-invalidated token -> must 401
    headers = {
        "Authorization": f"Bearer {tok2.access_token}",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
        "Mcp-Method": "tools/call",
        "Mcp-Name": "get_scheme_info",
    }
    payload = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"_meta": MODERN_META, "name": "get_scheme_info", "arguments": {}},
    }
    code, _, _ = http_post_json(CITIZEN_URL, payload, headers=headers)
    if code != 401:
        return False, f"Old access token still accepted after end_my_session! HTTP {code}"

    cleanup_persona(mobile)
    return True, "Revocation kills refresh and end_my_session invalidates access immediately"


def run_s4_tools_list_separation(client: OAuthClient) -> tuple[bool, str]:
    """S4: tools/list on each endpoint contains zero tools from the other."""
    # List tools on citizen endpoint
    tok_cit = client.obtain_citizen_token("9990000078", canonical_url=CITIZEN_URL)
    headers_cit = {
        "Authorization": f"Bearer {tok_cit.access_token}",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
    }
    payload_cit = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": MODERN_META}}
    _, body_cit, _ = http_post_json(CITIZEN_URL, payload_cit, headers=headers_cit)
    cit_tools = {t["name"] for t in body_cit.get("result", {}).get("tools", [])}

    # List tools on officer endpoint
    tok_off = client.obtain_officer_token(ADMIN_USER, ADMIN_PASS, canonical_url=OFFICER_URL)
    headers_off = {
        "Authorization": f"Bearer {tok_off.access_token}",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
    }
    payload_off = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": MODERN_META}}
    _, body_off, _ = http_post_json(OFFICER_URL, payload_off, headers=headers_off)
    off_tools = {t["name"] for t in body_off.get("result", {}).get("tools", [])}

    officer_only = {"verify_application", "decide_application", "queue_overview", "list_applications", "search_applications"}
    citizen_only = {"submit_application", "end_my_session", "withdraw_application"}

    cit_leak = cit_tools.intersection(officer_only)
    if cit_leak:
        return False, f"Citizen tools/list exposed officer tools: {cit_leak}"

    off_leak = off_tools.intersection(citizen_only)
    if off_leak:
        return False, f"Officer tools/list exposed citizen tools: {off_leak}"

    cleanup_persona("9990000078")
    return True, f"Clean separation: citizen has {len(cit_tools)} tools, officer has {len(off_tools)} tools"


def run_s5_internal_edge_blocking() -> tuple[bool, str]:
    """S5: /internal endpoints are blocked at the public gateway edge."""
    url = f"{PUBLIC_BASE_URL}/internal/applications"
    resp = requests.get(url, timeout=10)
    if resp.status_code not in (401, 403, 404):
        return False, f"Public request to /internal returned HTTP {resp.status_code} (expected blocked)"
    return True, f"Edge successfully blocked public access to /internal (HTTP {resp.status_code})"


def _extract_envelope(res: dict) -> dict:
    if isinstance(res, dict) and "result" in res:
        res = res["result"]
    if isinstance(res, dict) and "content" in res:
        for item in res.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    return json.loads(item.get("text", "{}"))
                except Exception:
                    pass
    return res if isinstance(res, dict) else {}


def run_s6_hostile_strings_stored_literally(client: OAuthClient) -> tuple[bool, str]:
    """S6: Hostile injection strings are stored literally and do not alter DB structure."""
    mobile = "9990000079"
    cleanup_persona(mobile)

    hostile_name = "Robert'); DROP TABLE applications;--"
    tok = client.obtain_citizen_token(mobile, canonical_url=CITIZEN_URL)

    # 1. Save draft with hostile applicant name
    res_draft = client.call_tool(
        tok.access_token,
        "save_application_draft",
        {
            "applicant_name": hostile_name,
            "dob": "10/10/1950",
            "gender": "Male",
            "marital_status": "Married",
            "village": "Kathoni",
            "block": "Sonari",
            "bank_account": "11223344556677",
            "ifsc": "SBIN0001234",
        },
        mcp_url=CITIZEN_URL,
    )
    env_draft = _extract_envelope(res_draft)
    if not env_draft.get("ok"):
        return False, f"Failed to save draft: {env_draft}"

    # 2. Attach age proof via base64 (deterministic, zero external HTTP dependency)
    b64_pdf = "JVBERi0xLjQKMSAwIG9iajw8Pj5lbmRvYmoKdHJhaWxlcjw8Pj4KJSVFT0YK"
    res_proof = client.call_tool(
        tok.access_token,
        "attach_age_proof",
        {
            "document_type": "voter_id",
            "filename": "proof.pdf",
            "file_base64": b64_pdf,
        },
        mcp_url=CITIZEN_URL,
    )
    env_proof = _extract_envelope(res_proof)
    if not env_proof.get("ok"):
        return False, f"Failed to attach proof: {env_proof}"

    # 3. Preview application to generate confirmation code
    res_prev = client.call_tool(
        tok.access_token,
        "preview_application",
        {},
        mcp_url=CITIZEN_URL,
    )
    env_prev = _extract_envelope(res_prev)
    conf_code = env_prev.get("data", {}).get("confirmation_code")
    if not conf_code:
        return False, f"Could not obtain confirmation code from preview: {env_prev}"

    # 4. Submit application
    res_sub = client.call_tool(
        tok.access_token,
        "submit_application",
        {
            "confirmation_code": conf_code,
            "declaration_accepted": True,
        },
        mcp_url=CITIZEN_URL,
    )
    env_sub = _extract_envelope(res_sub)
    if not env_sub.get("ok"):
        return False, f"Submission error on hostile input: {env_sub}"

    # 5. Verify database: table still exists and literal string is stored
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT applicant_name FROM applications WHERE mobile = %s", (mobile,))
            row = cur.fetchone()
            if not row or row[0] != hostile_name:
                return False, f"Literal string not preserved! DB returned: {row}"
    finally:
        conn.close()

    cleanup_persona(mobile)
    return True, "SQL injection string stored literally in applications table; table intact"


def run_s7_identity_isolation(client: OAuthClient) -> tuple[bool, str]:
    """S7: Citizen tools cannot query or modify another mobile's records by argument fuzzing."""
    mobile_a = "9990000080"
    mobile_b = "9990000081"
    cleanup_persona(mobile_a)

    tok_a = client.obtain_citizen_token(mobile_a, canonical_url=CITIZEN_URL)

    # Call get_my_status attempting to pass mobile_b in arguments
    res = client.call_tool(
        tok_a.access_token,
        "get_my_status",
        {"mobile": mobile_b, "phone": mobile_b, "target": mobile_b},
        mcp_url=CITIZEN_URL,
    )
    status_text = json.dumps(res)
    if mobile_b in status_text:
        return False, f"Citizen tool echoed foreign mobile: {status_text}"

    cleanup_persona(mobile_a)
    return True, "Identity strictly bound to token sub; foreign argument values ignored"


def run_all_security_checks() -> bool:
    print("\n========================================================")
    print("  RUNNING DETERMINISTIC SECURITY SUITE (S1–S7)")
    print("  (Zero LLM Cost — Verifying Edge & Server Invariants)")
    print("========================================================\n")

    client = OAuthClient(
        portal_base=PUBLIC_BASE_URL,
        smsgw_base=SMSGW_URL,
    )

    checks = [
        ("S1", "Role Separation (Citizen vs Officer)", lambda: run_s1_role_separation(client)),
        ("S2", "Audience Isolation (Root/Wrong aud rejected)", lambda: run_s2_audience_isolation(client)),
        ("S3", "Token Lifecycle (Revocation & Immediate Signout)", lambda: run_s3_token_lifecycle(client)),
        ("S4", "Tool Separation (Zero cross-tool leakage)", lambda: run_s4_tools_list_separation(client)),
        ("S5", "Edge Perimeter (/internal blocked at gateway)", run_s5_internal_edge_blocking),
        ("S6", "Hostile String Storage (SQL injection immunity)", lambda: run_s6_hostile_strings_stored_literally(client)),
        ("S7", "Identity Isolation (Subject binding enforcement)", lambda: run_s7_identity_isolation(client)),
    ]

    all_passed = True
    for test_id, name, func in checks:
        try:
            passed, detail = func()
            log_check(test_id, name, passed, detail)
            if not passed:
                all_passed = False
        except Exception as e:
            log_check(test_id, name, False, f"Exception: {e}")
            all_passed = False

    print("\n--------------------------------------------------------")
    if all_passed:
        print("  RESULT: ALL SECURITY INVARIANTS (S1–S7) PASSED [OK]")
    else:
        print("  RESULT: ONE OR MORE SECURITY CHECKS FAILED [FAIL]")
    print("--------------------------------------------------------\n")
    return all_passed


if __name__ == "__main__":
    success = run_all_security_checks()
    sys.exit(0 if success else 1)
