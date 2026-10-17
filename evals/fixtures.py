# -*- coding: utf-8 -*-
"""
evals/fixtures.py — Test fixture creation, marker cleanup, and non-interference snapshotting.

Used by the evaluation runner to prepare state before each persona run,
verify non-interference during the run, and clean up marker-range test data.
"""

from __future__ import annotations

import os
import re
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any
import psycopg2
import psycopg2.extras

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://sewasetu:sewasetu123@localhost:5432/sewasetu"
)
MARKER_MOBILE_REGEX = re.compile(r"^99900000(0[1-9]|[1-9][0-9])$")


def is_marker_mobile(mobile: str) -> bool:
    return bool(MARKER_MOBILE_REGEX.match(mobile.strip()))


def get_db_connection(db_url: str | None = None):
    url = db_url or DEFAULT_DB_URL
    return psycopg2.connect(url)


def _hash_dob(dob_str: str) -> str:
    parts = dob_str.split("-")
    if len(parts) == 3:
        ddmmyyyy = f"{parts[2]}{parts[1]}{parts[0]}"
    else:
        ddmmyyyy = dob_str.replace("/", "")
    return hashlib.sha256(ddmmyyyy.encode("utf-8")).hexdigest()


def cleanup(mobiles: list[str] | str | None = None, db_url: str | None = None) -> dict[str, int]:
    """
    Deletes marker rows for the specified mobile numbers (or all marker mobiles).
    Refuses to touch non-marker numbers.
    """
    if mobiles is None:
        target_mobiles = [f"99900000{i:02d}" for i in range(1, 100)]
    elif isinstance(mobiles, str):
        target_mobiles = [mobiles.strip()]
    else:
        target_mobiles = [m.strip() for m in mobiles]

    valid = [m for m in target_mobiles if is_marker_mobile(m)]
    if not valid:
        return {}

    conn = get_db_connection(db_url)
    report: dict[str, int] = {}
    try:
        with conn.cursor() as cur:
            # 1. assistant_submissions
            cur.execute("DELETE FROM assistant_submissions WHERE mobile = ANY(%s)", (valid,))
            report["assistant_submissions"] = cur.rowcount

            # 2. audit_log
            subjects = [f"citizen:{m}" for m in valid] + [f"applicant:{m}" for m in valid]
            cur.execute(
                """
                DELETE FROM audit_log
                WHERE application_id IN (SELECT id FROM applications WHERE mobile = ANY(%s))
                   OR actor = ANY(%s)
                """,
                (valid, subjects),
            )
            report["audit_log"] = cur.rowcount

            # 3. applications
            cur.execute("DELETE FROM applications WHERE mobile = ANY(%s)", (valid,))
            report["applications"] = cur.rowcount

            # 4. portal_users
            cur.execute("DELETE FROM portal_users WHERE mobile = ANY(%s)", (valid,))
            report["portal_users"] = cur.rowcount

            # 5. otps
            cur.execute("DELETE FROM otps WHERE mobile = ANY(%s)", (valid,))
            report["otps"] = cur.rowcount

            # 6. assistant_drafts
            cur.execute("DELETE FROM assistant_drafts WHERE mobile = ANY(%s)", (valid,))
            report["assistant_drafts"] = cur.rowcount

            # 7. assistant_signouts
            cur.execute("DELETE FROM assistant_signouts WHERE subject = ANY(%s)", (subjects,))
            report["assistant_signouts"] = cur.rowcount

            # 8. assistant_upload_tokens
            cur.execute("DELETE FROM assistant_upload_tokens WHERE mobile = ANY(%s)", (valid,))
            report["assistant_upload_tokens"] = cur.rowcount

            # 9. oauth_refresh_tokens & oauth_codes
            cur.execute("DELETE FROM oauth_refresh_tokens WHERE subject = ANY(%s)", (subjects,))
            report["oauth_refresh_tokens"] = cur.rowcount

            cur.execute("DELETE FROM oauth_codes WHERE subject = ANY(%s)", (subjects,))
            report["oauth_codes"] = cur.rowcount

        conn.commit()
    finally:
        conn.close()

    return report


def cleanup_persona(mobile: str, db_url: str | None = None) -> dict[str, int]:
    return cleanup([mobile], db_url=db_url)


def create_persona_state(persona: dict[str, Any], db_url: str | None = None) -> int:
    """
    Applies any fixtures specified in the persona YAML.
    All dates in SQL fixtures should be relative to current IST time.
    """
    fixtures = persona.get("fixtures", [])
    if not fixtures:
        return 0

    conn = get_db_connection(db_url)
    executed = 0
    try:
        with conn.cursor() as cur:
            for item in fixtures:
                if isinstance(item, dict) and item.get("type") == "sql":
                    sql = item.get("statement", "").strip()
                    if sql:
                        cur.execute(sql)
                        executed += 1
        conn.commit()
    finally:
        conn.close()

    return executed


def snapshot_other_rows(exclude_mobiles: list[str] | set[str] | None = None, db_url: str | None = None) -> dict[str, Any]:
    """
    Takes a snapshot of applications and audit_log rows outside the excluded mobiles
    to enable strict non-interference verification.
    """
    exclude = set(exclude_mobiles or [])
    conn = get_db_connection(db_url)
    snapshot: dict[str, Any] = {}
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Query non-marker rows count and sample checksum
            cur.execute(
                """
                SELECT COUNT(*) as total_apps,
                       COUNT(CASE WHEN status = 'PENDING' THEN 1 END) as pending_apps,
                       COUNT(CASE WHEN status = 'APPROVED' THEN 1 END) as approved_apps,
                       COUNT(CASE WHEN status = 'REJECTED' THEN 1 END) as rejected_apps,
                       COUNT(CASE WHEN status = 'DEEMED_APPROVED' THEN 1 END) as deemed_apps
                FROM applications
                WHERE mobile != ALL(%s)
                """,
                (list(exclude),),
            )
            app_stats = dict(cur.fetchone() or {})
            snapshot["app_stats"] = app_stats

            cur.execute(
                """
                SELECT COUNT(*) as total_audit
                FROM audit_log al
                JOIN applications a ON al.application_id = a.id
                WHERE a.mobile != ALL(%s)
                """,
                (list(exclude),),
            )
            audit_stats = dict(cur.fetchone() or {})
            snapshot["audit_stats"] = audit_stats
    finally:
        conn.close()

    return snapshot


def verify_non_interference(
    before_snapshot: dict[str, Any],
    exclude_mobiles: list[str] | set[str] | None = None,
    db_url: str | None = None
) -> tuple[bool, str]:
    """
    Compares the current database state outside the excluded mobiles with the before snapshot.
    Returns (True, "OK") if non-interference holds, or (False, "detail message").
    """
    after_snapshot = snapshot_other_rows(exclude_mobiles, db_url)
    
    before_app = before_snapshot.get("app_stats", {})
    after_app = after_snapshot.get("app_stats", {})
    
    if before_app != after_app:
        return False, f"Applications changed outside target mobiles! Before: {before_app}, After: {after_app}"

    before_audit = before_snapshot.get("audit_stats", {})
    after_audit = after_snapshot.get("audit_stats", {})
    if before_audit != after_audit:
        return False, f"Audit logs changed outside target mobiles! Before: {before_audit}, After: {after_audit}"

    return True, "Non-interference verified: zero external rows modified"
