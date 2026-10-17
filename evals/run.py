# -*- coding: utf-8 -*-
"""
evals/run.py — Pluggable Evaluation Runner for SewaSetu AI Assistant.

Supports OpenAI-compatible model backends (Groq, Gemini, OpenAI, Ollama, Together)
as well as Anthropic. Features tool calling via official MCP protocol, exponential
backoff for HTTP 429, pacing delay, resume capability, and guaranteed cleanup.

Usage:
    # List models available for configured key:
    python -m evals.run --list-models

    # Token/cost estimate only:
    python -m evals.run --suite all --reps 3 --estimate-only

    # Run single persona smoke test (C01, rep 1):
    python -m evals.run --persona C01 --reps 1 --label "smoke-test"

    # Run baseline suite with pacing and resume support:
    python -m evals.run --suite all --reps 3 --label "baseline" --pace-seconds 1.5 --max-calls 300
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
import random
import argparse
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import requests

try:
    import openai
except ImportError:
    openai = None

try:
    import anthropic
except ImportError:
    anthropic = None

from tools.oauth_client import (
    OAuthClient,
    MODERN_PROTOCOL_VERSION,
    MODERN_META,
    parse_sse_or_json,
    http_post_json,
)
from citizen_mcp.server import CITIZEN_INSTRUCTIONS
from officer_mcp.server import OFFICER_INSTRUCTIONS


def _load_env_file():
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k not in os.environ:
                            os.environ[k] = v


_load_env_file()

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


# ── Pluggable Backend Resolution ──────────────────────────────────────────────

def resolve_backend_config(role_type: str = "assistant") -> dict[str, str]:
    """
    Resolves base_url, api_key, model, and provider for assistant or simulator.
    Inherits GROQ_API_KEY, GEMINI_API_KEY, or OPENAI_API_KEY if role-specific key is omitted.
    """
    prefix = "EVAL_ASSISTANT_" if role_type == "assistant" else "EVAL_SIMULATOR_"

    custom_url = os.environ.get(f"{prefix}BASE_URL")
    custom_key = os.environ.get(f"{prefix}API_KEY")
    custom_model = os.environ.get(f"{prefix}MODEL")

    effective_key = (
        custom_key
        or os.environ.get("GROQ_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    )

    if custom_url:
        effective_url = custom_url.rstrip("/")
    elif os.environ.get("GROQ_API_KEY"):
        effective_url = "https://api.groq.com/openai/v1"
    elif os.environ.get("GEMINI_API_KEY"):
        effective_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    elif os.environ.get("OPENAI_API_KEY"):
        effective_url = "https://api.openai.com/v1"
    else:
        effective_url = ""

    if custom_model:
        effective_model = custom_model
    elif role_type == "assistant":
        effective_model = "openai/gpt-oss-20b"
    else:
        effective_model = "openai/gpt-oss-120b"

    provider = "openai_compatible" if effective_key else "unknown"

    return {
        "provider": provider,
        "base_url": effective_url,
        "api_key": effective_key,
        "model": effective_model,
    }

    # 3. OpenAI
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        return {
            "provider": "openai_compatible",
            "base_url": "https://api.openai.com/v1",
            "api_key": openai_key,
            "model": custom_model or "gpt-4o-mini",
        }

    # 4. Anthropic
    ant_key = os.environ.get("ANTHROPIC_API_KEY")
    if ant_key and not ant_key.startswith("sk-ant-..."):
        return {
            "provider": "anthropic",
            "base_url": "",
            "api_key": ant_key,
            "model": custom_model or "claude-3-5-haiku-20241022",
        }

    # Fallback to empty
    return {
        "provider": "unknown",
        "base_url": "",
        "api_key": "",
        "model": custom_model or "unknown",
    }


def list_available_models():
    """Lists available models from configured backend."""
    cfg = resolve_backend_config("assistant")
    if cfg["provider"] != "openai_compatible" or not cfg["api_key"]:
        print(f"Backend not configured or not OpenAI-compatible. Config: {cfg['provider']}")
        return

    if not openai:
        print("openai package not installed.")
        return

    client = openai.OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"])
    try:
        models = client.models.list()
        print(f"\nAvailable models at {cfg['base_url']}:")
        model_ids = sorted([m.id for m in models.data])
        for m_id in model_ids:
            rec = " [RECOMMENDED FOR TOOLS]" if any(x in m_id.lower() for x in ("70b", "flash", "gpt-4o")) else ""
            print(f"  - {m_id}{rec}")
        print()
    except Exception as e:
        print(f"Failed to list models: {e}")


# ── Global API Call Counter ───────────────────────────────────────────────────

GLOBAL_API_CALLS = 0
MAX_API_CALLS_LIMIT = 500


def increment_and_check_calls(limit: int):
    global GLOBAL_API_CALLS
    GLOBAL_API_CALLS += 1
    if GLOBAL_API_CALLS > limit:
        raise RuntimeError(
            f"HARD CALL GUARD TRIGGERED: Exceeded maximum allowed API calls ({limit}). "
            f"Stopping evaluation to protect rate limits and free tiers."
        )


def sanitize_secrets(obj: Any) -> Any:
    """Redacts tokens, passwords, and sensitive keys from stored JSON."""
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            if k in ("access_token", "refresh_token", "authorization", "code", "password", "token", "api_key"):
                cleaned[k] = "[REDACTED]"
            elif isinstance(v, (dict, list)):
                cleaned[k] = sanitize_secrets(v)
            elif isinstance(v, str) and ("Bearer " in v or "sk-ant-" in v or "gsk_" in v):
                cleaned[k] = re.sub(r"(Bearer|sk-ant-|gsk_)[A-Za-z0-9._-]+", r"\1[REDACTED]", v)
            else:
                cleaned[k] = v
        return cleaned
    elif isinstance(obj, list):
        return [sanitize_secrets(x) for x in obj]
    elif isinstance(obj, str) and ("Bearer " in obj or "sk-ant-" in obj or "gsk_" in obj):
        return re.sub(r"(Bearer|sk-ant-|gsk_)[A-Za-z0-9._-]+", r"\1[REDACTED]", obj)
    return obj


# ── Rate-Limited LLM Invocation with Exponential Backoff ─────────────────────

def call_openai_with_backoff(
    client: openai.OpenAI,
    model: str,
    messages: list[dict[str, Any]],
    tools: Optional[list[dict[str, Any]]] = None,
    max_tokens: int = 400,
    temperature: float = 0.0,
    pace_seconds: float = 1.0,
    max_calls: int = 500,
    max_retries: int = 5,
) -> Any:
    """Invokes OpenAI chat completion with exponential backoff on HTTP 429."""
    increment_and_check_calls(max_calls)

    if pace_seconds > 0:
        time.sleep(pace_seconds)

    for attempt in range(max_retries):
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

            return client.chat.completions.create(**kwargs)
        except Exception as e:
            err_str = str(e)
            is_rate_limit = "429" in err_str or "rate_limit" in err_str.lower()
            if is_rate_limit and attempt < max_retries - 1:
                # Check for Retry-After
                wait_time = 2.0 ** (attempt + 1) + random.uniform(0.1, 0.5)
                # Parse retry header if available in exception
                match = re.search(r"try again in ([0-9.]+)s", err_str)
                if match:
                    wait_time = max(wait_time, float(match.group(1)) + 0.5)
                wait_time = min(wait_time, 25.0)
                print(f"    [429 Rate Limit] Backing off for {wait_time:.1f}s (attempt {attempt + 1}/{max_retries})...")
                time.sleep(wait_time)
            else:
                raise e


# ── Tool Schema Conversion (MCP -> OpenAI) ───────────────────────────────────

def mcp_to_openai_tools(mcp_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Converts MCP tool definitions to OpenAI function tool schemas."""
    converted = []
    for t in mcp_tools:
        schema = t.get("inputSchema", {})
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        converted.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        })
    return converted


def post_mcp_request(
    url: str,
    payload: dict[str, Any],
    access_token: str,
    mcp_method: str,
    mcp_name: str = "",
) -> dict[str, Any]:
    """Posts a JSON-RPC request to MCP Streamable HTTP endpoint and parses SSE/JSON."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
        "Mcp-Method": mcp_method,
    }
    if mcp_name:
        headers["Mcp-Name"] = mcp_name

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=25)
        raw_text = resp.text
    except Exception as e:
        return {"error": {"code": -32000, "message": f"Network error calling MCP: {e}"}}

    return parse_sse_or_json(raw_text, expected_id=payload.get("id"))


def fetch_mcp_tools(canonical_url: str, access_token: str) -> list[dict[str, Any]]:
    """Queries MCP server tools/list and returns OpenAI function-call schemas."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {"_meta": MODERN_META},
    }
    body = post_mcp_request(canonical_url, payload, access_token, mcp_method="tools/list")
    raw_tools = body.get("result", {}).get("tools", [])
    return mcp_to_openai_tools(raw_tools)


def execute_tool_call(
    client: OAuthClient,
    canonical_url: str,
    access_token: str,
    tool_name: str,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    """Invokes a tool on the deployed MCP server and returns the parsed envelope."""
    call_id = int(time.time() * 1000) % 100000
    payload = {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "tools/call",
        "params": {
            "_meta": MODERN_META,
            "name": tool_name,
            "arguments": tool_input,
        },
    }
    return post_mcp_request(canonical_url, payload, access_token, mcp_method="tools/call", mcp_name=tool_name)


# ── Simulator Turn ────────────────────────────────────────────────────────────

def run_simulator_turn(
    sim_client: openai.OpenAI,
    sim_model: str,
    persona: dict[str, Any],
    conversation_history: list[dict[str, Any]],
    pace_seconds: float,
    max_calls: int,
) -> str:
    """Drives the persona simulation using the configured simulator model."""
    role = persona.get("role", "citizen")
    if role == "officer":
        system_prompt = (
            f"You are roleplaying as {persona.get('name')}, an officer reviewing pension applications.\n"
            f"Goal: {persona.get('goal')}\n"
            f"How you talk: {persona.get('how_they_talk')}\n"
            f"Private facts / inputs: {json.dumps(persona.get('private_facts', {}))}\n\n"
            f"CRITICAL RULES:\n"
            f"1. Stay completely in character. Never mention you are an AI, testing, or taking an evaluation.\n"
            f"2. Instruct the assistant according to your goal and private facts.\n"
            f"3. When your goal is achieved (e.g. decision confirmed or review finished), append [END] at the end of your message.\n"
        )
    else:
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
            f"2. Reveal private facts (DOB, bank account, IFSC, documents) ONLY when the assistant asks for them.\n"
            f"3. If the assistant asks for age proof, document, or photo: ALWAYS provide the doc_url from private facts directly (do NOT ask for a phone upload link). State your document type (e.g. voter ID) and the doc_url.\n"
            f"4. If the assistant presents preview or asks for confirmation to submit, answer clearly with 'Haan beta, sab sahi hai. Please submit kar dijiye'.\n"
            f"5. Emit [END] ONLY when:\n"
            f"   - An application number (e.g. starting with SSP...) is given to you by the assistant, OR\n"
            f"   - The assistant explicitly confirms your status question or withdrawal, OR\n"
            f"   - The assistant explicitly states that you are ineligible or refuses your request.\n"
            f"   NEVER emit [END] while the application is in progress, while answering questions, or before an application number is received!\n"
        )

    messages = [{"role": "system", "content": system_prompt}]
    recent_history = conversation_history[-8:] if len(conversation_history) > 8 else conversation_history
    for msg in recent_history:
        role_tag = "assistant" if msg["role"] == "citizen" else "user"
        messages.append({"role": role_tag, "content": msg["content"]})

    if len(messages) == 1:
        messages.append({"role": "user", "content": "Please start the conversation with your goal."})

    try:
        resp = call_openai_with_backoff(
            client=sim_client,
            model=sim_model,
            messages=messages,
            max_tokens=250,
            temperature=0.7,
            pace_seconds=pace_seconds,
            max_calls=max_calls,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"Hello, I need help with my pension application. (sim-err: {e})"


# ── Verification Checks ───────────────────────────────────────────────────────

def run_checks(
    persona: dict[str, Any],
    history: list[dict[str, Any]],
    tool_calls_made: list[dict[str, Any]],
    db_conn,
    snapshot_before: dict[str, Any],
    judge_client: Optional[openai.OpenAI],
    judge_model: str,
    pace_seconds: float,
    max_calls: int,
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
        results[f"web:{name}"] = True

    # 5. Non-interference Check
    if persona.get("non_interference", True) and snapshot_before:
        ni_passed, ni_detail = verify_non_interference(snapshot_before, [mobile])
        results["non_interference"] = ni_passed
        details["non_interference"] = ni_detail

    # 6. Judge Rubric (soft)
    judge_lines = must_be_true.get("judge", [])
    judge_scores = {}
    if judge_lines and judge_client:
        rubric_prompt = (
            "You are an impartial evaluator assessing dialogue between a citizen and a government pension assistant.\n"
            f"Transcript:\n{transcript_text}\n\n"
            f"Score each rubric criterion from 0 to 2 (0=Fail, 1=Partial, 2=Pass):\n"
            + "\n".join([f"- {line}" for line in judge_lines])
            + "\n\nOutput valid JSON format: {\"scores\": [score1, score2], \"reasoning\": \"...\"}"
        )
        try:
            j_resp = call_openai_with_backoff(
                client=judge_client,
                model=judge_model,
                messages=[{"role": "user", "content": rubric_prompt}],
                max_tokens=300,
                pace_seconds=pace_seconds,
                max_calls=max_calls,
            )
            content = j_resp.choices[0].message.content or ""
            match = re.search(r"\{.*\}", content, re.DOTALL)
            judge_scores = json.loads(match.group(0)) if match else {"raw": content}
        except Exception as e:
            judge_scores = {"error": str(e)}
    details["judge_rubric"] = judge_scores

    return results, details


# ── Run Single Persona (with Guaranteed Cleanup) ──────────────────────────────

def run_persona(
    persona: dict[str, Any],
    rep: int,
    client: OAuthClient,
    asst_client: openai.OpenAI,
    asst_config: dict[str, str],
    sim_client: openai.OpenAI,
    sim_config: dict[str, str],
    pace_seconds: float = 1.0,
    max_calls: int = 500,
    keep_data: bool = False,
) -> dict[str, Any]:
    """Executes a single persona run end-to-end with guaranteed cleanup."""
    p_id = persona.get("id", "unknown")
    role = persona.get("role", "citizen")
    mobile = persona.get("mobile", "")
    turn_budget = persona.get("turn_budget", 10)
    max_msgs = persona.get("max_citizen_messages", 10)

    print(f"  -> Running Persona {p_id} (Rep {rep}) | Role: {role} | Mobile: {mobile} ...")

    target_url = CITIZEN_URL if role == "citizen" else OFFICER_URL
    history: list[dict[str, Any]] = []
    tool_calls_made: list[dict[str, Any]] = []
    model_weaknesses: list[dict[str, str]] = []
    snapshot_before: dict[str, Any] = {}
    db_conn = None

    try:
        # Step 1: Cleanup & Setup Starting State
        cleanup_persona(mobile)
        create_persona_state(persona)
        if persona.get("non_interference", True):
            snapshot_before = snapshot_other_rows([mobile])

        # Step 2: Obtain Real OAuth Token
        if role == "citizen":
            token_result = client.obtain_citizen_token(mobile, canonical_url=CITIZEN_URL)
        elif role == "officer":
            token_result = client.obtain_officer_token(ADMIN_USER, ADMIN_PASS, canonical_url=OFFICER_URL)
        elif role == "chained":
            token_result = client.obtain_citizen_token(mobile, canonical_url=CITIZEN_URL)
        else:
            raise ValueError(f"Unknown role: {role}")

        # Step 3: Fetch MCP Tools from Deployed Server
        tools_schema = fetch_mcp_tools(target_url, token_result.access_token)

        if role in ("citizen", "chained"):
            server_instructions = CITIZEN_INSTRUCTIONS
        elif role == "officer":
            server_instructions = OFFICER_INSTRUCTIONS
        else:
            server_instructions = ""

        neutral_line = (
            "You are an assistant on a government pension portal helping the person you are chatting with. "
            "Follow all scheme rules strictly. Work case-by-case."
        )
        instructions = f"{server_instructions}\n\n{neutral_line}" if server_instructions else neutral_line

        # Step 4: Multi-Turn Conversation Loop
        turns_used = 0
        citizen_messages = 0
        start_time = time.time()

        # Initial message from persona simulator
        first_msg = run_simulator_turn(
            sim_client, sim_config["model"], persona, history, pace_seconds, max_calls
        )
        should_stop = "[END]" in first_msg
        clean_first_msg = first_msg.replace("[END]", "").strip() or "Hello"
        history.append({"role": "citizen", "content": clean_first_msg})
        citizen_messages += 1

        assistant_messages: list[dict[str, Any]] = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": clean_first_msg},
        ]

        while turns_used < turn_budget and citizen_messages <= max_msgs:
            turns_used += 1

            # Call Assistant Model
            try:
                resp = call_openai_with_backoff(
                    client=asst_client,
                    model=asst_config["model"],
                    messages=assistant_messages,
                    tools=tools_schema,
                    max_tokens=400,
                    pace_seconds=pace_seconds,
                    max_calls=max_calls,
                )
            except Exception as e:
                history.append({"role": "system", "content": f"Assistant LLM error: {e}"})
                break

            msg = resp.choices[0].message

            # Check if model made tool calls
            if msg.tool_calls:
                asst_entry: dict[str, Any] = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                }
                if msg.content:
                    asst_entry["content"] = msg.content
                assistant_messages.append(asst_entry)

                for tc in msg.tool_calls:
                    call_name = tc.function.name
                    raw_args = tc.function.arguments

                    # Parse arguments; detect malformed tool call
                    try:
                        call_args = json.loads(raw_args) if raw_args else {}
                    except Exception as json_err:
                        model_weaknesses.append({
                            "type": "MALFORMED_TOOL_ARGUMENTS",
                            "tool": call_name,
                            "raw": raw_args,
                            "error": str(json_err),
                        })
                        call_args = {}

                    tool_res = execute_tool_call(
                        client, target_url, token_result.access_token, call_name, call_args
                    )
                    tool_calls_made.append({"name": call_name, "args": call_args, "res": tool_res})

                    # Extract inner text if present for cleaner LLM context
                    tool_content = ""
                    if isinstance(tool_res, dict) and "result" in tool_res:
                        content_items = tool_res["result"].get("content", [])
                        if content_items and isinstance(content_items, list) and "text" in content_items[0]:
                            tool_content = content_items[0]["text"]
                    if not tool_content:
                        tool_content = json.dumps(tool_res)

                    assistant_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": call_name,
                        "content": tool_content,
                    })
                # Loop back to let assistant process tool results
                continue

            # Assistant produced final text response
            reply_text = (msg.content or "").strip()
            history.append({"role": "assistant", "content": reply_text})
            assistant_messages.append({"role": "assistant", "content": reply_text})

            # If previous simulator message requested stop, or assistant emitted [END], stop now
            if should_stop or "[END]" in reply_text:
                break

            # Simulator turn
            sim_reply = run_simulator_turn(
                sim_client, sim_config["model"], persona, history, pace_seconds, max_calls
            )
            should_stop = "[END]" in sim_reply
            clean_sim_reply = sim_reply.replace("[END]", "").strip() or "Thank you, that is all."
            history.append({"role": "citizen", "content": clean_sim_reply})
            citizen_messages += 1

            assistant_messages.append({"role": "user", "content": clean_sim_reply})

        duration = time.time() - start_time

        # Step 5: Verification Checks
        try:
            db_conn = get_db_connection()
        except Exception:
            pass

        check_results, check_details = run_checks(
            persona,
            history,
            tool_calls_made,
            db_conn,
            snapshot_before,
            asst_client,
            asst_config["model"],
            pace_seconds,
            max_calls,
        )

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
            "backend": asst_config.get("provider", "openai_compatible"),
            "backend_note": "Evaluated via official Python MCP client and OpenAI-compatible API (not Claude Code)",
            "assistant_model": asst_config.get("model"),
            "assistant_base_url": asst_config.get("base_url"),
            "simulator_model": sim_config.get("model"),
            "simulator_base_url": sim_config.get("base_url"),
            "model_weaknesses": model_weaknesses,
            "checks": check_results,
            "check_details": check_details,
            "tool_calls": [t["name"] for t in tool_calls_made],
            "history": history,
        }
        return sanitize_secrets(result_payload)

    finally:
        if db_conn:
            try:
                db_conn.close()
            except Exception:
                pass
        # GUARANTEED CLEANUP: delete marker rows even on failure, unless keep_data
        if not keep_data:
            try:
                cleanup_persona(mobile)
            except Exception as e:
                print(f"Notice: cleanup_persona failed in finally: {e}")


# ── Main Entrypoint ───────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="SewaSetu Pluggable Eval Runner")
    parser.add_argument("--persona", type=str, help="Specific persona ID to evaluate (e.g. C01, O1)")
    parser.add_argument("--suite", type=str, default="all", choices=["all", "citizens", "officers", "chained"])
    parser.add_argument("--reps", type=int, default=3, help="Number of repetitions per persona")
    parser.add_argument("--label", type=str, default="run", help="Label for this eval run")
    parser.add_argument("--keep", action="store_true", help="Keep DB rows after eval")
    parser.add_argument("--estimate-only", action="store_true", help="Print token/cost estimate and exit")
    parser.add_argument("--list-models", action="store_true", help="List models available for configured key")
    parser.add_argument("--pace-seconds", type=float, default=1.0, help="Delay between API calls in seconds")
    parser.add_argument("--max-calls", type=int, default=500, help="Hard maximum allowed API calls across the run")
    parser.add_argument("--resume", action="store_true", default=True, help="Skip already-passed personas in run directory")
    args = parser.parse_args()

    # Model listing mode
    if args.list_models:
        list_available_models()
        return 0

    # Resolve configs for Assistant and Simulator
    asst_config = resolve_backend_config("assistant")
    sim_config = resolve_backend_config("simulator")

    # Load persona YAMLs
    personas_dir = os.path.join(os.path.dirname(__file__), "personas")
    yaml_paths = sorted(glob.glob(os.path.join(personas_dir, "*.yaml")))
    all_personas = []
    for yp in yaml_paths:
        with open(yp, "r", encoding="utf-8") as f:
            all_personas.append(yaml.safe_load(f))

    # Target selection
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

    # Print token and cost estimates
    total_runs = len(targets) * args.reps
    print("\n==================================================================")
    print("                EVALUATION RUN CONFIGURATION")
    print("==================================================================")
    print(f"  Backend Provider:       {asst_config['provider']}")
    print(f"  Assistant Base URL:     {asst_config['base_url'] or '(native)'}")
    print(f"  Assistant Model:        {asst_config['model']}")
    print(f"  Simulator Model:        {sim_config['model']}")
    print(f"  Personas in Run:        {len(targets)}")
    print(f"  Reps per Persona:       {args.reps}")
    print(f"  Total Conversations:    {total_runs}")
    print(f"  Pacing Delay:           {args.pace_seconds}s per call")
    print(f"  Hard Call Guard:        {args.max_calls} max calls")
    print(f"  Estimated Tokens:       ~{total_runs * 14_000:,}")
    print(f"  Estimated Cost (USD):   ~${(total_runs * 14_000 / 1_000_000) * 0.35:.4f}")
    print("==================================================================\n")

    if args.estimate_only:
        return 0

    if not asst_config["api_key"]:
        print("ERROR: No API key found! Please set GROQ_API_KEY, GEMINI_API_KEY, or EVAL_ASSISTANT_API_KEY in .env")
        return 1

    if not openai:
        print("ERROR: openai package is required for OpenAI-compatible backends. Run: pip install openai")
        return 1

    # Initialize OpenAI clients (max_retries=0 so call_openai_with_backoff manages retries)
    asst_client = openai.OpenAI(base_url=asst_config["base_url"], api_key=asst_config["api_key"], max_retries=0)
    sim_client = openai.OpenAI(base_url=sim_config["base_url"], api_key=sim_config["api_key"], max_retries=0)
    oauth_client = OAuthClient(portal_base=PUBLIC_BASE_URL, smsgw_base=SMSGW_URL)

    run_id = f"run_{datetime.now(IST).strftime('%Y%m%d_%H%M%S')}_{args.label}"
    runs_dir = os.path.join(os.path.dirname(__file__), "runs", run_id)
    os.makedirs(runs_dir, exist_ok=True)

    print(f"Starting Evaluation Run: {run_id}")
    print(f"Results directory: {runs_dir}\n")

    results_summary = []
    passed_count = 0
    total_count = 0

    for persona in targets:
        p_id = persona.get("id")
        for rep in range(1, args.reps + 1):
            out_file = os.path.join(runs_dir, f"{p_id}-rep{rep}.json")

            # Resume check: skip already-passed runs
            if args.resume and os.path.exists(out_file):
                try:
                    with open(out_file, "r", encoding="utf-8") as f:
                        prev = json.load(f)
                    if prev.get("passed"):
                        print(f"  [RESUME] Persona {p_id} (Rep {rep}) already PASSED. Skipping.")
                        results_summary.append(prev)
                        total_count += 1
                        passed_count += 1
                        continue
                except Exception:
                    pass

            total_count += 1
            run_data = run_persona(
                persona=persona,
                rep=rep,
                client=oauth_client,
                asst_client=asst_client,
                asst_config=asst_config,
                sim_client=sim_client,
                sim_config=sim_config,
                pace_seconds=args.pace_seconds,
                max_calls=args.max_calls,
                keep_data=args.keep,
            )

            if run_data.get("passed"):
                passed_count += 1
            results_summary.append(run_data)

            # Persist individual run result
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(run_data, f, indent=2, default=str)

    # Persist summary
    summary_path = os.path.join(runs_dir, "summary.json")
    summary_data = {
        "run_id": run_id,
        "label": args.label,
        "timestamp": datetime.now(IST).isoformat(),
        "total_evals": total_count,
        "passed": passed_count,
        "pass_rate": round((passed_count / total_count) * 100, 1) if total_count else 0,
        "backend": asst_config.get("provider"),
        "backend_note": "Evaluated via official Python MCP client and OpenAI-compatible API (not Claude Code)",
        "assistant_model": asst_config.get("model"),
        "assistant_base_url": asst_config.get("base_url"),
        "simulator_model": sim_config.get("model"),
        "simulator_base_url": sim_config.get("base_url"),
        "total_api_calls_used": GLOBAL_API_CALLS,
        "results": [
            {
                "persona_id": r["persona_id"],
                "rep": r["rep"],
                "passed": r["passed"],
                "turns_used": r["turns_used"],
                "checks": r["checks"],
                "model_weaknesses": r.get("model_weaknesses", []),
            }
            for r in results_summary
        ],
    }
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, indent=2, default=str)

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
        "backend": asst_config.get("provider"),
        "assistant_model": asst_config.get("model"),
        "simulator_model": sim_config.get("model"),
    })
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f, indent=2, default=str)

    print("\n==================================================================")
    print("                    EVALUATION RUN COMPLETE")
    print("==================================================================")
    print(f"  Run ID:    {run_id}")
    print(f"  Total:     {total_count}")
    print(f"  Passed:    {passed_count}")
    print(f"  Pass Rate: {summary_data['pass_rate']}%")
    print(f"  API Calls: {GLOBAL_API_CALLS} / {args.max_calls}")
    print(f"  Summary:   {summary_path}")
    print("==================================================================\n")
    return 0 if passed_count == total_count else 1


if __name__ == "__main__":
    sys.exit(main())
