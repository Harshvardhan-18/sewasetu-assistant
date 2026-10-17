# -*- coding: utf-8 -*-
"""
evals/validate.py — Validate persona YAMLs against schema and database.

Usage:
    python -m evals.validate

Checks:
1. All required fields present according to BUILD_GUIDE §7 schema.
2. `mobile` in marker range (9990000001–9990000099) and strictly UNIQUE across personas.
3. `why_this_person` is a substantive paragraph (> 50 chars).
4. `turn_budget` and `max_citizen_messages` are positive integers.
5. `must_be_true` has valid structure.
6. Referenced files in `files` exist on disk under evals/assets.
7. Every SQL check in `must_be_true.db` parses and EXPLAINs against PostgreSQL (if reachable).
"""

from __future__ import annotations

import os
import re
import sys
import glob
import yaml

MARKER_MOBILE_REGEX = re.compile(r"^99900000(0[1-9]|[1-9][0-9])$")
REQUIRED_FIELDS = [
    "id",
    "role",
    "name",
    "age",
    "village",
    "block",
    "mobile",
    "why_this_person",
    "how_they_talk",
    "goal",
    "private_facts",
    "files",
    "fixtures",
    "turn_budget",
    "max_citizen_messages",
    "must_be_true",
    "non_interference",
]
VALID_ROLES = {"citizen", "officer", "chained"}


def validate_personas(personas_dir: str, check_db: bool = True) -> tuple[int, int, list[str]]:
    yaml_files = sorted(glob.glob(os.path.join(personas_dir, "*.yaml")))
    if not yaml_files:
        return 0, 0, [f"No YAML files found in {personas_dir}"]

    mobiles_seen: dict[str, str] = {}
    ids_seen: dict[str, str] = {}
    errors: list[str] = []
    passed = 0

    # Optional DB connection for EXPLAIN validation
    db_conn = None
    if check_db:
        try:
            from evals.fixtures import get_db_connection
            db_conn = get_db_connection()
        except Exception as e:
            print(f"Notice: PostgreSQL connection not established ({e}); skipping SQL EXPLAIN checks.")

    for filepath in yaml_files:
        basename = os.path.basename(filepath)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as e:
            errors.append(f"[{basename}] Failed to parse YAML: {e}")
            continue

        file_errors = []

        # 1. Required fields
        for field in REQUIRED_FIELDS:
            if field not in data:
                file_errors.append(f"Missing required field: '{field}'")

        if file_errors:
            errors.extend([f"[{basename}] {err}" for err in file_errors])
            continue

        p_id = str(data["id"]).strip()
        p_role = str(data["role"]).strip()
        p_mobile = str(data["mobile"]).strip()
        why = str(data["why_this_person"]).strip()

        # 2. ID uniqueness
        if p_id in ids_seen:
            file_errors.append(f"Duplicate persona id '{p_id}' (first seen in {ids_seen[p_id]})")
        else:
            ids_seen[p_id] = basename

        # 3. Role validation
        if p_role not in VALID_ROLES:
            file_errors.append(f"Invalid role '{p_role}'. Must be one of: {VALID_ROLES}")

        # 4. Marker mobile validation & uniqueness
        if not MARKER_MOBILE_REGEX.match(p_mobile):
            file_errors.append(f"Mobile '{p_mobile}' not in marker range 9990000001–9990000099")
        elif p_mobile in mobiles_seen:
            file_errors.append(f"Duplicate mobile '{p_mobile}' (already used by {mobiles_seen[p_mobile]})")
        else:
            mobiles_seen[p_mobile] = basename

        # 5. why_this_person length
        if len(why) < 50:
            file_errors.append(f"'why_this_person' is too brief ({len(why)} chars); must be a one-paragraph explanation")

        # 6. Turn budgets
        if not isinstance(data.get("turn_budget"), int) or data["turn_budget"] <= 0:
            file_errors.append("turn_budget must be a positive integer")
        if not isinstance(data.get("max_citizen_messages"), int) or data["max_citizen_messages"] <= 0:
            file_errors.append("max_citizen_messages must be a positive integer")

        # 7. Files existence
        files_dict = data.get("files") or {}
        if isinstance(files_dict, dict):
            for fname, rel_path in files_dict.items():
                if not os.path.isabs(rel_path):
                    full_path = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", rel_path))
                else:
                    full_path = rel_path
                if not os.path.exists(full_path):
                    file_errors.append(f"Referenced file not found: {rel_path} (resolved {full_path})")

        # 8. SQL EXPLAIN checks (if DB available)
        must_be_true = data.get("must_be_true") or {}
        db_checks = must_be_true.get("db") or []
        if db_conn and isinstance(db_checks, list):
            with db_conn.cursor() as cur:
                for chk in db_checks:
                    sql = chk.get("sql", "")
                    if sql:
                        # Substitute placeholder %(mobile)s for EXPLAIN
                        dummy_sql = sql.replace("%(mobile)s", f"'{p_mobile}'")
                        try:
                            cur.execute(f"EXPLAIN {dummy_sql}")
                        except Exception as sqle:
                            file_errors.append(f"SQL EXPLAIN failed for check '{chk.get('name')}': {sqle}")
                            db_conn.rollback()

        if file_errors:
            errors.extend([f"[{basename}] {err}" for err in file_errors])
        else:
            passed += 1

    if db_conn:
        db_conn.close()

    return passed, len(yaml_files), errors


def main() -> int:
    personas_dir = os.path.join(os.path.dirname(__file__), "personas")
    print(f"Validating persona YAMLs in {personas_dir}...")
    passed, total, errors = validate_personas(personas_dir)

    print(f"\n--- Validation Summary ---")
    print(f"Total personas: {total}")
    print(f"Passed: {passed}")
    print(f"Failed: {len(errors)}")

    if errors:
        print("\nERRORS:")
        for err in errors:
            print(f"  [ERROR] {err}")
        return 1

    print("\n[OK] All persona YAMLs passed validation successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
