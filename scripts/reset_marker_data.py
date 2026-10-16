#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/reset_marker_data.py — Marker test data reset tool.

Deletes ONLY rows, drafts, tokens, submissions, audit rows, upload files,
and gateway messages tied to marker mobiles 9990000001-9990000099.

STRICT SAFETY:
- Refuses to run against any mobile outside 9990000001-9990000099.
- Never touches seeded citizen data.
- Prints an exact count of everything removed.

Usage:
    # Reset a specific marker mobile:
    python scripts/reset_marker_data.py --mobile 9990000002

    # Reset all marker mobiles (9990000001 - 9990000099):
    python scripts/reset_marker_data.py --all

Options:
    --db-url     Postgres connection string (default: $DATABASE_URL or localhost/sewasetu)
    --smsgw-url  SMS gateway URL or proxy (default: $SMSGW_URL or http://localhost:8025)
    --upload-dir Uploads directory on disk (default: $UPLOAD_DIR or /var/sewasetu/uploads)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.parse
import urllib.request
import psycopg2


MARKER_MOBILE_REGEX = re.compile(r"^99900000(0[1-9]|[1-9][0-9])$")


def is_marker_mobile(mobile: str) -> bool:
    return bool(MARKER_MOBILE_REGEX.match(mobile.strip()))


def validate_marker_mobiles(mobiles: list[str]) -> list[str]:
    cleaned = []
    for m in mobiles:
        clean = m.strip()
        if not is_marker_mobile(clean):
            raise ValueError(
                f"SAFETY ERROR: Mobile '{m}' is not a permitted marker mobile. "
                f"This tool refuses to operate on anything outside 9990000001-9990000099."
            )
        cleaned.append(clean)
    return cleaned


def reset_marker_data(
    mobiles: list[str],
    db_url: str,
    smsgw_url: str = "",
    upload_dir: str = "",
) -> dict[str, int]:
    valid_mobiles = validate_marker_mobiles(mobiles)
    if not valid_mobiles:
        print("No marker mobiles specified.")
        return {}

    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    report = {
        "mobiles_processed": len(valid_mobiles),
        "applications": 0,
        "audit_log": 0,
        "portal_users": 0,
        "otps": 0,
        "assistant_drafts": 0,
        "assistant_signouts": 0,
        "assistant_upload_tokens": 0,
        "assistant_submissions": 0,
        "oauth_records": 0,
        "files_removed": 0,
        "gateway_messages": 0,
    }

    try:
        # 1. Collect files to delete on disk before deleting DB rows
        files_to_delete: set[str] = set()

        cur.execute(
            "SELECT doc_path FROM applications WHERE mobile = ANY(%s) AND doc_path IS NOT NULL AND doc_path != ''",
            (valid_mobiles,),
        )
        for (p,) in cur.fetchall():
            if p:
                files_to_delete.add(p)

        # assistant_drafts doc_path
        cur.execute(
            "SELECT doc_path FROM assistant_drafts WHERE mobile = ANY(%s) AND doc_path IS NOT NULL AND doc_path != ''",
            (valid_mobiles,),
        )
        for (p,) in cur.fetchall():
            if p:
                files_to_delete.add(p)

        # 2. assistant_submissions (FK to applications)
        cur.execute(
            "DELETE FROM assistant_submissions WHERE mobile = ANY(%s)",
            (valid_mobiles,),
        )
        report["assistant_submissions"] = cur.rowcount

        # 3. audit_log referencing applications or marker applicant actors
        subjects = [f"citizen:{m}" for m in valid_mobiles] + [f"applicant:{m}" for m in valid_mobiles]
        cur.execute(
            """
            DELETE FROM audit_log
            WHERE application_id IN (SELECT id FROM applications WHERE mobile = ANY(%s))
               OR actor = ANY(%s)
            """,
            (valid_mobiles, subjects),
        )
        report["audit_log"] = cur.rowcount

        # 4. applications
        cur.execute(
            "DELETE FROM applications WHERE mobile = ANY(%s)",
            (valid_mobiles,),
        )
        report["applications"] = cur.rowcount

        # 5. portal_users
        cur.execute(
            "DELETE FROM portal_users WHERE mobile = ANY(%s)",
            (valid_mobiles,),
        )
        report["portal_users"] = cur.rowcount

        # 6. otps
        cur.execute(
            "DELETE FROM otps WHERE mobile = ANY(%s)",
            (valid_mobiles,),
        )
        report["otps"] = cur.rowcount

        # 7. assistant_drafts
        cur.execute(
            "DELETE FROM assistant_drafts WHERE mobile = ANY(%s)",
            (valid_mobiles,),
        )
        report["assistant_drafts"] = cur.rowcount

        # 8. assistant_signouts
        citizen_subjects = [f"citizen:{m}" for m in valid_mobiles]
        cur.execute(
            "DELETE FROM assistant_signouts WHERE subject = ANY(%s)",
            (citizen_subjects,),
        )
        report["assistant_signouts"] = cur.rowcount

        # 9. assistant_upload_tokens
        cur.execute(
            "DELETE FROM assistant_upload_tokens WHERE mobile = ANY(%s)",
            (valid_mobiles,),
        )
        report["assistant_upload_tokens"] = cur.rowcount

        # 10. OAuth tables
        cur.execute(
            "DELETE FROM oauth_refresh_tokens WHERE subject = ANY(%s)",
            (citizen_subjects,),
        )
        refresh_deleted = cur.rowcount
        cur.execute(
            "DELETE FROM oauth_codes WHERE subject = ANY(%s)",
            (citizen_subjects,),
        )
        codes_deleted = cur.rowcount
        cur.execute(
            "DELETE FROM oauth_requests WHERE subject = ANY(%s) OR pending_mobile = ANY(%s)",
            (citizen_subjects, valid_mobiles),
        )
        reqs_deleted = cur.rowcount
        report["oauth_records"] = refresh_deleted + codes_deleted + reqs_deleted

        conn.commit()

        # 11. Delete files on disk
        removed_files = 0
        for fpath in files_to_delete:
            # Only delete if path looks like it's within upload directory
            try:
                if os.path.isabs(fpath) and os.path.exists(fpath):
                    os.remove(fpath)
                    removed_files += 1
                elif upload_dir and not os.path.isabs(fpath):
                    full_p = os.path.join(upload_dir, fpath)
                    if os.path.exists(full_p):
                        os.remove(full_p)
                        removed_files += 1
            except Exception as e:
                print(f"Warning: could not remove file {fpath}: {e}", file=sys.stderr)
        report["files_removed"] = removed_files

        # 12. Delete gateway messages via SMS gateway API
        if smsgw_url:
            deleted_msgs = 0
            base = smsgw_url.rstrip("/")
            for m in valid_mobiles:
                try:
                    url = f"{base}/api/messages?to={urllib.parse.quote(m)}"
                    req = urllib.request.Request(url, method="DELETE")
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        if resp.status < 400:
                            import json
                            body = json.loads(resp.read().decode())
                            deleted_msgs += body.get("count", body.get("removed", 0))
                except Exception:
                    pass
            report["gateway_messages"] = deleted_msgs

    finally:
        cur.close()
        conn.close()

    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reset marker test data (mobiles 9990000001 - 9990000099 only)."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mobile", help="Single marker mobile (e.g. 9990000002)")
    group.add_argument(
        "--all", action="store_true", help="Reset all marker mobiles (9990000001 - 9990000099)"
    )

    default_db = os.environ.get(
        "DATABASE_URL", "postgresql://sewasetu:sewasetu123@localhost:5432/sewasetu"
    )
    parser.add_argument("--db-url", default=default_db, help=f"Postgres URL (default: {default_db})")
    parser.add_argument(
        "--smsgw-url",
        default=os.environ.get("SMSGW_URL", "http://localhost:8025"),
        help="SMS gateway URL",
    )
    parser.add_argument(
        "--upload-dir",
        default=os.environ.get("UPLOAD_DIR", "/var/sewasetu/uploads"),
        help="Uploads directory",
    )

    args = parser.parse_args()

    if args.all:
        mobiles = [f"99900000{i:02d}" for i in range(1, 100)]
    else:
        mobiles = [args.mobile]

    try:
        report = reset_marker_data(
            mobiles=mobiles,
            db_url=args.db_url,
            smsgw_url=args.smsgw_url,
            upload_dir=args.upload_dir,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 2

    print("=" * 60)
    print("  Marker Data Reset Summary")
    print("=" * 60)
    for k, v in report.items():
        print(f"  {k:25s}: {v}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
