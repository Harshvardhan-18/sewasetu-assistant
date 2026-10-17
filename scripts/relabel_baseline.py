# -*- coding: utf-8 -*-
"""
scripts/relabel_baseline.py
Relabels run_20261017_125634_baseline as 'incomplete: quota-aborted'
and classifies the 17 rate-limit truncated runs as status: ERROR (not FAIL).
"""

import os
import glob
import json

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RUN_DIR = os.path.join(BASE_DIR, "evals", "runs", "run_20261017_125634_baseline")
INDEX_PATH = os.path.join(BASE_DIR, "evals", "runs", "index.json")

def main():
    if not os.path.isdir(RUN_DIR):
        print(f"Error: Run dir not found: {RUN_DIR}")
        return 1

    summary_file = os.path.join(RUN_DIR, "summary.json")
    with open(summary_file, "r", encoding="utf-8") as f:
        summary = json.load(f)

    summary["label"] = "incomplete: quota-aborted"
    summary["status"] = "quota_aborted"

    error_list = []
    updated_results = []
    passed_cnt = 0
    failed_cnt = 0
    error_cnt = 0

    json_files = sorted(glob.glob(os.path.join(RUN_DIR, "*.json")))
    for jf in json_files:
        if os.path.basename(jf) == "summary.json":
            continue
        with open(jf, "r", encoding="utf-8") as f:
            data = json.load(f)

        pid = data.get("persona_id")
        rep = data.get("rep", 1)

        # Check if history ended with 429
        history = data.get("history", [])
        last_msg = history[-1].get("content", "") if history else ""
        has_429 = "429" in last_msg or "rate limit" in last_msg.lower() or "tokens per day" in last_msg.lower()

        if has_429:
            data["status"] = "ERROR"
            data["passed"] = False
            data["is_error"] = True
            data["error_type"] = "quota_exhaustion"
            data["error_reason"] = "Groq Tokens-Per-Day (TPD) quota exhausted (HTTP 429)"
            data["checks"] = {}
            data["check_details"] = {
                "aborted_reason": "Groq Tokens-Per-Day (TPD) quota exhausted (HTTP 429)"
            }
            error_cnt += 1
            error_list.append({
                "persona_id": pid,
                "rep": rep,
                "error_type": "quota_exhaustion",
                "error_reason": "Groq Tokens-Per-Day (TPD) quota exhausted (HTTP 429)",
            })
        else:
            if data.get("passed"):
                data["status"] = "PASS"
                data["is_error"] = False
                passed_cnt += 1
            else:
                data["status"] = "FAIL"
                data["is_error"] = False
                failed_cnt += 1

        with open(jf, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)

        updated_results.append({
            "persona_id": pid,
            "rep": rep,
            "status": data["status"],
            "passed": data["passed"],
            "is_error": data.get("is_error", False),
            "error_type": data.get("error_type"),
            "error_reason": data.get("error_reason"),
            "turns_used": data.get("turns_used", 1),
            "checks": data.get("checks", {}),
            "model_weaknesses": data.get("model_weaknesses", []),
        })

    valid_evals = passed_cnt + failed_cnt
    pass_rate = round((passed_cnt / valid_evals) * 100, 1) if valid_evals > 0 else 0.0

    summary["passed"] = passed_cnt
    summary["failed"] = failed_cnt
    summary["errors"] = error_cnt
    summary["valid_evals"] = valid_evals
    summary["pass_rate"] = pass_rate
    summary["error_list"] = error_list
    summary["results"] = updated_results

    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)

    # Update index.json
    if os.path.exists(INDEX_PATH):
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            index_data = json.load(f)
        for entry in index_data:
            if entry.get("run_id") == "run_20261017_125634_baseline":
                entry["label"] = "incomplete: quota-aborted"
                entry["status"] = "quota_aborted"
                entry["passed"] = passed_cnt
                entry["failed"] = failed_cnt
                entry["errors"] = error_cnt
                entry["valid_evals"] = valid_evals
                entry["pass_rate"] = pass_rate
        with open(INDEX_PATH, "w", encoding="utf-8") as f:
            json.dump(index_data, f, indent=2, default=str)

    print(f"Relabeled run_20261017_125634_baseline:")
    print(f"  Passed: {passed_cnt}")
    print(f"  Failed: {failed_cnt}")
    print(f"  Errors: {error_cnt}")
    print(f"  Valid Evals: {valid_evals}")
    print(f"  Pass Rate: {pass_rate}%")
    return 0

if __name__ == "__main__":
    main()
