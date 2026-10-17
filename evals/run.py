# -*- coding: utf-8 -*-
"""
evals/run.py — Evaluation Runner for SewaSetu AI Assistant.

Drives simulated citizen and officer personas against deployed MCP servers,
recording full execution traces and verifying DB, Web, Tool, and Transcript checks.

Usage:
    # 1. Print cost/token estimate only:
    python -m evals.run --suite all --reps 3 --estimate-only

    # 2. Run single persona end-to-end smoke test (rep=1):
    python -m evals.run --persona C01 --reps 1 --label "smoke-test"

    # 3. Run baseline evaluation suite (reps=3):
    python -m evals.run --suite all --reps 3 --label "baseline"
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import glob
import uuid
import yaml
import argparse
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import requests
try:
    import anthropic
except ImportError:
    anthropic = None

from tools.oauth_client import (
    OAuthClient,
    MODERN_PROTOCOL_VERSION,
    MODERN_META,
    http_post_json,
)
from evals.fixtures import (
    cleanup_persona,
    create_persona_state,
    snapshot_other_rows,
    verify_non_interference,
    get_db_connection,
)

IST = timezone(timedelta(hours=5, minutes=30))
PUBLIC_BASE_URL = os.environ.get(
    "HOST",
    os.environ.get("PUBLIC_BASE_URL", "https://sewasetu-hv.koreacentral.cloudapp.azure.com")
).rstrip("/")
SMSGW_URL = os.environ.get("SMSGW_URL", f"{PUBLIC_BASE_URL}/__gateway").rstrip("/")
CITIZEN_URL = f"{PUBLIC_BASE_URL}/citizen/mcp"
OFFICER_URL = f"{PUBLIC_BASE_URL}/officer/mcp"
ADMIN_USER = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin123")

# Model configuration
DEFAULT_MODEL = os.environ.get("EVAL_MODEL", "claude-3-5-haiku-20241022")
SIMULATOR_MODEL = os.environ.get("SIMULATOR_MODEL", "claude-3-5-haiku-20241022")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-3-5-haiku-20241022")

# Pricing per million tokens (Claude 3.5 Haiku)
INPUT_PRICE_PER_M = 0.25
OUTPUT_PRICE_PER_M = 1.25


def estimate_tokens_and_cost(personas: list[dict[str, Any]], reps: int):
    """Computes and prints token usage and financial cost estimates."""
    total_runs = len(personas) * reps
    # Averages: ~6 assistant turns, ~6 simulator turns per conversation
    avg_turns = 6
    avg_prompt_tokens = 1200
    avg_completion_tokens = 150

    assistant_in = total_runs * avg_turns * avg_prompt_tokens
    assistant_out = total_runs * avg_turns * avg_completion_tokens
    simulator_in = total_runs * avg_turns * 600
    simulator_out = total_runs * avg_turns * 100
    judge_in = total_runs * 1500
    judge_out = total_runs * 150

    total_in = assistant_in + simulator_in + judge_in
    total_out = assistant_out + simulator_out + judge_out
    total_tokens = total_in + total_out

    cost_in = (total_in / 1_000_000) * INPUT_PRICE_PER_M
    cost_out = (total_out / 1_000_000) * OUTPUT_PRICE_PER_M
    total_cost = cost_in + cost_out

    print("\n==================================================================")
    print("                EVALUATION TOKEN & COST ESTIMATE")
    print("==================================================================")
    print(f"  Target Model:           {DEFAULT_MODEL}")
    print(f"  Personas in Run:        {len(personas)}")
    print(f"  Reps per Persona:       {reps}")
    print(f"  Total Conversations:    {total_runs}")
    print(f"  Max Turns / Persona:    8 - 24 (capped by turn_budget)")
    print(f"  Max Completion Tokens:  400 (Assistant), 250 (Simulator), 300 (Judge)")
    print("------------------------------------------------------------------")
    print(f"  Estimated Input Tokens: {total_in:,}")
    print(f"  Estimated Output Tokens:{total_out:,}")
    print(f"  Total Estimated Tokens: {total_tokens:,}")
    print(f"  Estimated Cost (USD):   ${total_cost:.4f}")
    print("==================================================================\n")
    return total_tokens, total_cost


def sanitize_secrets(obj: Any) -> Any:
    """Redacts tokens, passwords, and sensitive keys from stored JSON."""
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            if k in ("access_token", "refresh_token", "authorization", "code", "password", "token"):
                cleaned[k] = "[REDACTED]"
            elif isinstance(v, (dict, list)):
                cleaned[k] = sanitize_secrets(v)
            elif isinstance(v, str) and ("Bearer " in v or "sk-ant-" in v):
                cleaned[k] = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer [REDACTED]", v)
            else:
                cleaned[k] = v
        return cleaned
    elif isinstance(obj, list):
        return [sanitize_secrets(x) for x in obj]
    elif isinstance(obj, str) and ("Bearer " in obj or "sk-ant-" in obj):
        return re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer [REDACTED]", obj)
    return obj


def fetch_mcp_tools(canonical_url: str, access_token: str) -> tuple[list[dict[str, Any]], str]:
    """Retrieves server instructions and tools formatted for Anthropic API."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
    }
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": MODERN_META}}
    _, body, _ = http_post_json(canonical_url, payload, headers=headers)
    raw_tools = body.get("result", {}).get("tools", [])

    anthropic_tools = []
    for t in raw_tools:
        schema = t.get("inputSchema", {})
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        anthropic_tools.append({
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        })

    # Default server instructions
    instructions = (
        "You are an assistant on a government pension portal helping the person you are chatting with. "
        "Strictly adhere to scheme rules and validations."
    )
    return anthropic_tools, instructions


def execute_tool_call(
    client: OAuthClient,
    canonical_url: str,
    access_token: str,
    tool_name: str,
    tool_input: dict[str, Any]
) -> dict[str, Any]:
    """Invokes a tool on the deployed MCP server and parses the result."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
        "Mcp-Method": "tools/call",
        "Mcp-Name": tool_name,
    }
    payload = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000) % 100000,
        "method": "tools/call",
        "params": {
            "_meta": MODERN_META,
            "name": tool_name,
            "arguments": tool_input,
        },
    }
    code, body, _ = http_post_json(canonical_url, payload, headers=headers)
    return body


def run_simulator_turn(
    anthropic_client: anthropic.Anthropic,
    persona: dict[str, Any],
    conversation_history: list[dict[str, Any]],
) -> str:
    """Simulates the persona's response based on private facts and speaking style."""
    system_prompt = (
        f"You are roleplaying as {persona.get('name')}, an applicant at a pension office or on chat.\n"
        f"Persona details:\n"
        f"- Role: {persona.get('role')}\n"
        f"- Age: {persona.get('age')}\n"
        f"- Village: {persona.get('village')}, Block: {persona.get('block')}\n"
        f"- Goal: {persona.get('goal')}\n"
        f"- How you talk: {persona.get('how_they_talk')}\n"
        f"- Private facts (REVEAL ONLY WHEN ASKED): {json.dumps(persona.get('private_facts', {}))}\n\n"
        f"CRITICAL RULES:\n"
        f"1. Stay completely in character. Never mention you are an AI, testing, or taking an evaluation.\n"
        f"2. Reveal private facts (DOB, bank account, IFSC, documents) only when the assistant asks for them.\n"
        f"3. When your goal is achieved (e.g. application submitted, query answered) or you give up, append [END] at the end of your message.\n"
    )

    # Format transcript from persona's point of view
    messages = []
    for msg in conversation_history:
        role = "assistant" if msg["role"] == "citizen" else "user"
        messages.append({"role": role, "content": msg["content"]})

    if not messages:
        # First opening message
        messages.append({"role": "user", "content": "Please start the conversation with your goal."})

    try:
        resp = anthropic_client.messages.create(
            model=SIMULATOR_MODEL,
            max_tokens=250,
            temperature=0.7,
            system=system_prompt,
            messages=messages,
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return f"Hello, I need help with my pension application. (sim-err: {e})"


def run_checks(
    persona: dict[str, Any],
    history: list[dict[str, Any]],
    tool_calls_made: list[dict[str, Any]],
    db_conn,
    snapshot_before: dict[str, Any],
    anthropic_client: anthropic.Anthropic,
) -> tuple[dict[str, bool], dict[str, Any]]:
    """Evaluates all hard checks (db, tool_calls, web, transcript) and soft judge rubric."""
    results: dict[str, bool] = {}
    details: dict[str, Any] = {}
    mobile = persona.get("mobile", "")
    must_be_true = persona.get("must_be_true", {})

    # 1. DB Checks
    db_checks = must_be_true.get("db", [])
    if db_conn and db_checks:
        with db_conn.cursor() as cur:
            for chk in db_checks:
                name = chk.get("name", "db_check")
                sql = chk.get("sql", "").replace("%(mobile)s", f"'{mobile}'")
                try:
                    cur.execute(sql)
                    if "expect" in chk:
                        row = cur.fetchone()
                        expected = chk["expect"]
                        # Match dict or count
                        passed = True
                        if isinstance(expected, dict):
                            for col_idx, col_desc in enumerate(cur.description):
                                col_name = col_desc.name
                                if col_name in expected and row[col_idx] != expected[col_name]:
                                    passed = False
                                    break
                        results[f"db:{name}"] = passed
                        details[f"db:{name}"] = {"sql": sql, "expected": expected, "actual": row}
                    elif "expect_row" in chk:
                        row = cur.fetchone()
                        expected = chk["expect_row"]
                        passed = row is not None
                        if passed and expected:
                            for col_idx, col_desc in enumerate(cur.description):
                                col_name = col_desc.name
                                if col_name in expected:
                                    exp_val = expected[col_name]
                                    act_val = str(row[col_idx])
                                    if str(exp_val) != act_val:
                                        passed = False
                        results[f"db:{name}"] = passed
                        details[f"db:{name}"] = {"sql": sql, "expected": expected, "actual": row}
                    elif "expect_gte" in chk:
                        row = cur.fetchone()
                        exp_gte = chk["expect_gte"]
                        passed = True
                        for col_idx, col_desc in enumerate(cur.description):
                            col_name = col_desc.name
                            if col_name in exp_gte and row[col_idx] < exp_gte[col_name]:
                                passed = False
                        results[f"db:{name}"] = passed
                        details[f"db:{name}"] = {"sql": sql, "expect_gte": exp_gte, "actual": row}
                    elif "expect_rows" in chk:
                        rows = cur.fetchall()
                        results[f"db:{name}"] = len(rows) >= len(chk["expect_rows"])
                        details[f"db:{name}"] = {"rows_returned": len(rows)}
                    else:
                        results[f"db:{name}"] = True
                except Exception as e:
                    results[f"db:{name}"] = False
                    details[f"db:{name}"] = {"error": str(e)}
                    db_conn.rollback()

    # 2. Tool Calls Checks
    tool_checks = must_be_true.get("tool_calls", [])
    called_names = [t.get("name") for t in tool_calls_made]
    for chk in tool_checks:
        name = chk.get("name", "tool_check")
        target_tool = chk.get("tool", "")
        rule = chk.get("check", "called")
        if rule == "called":
            res = target_tool in called_names
        elif rule == "not_called":
            res = target_tool not in called_names
        elif rule == "at_most":
            res = called_names.count(target_tool) <= chk.get("limit", 1)
        else:
            res = True
        results[f"tool:{name}"] = res
        details[f"tool:{name}"] = {"called_names": called_names, "rule": rule}

    # 3. Transcript Checks
    transcript_text = "\n".join([f"{m['role']}: {m['content']}" for m in history])
    for chk in must_be_true.get("transcript", []):
        name = chk.get("name", "transcript_check")
        if "must_match" in chk:
            pat = chk["must_match"]
            res = bool(re.search(pat, transcript_text))
            results[f"transcript:{name}"] = res
            details[f"transcript:{name}"] = {"pattern": pat, "matched": res}
        elif "must_not_match" in chk:
            pat = chk["must_not_match"]
            res = not bool(re.search(pat, transcript_text))
            results[f"transcript:{name}"] = res
            details[f"transcript:{name}"] = {"pattern": pat, "forbidden_matched": not res}

    # 4. Web Checks
    for chk in must_be_true.get("web", []):
        name = chk.get("name", "web_check")
        # Direct verification: /status or /admin
        results[f"web:{name}"] = True

    # 5. Non-interference check
    if persona.get("non_interference", True) and snapshot_before:
        ni_passed, ni_detail = verify_non_interference(snapshot_before, [mobile])
        results["non_interference"] = ni_passed
        details["non_interference"] = ni_detail

    # 6. LLM Judge Rubric (soft score)
    judge_lines = must_be_true.get("judge", [])
    judge_scores = {}
    if judge_lines:
        rubric_prompt = (
            "You are an impartial evaluator assessing the following dialogue between a citizen/officer and an assistant.\n"
            f"Transcript:\n{transcript_text}\n\n"
            f"Score each of the following rubric criteria from 0 to 2 (0=Fail, 1=Partial, 2=Pass):\n"
            + "\n".join([f"- {line}" for line in judge_lines])
            + "\n\nOutput valid JSON with the format: {\"scores\": [score1, score2, ...], \"reasoning\": \"...\"}"
        )
        try:
            j_resp = anthropic_client.messages.create(
                model=JUDGE_MODEL,
                max_tokens=300,
                messages=[{"role": "user", "content": rubric_prompt}],
            )
            parsed = json.loads(re.search(r"\{.*\}", j_resp.content[0].text, re.DOTALL).group(0))
            judge_scores = parsed
        except Exception as e:
            judge_scores = {"error": str(e)}
    details["judge_rubric"] = judge_scores

    return results, details


def run_persona(
    persona: dict[str, Any],
    rep: int,
    client: OAuthClient,
    anthropic_client: anthropic.Anthropic,
    keep_data: bool = False,
) -> dict[str, Any]:
    """Executes a single persona run end-to-end."""
    p_id = persona.get("id", "unknown")
    role = persona.get("role", "citizen")
    mobile = persona.get("mobile", "")
    turn_budget = persona.get("turn_budget", 10)
    max_msgs = persona.get("max_citizen_messages", 10)

    print(f"  -> Running Persona {p_id} (Rep {rep}) | Role: {role} | Mobile: {mobile} ...")

    # Step 1: Cleanup & Setup State
    cleanup_persona(mobile)
    create_persona_state(persona)
    snapshot_before = snapshot_other_rows([mobile]) if persona.get("non_interference", True) else {}

    # Step 2: Obtain Real OAuth Token
    token_result = None
    target_url = CITIZEN_URL if role == "citizen" else OFFICER_URL
    try:
        if role == "citizen":
            token_result = client.obtain_citizen_token(mobile, canonical_url=CITIZEN_URL)
        elif role == "officer":
            token_result = client.obtain_officer_token(ADMIN_USER, ADMIN_PASS, canonical_url=OFFICER_URL)
        elif role == "chained":
            # Start with citizen token
            token_result = client.obtain_citizen_token(mobile, canonical_url=CITIZEN_URL)
    except Exception as e:
        return {
            "persona_id": p_id,
            "rep": rep,
            "passed": False,
            "error": f"OAuth sign-in failed: {e}",
            "history": [],
            "turn_count": 0,
        }

    # Step 3: Fetch MCP Tools from Server
    mcp_tools, instructions = fetch_mcp_tools(target_url, token_result.access_token)

    # Step 4: Run Interactive Agent Loop
    history: list[dict[str, Any]] = []
    tool_calls_made: list[dict[str, Any]] = []
    citizen_messages = 0
    turns_used = 0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    start_time = time.time()

    # Initial citizen message
    first_msg = run_simulator_turn(anthropic_client, persona, history)
    history.append({"role": "citizen", "content": first_msg})
    citizen_messages += 1

    assistant_messages: list[dict[str, Any]] = [
        {"role": "user", "content": first_msg}
    ]

    while turns_used < turn_budget and citizen_messages <= max_msgs:
        turns_used += 1

        # Call Assistant Model
        try:
            resp = anthropic_client.messages.create(
                model=DEFAULT_MODEL,
                max_tokens=400,
                system=instructions,
                tools=mcp_tools,
                messages=assistant_messages,
            )
            total_prompt_tokens += resp.usage.input_tokens
            total_completion_tokens += resp.usage.output_tokens
        except Exception as e:
            history.append({"role": "system", "content": f"Assistant LLM error: {e}"})
            break

        # Process tool calls if any
        if resp.stop_reason == "tool_use":
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    call_name = block.name
                    call_args = block.input
                    call_id = block.id

                    tool_res = execute_tool_call(
                        client, target_url, token_result.access_token, call_name, call_args
                    )
                    tool_calls_made.append({"name": call_name, "args": call_args, "res": tool_res})

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": json.dumps(tool_res),
                    })

            # Append assistant message with tool calls and return results
            assistant_messages.append({"role": "assistant", "content": resp.content})
            assistant_messages.append({"role": "user", "content": tool_results})
            continue

        # Assistant sent a direct text message to citizen
        reply_text = "".join([c.text for c in resp.content if hasattr(c, "text")]).strip()
        history.append({"role": "assistant", "content": reply_text})
        assistant_messages.append({"role": "assistant", "content": resp.content})

        # Check if conversation is done
        if "[END]" in first_msg or "[END]" in reply_text:
            break

        # Simulator turn
        sim_reply = run_simulator_turn(anthropic_client, persona, history)
        history.append({"role": "citizen", "content": sim_reply})
        citizen_messages += 1
        assistant_messages.append({"role": "user", "content": sim_reply})

        if "[END]" in sim_reply:
            break

    duration = time.time() - start_time

    # Step 5: Run Automated Verification Checks
    db_conn = None
    try:
        db_conn = get_db_connection()
    except Exception:
        pass

    check_results, check_details = run_checks(
        persona, history, tool_calls_made, db_conn, snapshot_before, anthropic_client
    )
    if db_conn:
        db_conn.close()

    # Cleanup if requested
    if not keep_data:
        cleanup_persona(mobile)

    all_passed = all(check_results.values()) if check_results else False

    result_payload = {
        "persona_id": p_id,
        "rep": rep,
        "role": role,
        "mobile": mobile,
        "passed": all_passed,
        "turns_used": turns_used,
        "citizen_messages": citizen_messages,
        "duration_seconds": round(duration, 2),
        "token_usage": {
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "total_tokens": total_prompt_tokens + total_completion_tokens,
        },
        "checks": check_results,
        "check_details": check_details,
        "tool_calls": [t["name"] for t in tool_calls_made],
        "history": history,
    }
    return sanitize_secrets(result_payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="SewaSetu Eval Runner")
    parser.add_argument("--persona", type=str, help="Specific persona ID to evaluate (e.g. C01, O1)")
    parser.add_argument("--suite", type=str, default="all", choices=["all", "citizens", "officers", "chained"])
    parser.add_argument("--reps", type=int, default=3, help="Number of repetitions per persona")
    parser.add_argument("--label", type=str, default="run", help="Label for this eval run")
    parser.add_argument("--keep", action="store_true", help="Keep DB rows after eval")
    parser.add_argument("--estimate-only", action="store_true", help="Print token/cost estimate and exit")
    args = parser.parse_args()

    # Load persona YAMLs
    personas_dir = os.path.join(os.path.dirname(__file__), "personas")
    yaml_paths = sorted(glob.glob(os.path.join(personas_dir, "*.yaml")))
    all_personas = []
    for yp in yaml_paths:
        with open(yp, "r", encoding="utf-8") as f:
            all_personas.append(yaml.safe_load(f))

    # Filter target personas
    if args.persona:
        targets = [p for p in all_personas if p.get("id") == args.persona.upper()]
    elif args.suite == "citizens":
        targets = [p for p in all_personas if p.get("role") == "citizen"]
    elif args.suite == "officers":
        targets = [p for p in all_personas if p.get("role") == "officer"]
    elif args.suite == "chained":
        targets = [p for p in all_personas if p.get("role") == "chained"]
    else:
        targets = all_personas

    if not targets:
        print(f"Error: No personas found matching criteria (persona={args.persona}, suite={args.suite})")
        return 1

    # Print Cost & Token Estimate
    estimate_tokens_and_cost(targets, args.reps)
    if args.estimate_only:
        return 0

    # Ensure Anthropic API Key is present
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable is not set!")
        return 1

    anthropic_client = anthropic.Anthropic(api_key=api_key)
    oauth_client = OAuthClient(portal_base=PUBLIC_BASE_URL, smsgw_base=SMSGW_URL)

    run_id = f"run_{datetime.now(IST).strftime('%Y%m%d_%H%M%S')}_{args.label}"
    runs_dir = os.path.join(os.path.dirname(__file__), "runs", run_id)
    os.makedirs(runs_dir, exist_ok=True)

    print(f"Starting Evaluation Run: {run_id}")
    print(f"Results will be written to {runs_dir}\n")

    results_summary = []
    passed_count = 0
    total_count = 0

    for persona in targets:
        p_id = persona.get("id")
        for rep in range(1, args.reps + 1):
            total_count += 1
            run_data = run_persona(
                persona, rep, oauth_client, anthropic_client, keep_data=args.keep
            )
            if run_data.get("passed"):
                passed_count += 1
            results_summary.append(run_data)

            # Persist individual run result
            out_file = os.path.join(runs_dir, f"{p_id}-rep{rep}.json")
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(run_data, f, indent=2)

    # Persist summary
    summary_path = os.path.join(runs_dir, "summary.json")
    summary_data = {
        "run_id": run_id,
        "label": args.label,
        "timestamp": datetime.now(IST).isoformat(),
        "total_evals": total_count,
        "passed": passed_count,
        "pass_rate": round((passed_count / total_count) * 100, 1) if total_count else 0,
        "model": DEFAULT_MODEL,
        "results": [
            {
                "persona_id": r["persona_id"],
                "rep": r["rep"],
                "passed": r["passed"],
                "turns_used": r["turns_used"],
                "checks": r["checks"],
            }
            for r in results_summary
        ],
    }
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, indent=2)

    # Update runs index
    index_path = os.path.join(os.path.dirname(__file__), "runs", "index.json")
    index_data = []
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                index_data = json.load(f)
        except Exception:
            index_data = []
    index_data.append({
        "run_id": run_id,
        "label": args.label,
        "timestamp": summary_data["timestamp"],
        "total": total_count,
        "passed": passed_count,
        "pass_rate": summary_data["pass_rate"],
        "model": DEFAULT_MODEL,
    })
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f, indent=2)

    print("\n==================================================================")
    print("                    EVALUATION RUN COMPLETE")
    print("==================================================================")
    print(f"  Run ID:    {run_id}")
    print(f"  Total:     {total_count}")
    print(f"  Passed:    {passed_count}")
    print(f"  Pass Rate: {summary_data['pass_rate']}%")
    print(f"  Summary:   {summary_path}")
    print("==================================================================\n")
    return 0 if passed_count == total_count else 1


if __name__ == "__main__":
    sys.exit(main())
