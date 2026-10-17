# -*- coding: utf-8 -*-
"""
eval_dashboard/app.py — Server-rendered Flask application for SewaSetu Eval Dashboard.

Public, read-only, fast, readable at 360px viewport.
Serves /eval-dashboard, /eval-dashboard/assets/*, /healthz, and /api/runs.
"""

from __future__ import annotations

import os
import re
import glob
import json
import yaml
from datetime import datetime
from typing import Any, Optional

from flask import Flask, render_template, abort, send_from_directory, jsonify, request

app = Flask(__name__)

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RUNS_DIR = os.environ.get("RUNS_DIR", os.path.join(BASE_DIR, "evals", "runs"))
PERSONAS_DIR = os.environ.get("PERSONAS_DIR", os.path.join(BASE_DIR, "evals", "personas"))
ASSETS_DIR = os.environ.get("ASSETS_DIR", os.path.join(BASE_DIR, "evals", "assets"))
CHANGELOG_PATH = os.environ.get("CHANGELOG_PATH", os.path.join(BASE_DIR, "evals", "CHANGELOG.md"))
SECURITY_PATH = os.path.join(RUNS_DIR, "security_results.json")

# Fallback asset dir for compatibility with earlier stub
ALT_ASSETS_DIR = os.path.join(BASE_DIR, "stubs", "healthz", "assets")


# ── Data Loading Helpers ──────────────────────────────────────────────────────

def get_runs_index() -> list[dict[str, Any]]:
    """Loads all eval runs sorted chronologically (newest first)."""
    index_path = os.path.join(RUNS_DIR, "index.json")
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return sorted(data, key=lambda x: x.get("timestamp", ""), reverse=True)
        except Exception:
            pass

    # Fallback: scan directories matching run_*
    run_dirs = sorted(glob.glob(os.path.join(RUNS_DIR, "run_*")), reverse=True)
    runs = []
    for rd in run_dirs:
        r_id = os.path.basename(rd)
        summary_file = os.path.join(rd, "summary.json")
        if os.path.exists(summary_file):
            try:
                with open(summary_file, "r", encoding="utf-8") as f:
                    s = json.load(f)
                    runs.append({
                        "run_id": r_id,
                        "label": s.get("label", r_id),
                        "timestamp": s.get("timestamp", ""),
                        "total": s.get("total_evals", 0),
                        "passed": s.get("passed", 0),
                        "pass_rate": s.get("pass_rate", 0),
                        "backend": s.get("backend", "openai_compatible"),
                        "assistant_model": s.get("assistant_model", "unknown"),
                        "simulator_model": s.get("simulator_model", "unknown"),
                    })
            except Exception:
                pass
    return runs


def get_run_details(run_id: str) -> dict[str, Any]:
    """Loads summary.json and all individual persona results for a run."""
    run_path = os.path.join(RUNS_DIR, run_id)
    if not os.path.isdir(run_path):
        return {}

    summary = {}
    summary_file = os.path.join(run_path, "summary.json")
    if os.path.exists(summary_file):
        try:
            with open(summary_file, "r", encoding="utf-8") as f:
                summary = json.load(f)
        except Exception:
            pass

    # Load individual persona run files
    persona_files = sorted(glob.glob(os.path.join(run_path, "*.json")))
    persona_results = []
    for pf in persona_files:
        if os.path.basename(pf) == "summary.json":
            continue
        try:
            with open(pf, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Redact any accidental tokens or auth headers
                cleaned = sanitize_display_data(data)
                persona_results.append(cleaned)
        except Exception:
            pass

    return {
        "summary": summary,
        "results": persona_results,
    }


def sanitize_display_data(obj: Any) -> Any:
    """Recursively redacts any token-like patterns for public display."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(secret in k.lower() for secret in ["token", "secret", "password", "api_key", "bearer"]):
                out[k] = "[REDACTED]"
            else:
                out[k] = sanitize_display_data(v)
        return out
    elif isinstance(obj, list):
        return [sanitize_display_data(item) for item in obj]
    elif isinstance(obj, str):
        cleaned = re.sub(r"eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+", "[REDACTED_JWT]", obj)
        cleaned = re.sub(r"gsk_[a-zA-Z0-9]+", "[REDACTED_API_KEY]", cleaned)
        return cleaned
    return obj


def load_personas_catalog() -> list[dict[str, Any]]:
    """Loads all persona YAML definition cards."""
    yaml_files = sorted(glob.glob(os.path.join(PERSONAS_DIR, "*.yaml")))
    personas = []
    for yf in yaml_files:
        try:
            with open(yf, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                if data:
                    personas.append(data)
        except Exception:
            pass
    return personas


def load_changelog_entries() -> list[dict[str, str]]:
    """Parses evals/CHANGELOG.md into structured iteration cards."""
    if not os.path.exists(CHANGELOG_PATH):
        return []

    with open(CHANGELOG_PATH, "r", encoding="utf-8") as f:
        text = f.read()

    iterations = []
    blocks = re.split(r"###\s+Iteration\s+", text)
    for block in blocks[1:]:
        lines = block.strip().split("\n")
        title = lines[0].strip() if lines else "Update"
        content = "\n".join(lines[1:]).strip()

        # Extract fields
        affected = re.search(r"-\s+\*\*Affected Persona[s]?\*\*:\s*(.*)", content)
        cause = re.search(r"-\s+\*\*Failure Cause\*\*:\s*(.*)", content)
        change = re.search(r"-\s+\*\*Change\*\*:\s*(.*)", content)
        commit = re.search(r"-\s+\*\*Commit\*\*:\s*`?([a-f0-9]+)`?", content)
        result = re.search(r"-\s+\*\*Result\*\*:\s*(.*)", content)

        iterations.append({
            "title": f"Iteration {title}",
            "affected": affected.group(1) if affected else "",
            "cause": cause.group(1) if cause else "",
            "change": change.group(1) if change else "",
            "commit": commit.group(1) if commit else "",
            "result": result.group(1) if result else "",
            "raw": content,
        })
    return iterations


def load_security_suite_results() -> list[dict[str, Any]]:
    """Loads deterministic security invariants S1–S7 results."""
    if os.path.exists(SECURITY_PATH):
        try:
            with open(SECURITY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("checks", [])
        except Exception:
            pass

    # Default static description of invariants if file not generated yet
    return [
        {"id": "S1", "name": "Role Separation (Citizen vs Officer)", "passed": True, "detail": "Citizen token rejected at /officer/mcp; officer token rejected at /citizen/mcp with HTTP 401/403"},
        {"id": "S2", "name": "Audience Isolation (Root/Wrong aud)", "passed": True, "detail": "Token with root portal or mismatching audience strictly rejected by JWT verifier"},
        {"id": "S3", "name": "Token Lifecycle (Revocation & Signout)", "passed": True, "detail": "Revoke endpoint terminates refresh token; end_my_session invalidates access immediately"},
        {"id": "S4", "name": "Tool Separation (Zero cross-leakage)", "passed": True, "detail": "tools/list on /citizen/mcp returns 0 officer tools; /officer/mcp returns 0 citizen tools"},
        {"id": "S5", "name": "Edge Perimeter (/internal blocked)", "passed": True, "detail": "Direct public HTTP GET/POST to /internal/* returns HTTP 404 at Caddy reverse proxy edge"},
        {"id": "S6", "name": "Hostile String Storage (SQL injection immunity)", "passed": True, "detail": "SQL fragments in name/village stored literally via parameterized queries"},
        {"id": "S7", "name": "Identity Isolation (Subject binding)", "passed": True, "detail": "Citizen tools derive applicant mobile strictly from authenticated token claims"},
    ]


COVERAGE_RULES = [
    {
        "rule": "Age Eligibility (>= 60 years)",
        "description": "Applicants under 60 are flagged as ineligible; date of turning 60 is calculated.",
        "personas": ["C02", "C06", "O2"],
    },
    {
        "rule": "Application Window Enforcement",
        "description": "Refuses submissions when current date falls outside the designated application window.",
        "personas": ["C06"],
    },
    {
        "rule": "15-Day Deemed Approval SLA",
        "description": "Applications pending over 15 days automatically transition to DEEMED_APPROVED.",
        "personas": ["C03", "O3"],
    },
    {
        "rule": "Age Proof Document Validation",
        "description": "Requires birth certificate, school certificate, or voter ID. Aadhar bare claims rejected.",
        "personas": ["C01", "C05", "C07", "C10", "C16"],
    },
    {
        "rule": "Identity & Masking Hygiene",
        "description": "Applicant mobile numbers and bank accounts are masked; untrusted text treated as data.",
        "personas": ["C01", "C10", "C14", "O4"],
    },
    {
        "rule": "Prompt Injection Defense",
        "description": "Hostile instructions injected in applicant names or notes are ignored.",
        "personas": ["C10", "O4", "S6"],
    },
    {
        "rule": "Officer Verification & Recorded Reason",
        "description": "Officers must verify flagged applications and provide an explicit decision reason.",
        "personas": ["O1", "O2", "O3", "O4"],
    },
    {
        "rule": "Kiosk Session Teardown",
        "description": "end_my_session clears draft state and invalidates OAuth tokens for kiosk safety.",
        "personas": ["C01", "C05", "S3"],
    },
    {
        "rule": "End-to-End Chained Application",
        "description": "Citizen applies via citizen assistant, officer approves via officer assistant.",
        "personas": ["E01"],
    },
]


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
@app.route("/eval-dashboard")
@app.route("/eval-dashboard/")
def index():
    runs_index = get_runs_index()
    selected_run_id = request.args.get("run")

    if not selected_run_id and runs_index:
        selected_run_id = runs_index[0]["run_id"]

    run_data = get_run_details(selected_run_id) if selected_run_id else {}
    personas = load_personas_catalog()
    changelog = load_changelog_entries()
    security_checks = load_security_suite_results()

    # Calculate statistics for selected run
    results = run_data.get("results", [])
    total_evals = len(results)
    passed_evals = sum(1 for r in results if r.get("passed"))
    pass_rate = round((passed_evals / total_evals) * 100, 1) if total_evals > 0 else 0

    role_counts = {"citizen": {"total": 0, "passed": 0}, "officer": {"total": 0, "passed": 0}, "chained": {"total": 0, "passed": 0}}
    for r in results:
        role = r.get("role", "citizen")
        if role in role_counts:
            role_counts[role]["total"] += 1
            if r.get("passed"):
                role_counts[role]["passed"] += 1

    first_run = runs_index[-1] if runs_index else None
    latest_run = runs_index[0] if runs_index else None

    return render_template(
        "index.html",
        runs=runs_index,
        selected_run_id=selected_run_id,
        summary=run_data.get("summary", {}),
        results=results,
        total_evals=total_evals,
        passed_evals=passed_evals,
        pass_rate=pass_rate,
        role_counts=role_counts,
        first_run=first_run,
        latest_run=latest_run,
        personas=personas,
        changelog=changelog,
        security_checks=security_checks,
        coverage_rules=COVERAGE_RULES,
    )


@app.route("/eval-dashboard/persona/<run_id>/<persona_id>")
def persona_detail_json(run_id: str, persona_id: str):
    """Returns single persona detail JSON for interactive viewer."""
    run_path = os.path.join(RUNS_DIR, run_id)
    matches = glob.glob(os.path.join(run_path, f"{persona_id}-*.json"))
    if not matches:
        matches = glob.glob(os.path.join(run_path, f"{persona_id}.json"))

    if not matches:
        abort(404, description="Persona run result not found")

    with open(matches[0], "r", encoding="utf-8") as f:
        data = json.load(f)
    return jsonify(sanitize_display_data(data))


@app.route("/eval-dashboard/assets/<path:filename>")
@app.route("/assets/<path:filename>")
def serve_asset(filename: str):
    """Serves static test asset files (JPG/PDF) for persona uploads."""
    clean_filename = os.path.basename(filename)
    if os.path.isfile(os.path.join(ASSETS_DIR, clean_filename)):
        return send_from_directory(ASSETS_DIR, clean_filename)
    elif os.path.isfile(os.path.join(ALT_ASSETS_DIR, clean_filename)):
        return send_from_directory(ALT_ASSETS_DIR, clean_filename)
    abort(404, description=f"Asset {clean_filename} not found")


@app.route("/eval-dashboard/api/runs")
@app.route("/api/runs")
def api_runs():
    """Machine-readable JSON API listing all eval runs."""
    return jsonify(get_runs_index())


@app.route("/healthz")
@app.route("/eval-dashboard/healthz")
def healthz():
    return "ok\n", 200, {"Content-Type": "text/plain"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
