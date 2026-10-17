# -*- coding: utf-8 -*-
"""
evals/report.py — Terminal report generator for evaluation runs.

Usage:
    # Print report for latest run:
    python -m evals.report

    # Print report for specific run:
    python -m evals.report --run run_20261017_110000_baseline
"""

from __future__ import annotations

import os
import sys
import json
import glob
import argparse


def generate_report(run_dir: str):
    summary_file = os.path.join(run_dir, "summary.json")
    if not os.path.exists(summary_file):
        print(f"Error: summary.json not found in {run_dir}")
        return

    with open(summary_file, "r", encoding="utf-8") as f:
        summary = json.load(f)

    print("\n================================================================================")
    print(f"  EVALUATION RUN REPORT: {summary.get('run_id')} ({summary.get('label')})")
    print("================================================================================")
    print(f"  Timestamp: {summary.get('timestamp')}")
    print(f"  Model:     {summary.get('model')}")
    print(f"  Total:     {summary.get('total_evals')} | Passed: {summary.get('passed')} | Pass Rate: {summary.get('pass_rate')}%")
    print("--------------------------------------------------------------------------------")
    print(f"{'PERSONA':<10} | {'REP':<5} | {'STATUS':<8} | {'TURNS':<6} | {'FAILING CHECKS'}")
    print("-" * 80)

    for item in summary.get("results", []):
        p_id = item.get("persona_id")
        rep = item.get("rep")
        passed = item.get("passed")
        turns = item.get("turns_used")
        checks = item.get("checks", {})
        status = "[PASS]" if passed else "[FAIL]"

        failing = [k for k, v in checks.items() if not v]
        fail_str = ", ".join(failing) if failing else "-"

        print(f"{p_id:<10} | {rep:<5} | {status:<8} | {turns:<6} | {fail_str}")

    print("================================================================================\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Eval Report Generator")
    parser.add_argument("--run", type=str, help="Specific run ID (default: latest run)")
    args = parser.parse_args()

    runs_base = os.path.join(os.path.dirname(__file__), "runs")
    if not os.path.exists(runs_base):
        print(f"No runs directory found at {runs_base}")
        return 1

    if args.run:
        target_dir = os.path.join(runs_base, args.run)
    else:
        run_dirs = sorted(glob.glob(os.path.join(runs_base, "run_*")))
        if not run_dirs:
            print("No evaluation runs found.")
            return 1
        target_dir = run_dirs[-1]

    generate_report(target_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
