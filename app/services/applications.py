# -*- coding: utf-8 -*-
"""
app/services/applications.py — Single source of truth for application rules.

Every channel that creates or changes an application (web form, internal API,
any future batch import) MUST go through these functions so that no channel
admits an application another channel would refuse, and audit rows always have
the same actor/format.

Public surface:
  submit_application(cur, mobile, data, doc_path, actor_note="")
      -> {"ok": True,  "id": int, "application_no": str}
      -> {"ok": False, "errors": [str]}

  withdraw_application(cur, mobile, application_no_or_id)
      -> {"ok": True,  "application_no": str}
      -> {"ok": False, "error": str}

  decide_application(cur, app_id, new_status, actor_username, note)
      -> {"ok": True,  "application_no": str}
      -> {"ok": False, "error": str}

  eligibility_for(dob_str_or_date) -> dict
  verify_checks(application_dict)   -> dict
  scheme_facts()                    -> dict
"""

import os
import re
import unicodedata
import configparser
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Config (same source as app.py)
# ---------------------------------------------------------------------------

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_config = configparser.ConfigParser()
_config.read(os.path.join(_BASE_DIR, "config", "app.ini"))

MIN_AGE = _config.getint("pension", "min_age")
SLA_DAYS = _config.getint("pension", "sla_days")
PENSION_AMOUNT = _config.getint("pension", "amount")
SCHEME_DEADLINE = datetime.strptime(
    _config.get("pension", "scheme_deadline"), "%Y-%m-%d %H:%M"
)
ACCEPTED_DOCUMENTS = [
    d.strip()
    for d in _config.get("pension", "accepted_documents", fallback="").split(",")
    if d.strip()
]

BLOCKS = ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]

ACTIVE_STATUSES = ("PENDING", "APPROVED", "DEEMED_APPROVED")


def _now():
    """Portal clock — always IST, tzinfo stripped for Postgres compatibility."""
    return datetime.now(IST).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Helpers (shared with app.py)
# ---------------------------------------------------------------------------

def sanitize(value, maxlen=100):
    if value is None:
        return ""
    return value.strip()[:maxlen]


def parse_dob(raw):
    try:
        return datetime.strptime((raw or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def age_on(dob, on):
    """Exact age in completed years on a given date."""
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def _mask_account(acct):
    """Return last-4 digits of a bank account, masked: xxxxxx1234."""
    if not acct or len(acct) < 4:
        return acct
    return "x" * (len(acct) - 4) + acct[-4:]


# ---------------------------------------------------------------------------
# Scheme facts — single source for the API and templates
# ---------------------------------------------------------------------------

def scheme_facts():
    """Return all scheme-level facts from config and constants.

    This is the authoritative source; never hard-code these values in
    MCP server descriptions or tool results.
    """
    now = _now()
    deadline = SCHEME_DEADLINE
    hours_left = max(0, int((deadline - now).total_seconds() // 3600))
    window_open = now <= deadline
    return {
        "amount_rs_per_month": PENSION_AMOUNT,
        "min_age": MIN_AGE,
        "sla_days": SLA_DAYS,
        "window_open": window_open,
        "window_closes_at": deadline.isoformat(timespec="minutes"),
        "hours_left": hours_left if window_open else 0,
        "accepted_documents": ACCEPTED_DOCUMENTS,
        "valid_blocks": BLOCKS,
        "description": (
            "Old Age Pension of Rs. %d/month for residents of Purvanchal "
            "aged %d or above. Applications processed within %d days; "
            "unprocessed applications are deemed approved by law."
            % (PENSION_AMOUNT, MIN_AGE, SLA_DAYS)
        ),
    }


# ---------------------------------------------------------------------------
# Validate — one function, one set of rules for every channel
# ---------------------------------------------------------------------------

def validate_application(data, doc_path):
    """Validate a complete application. Returns (errors, cleaned).

    `errors` is a list of plain-language problems (empty when valid).
    `cleaned` holds normalised values ready for create_application().

    This is the ONLY place rules live. The web form and the internal API
    both call this. Do not duplicate rule logic in callers.
    """
    errors = []
    cleaned = {
        "applicant_name": sanitize(data.get("applicant_name")),
        "village": (data.get("village") or "").strip(),
        "block": (data.get("block") or "").strip(),
        "gender": data.get("gender") or "",
        "marital_status": data.get("marital_status") or "",
        "husband_name": (data.get("husband_name") or "").strip(),
        "husband_employer": "",  # no longer collected
        "bank_account": re.sub(r"[\s-]", "", data.get("bank_account") or ""),
        "ifsc": re.sub(r"[\s-]", "", (data.get("ifsc") or "")).upper(),
        "doc_path": doc_path or "",
        "dob": None,
    }

    # ── Required fields ──────────────────────────────────────────────────
    for label, key in (
        ("full name", "applicant_name"),
        ("village", "village"),
        ("block", "block"),
        ("bank account", "bank_account"),
    ):
        if not cleaned[key]:
            errors.append("%s is required" % label)

    # ── Block validation ─────────────────────────────────────────────────
    if cleaned["block"] and cleaned["block"] not in BLOCKS:
        errors.append("block must be one of: " + ", ".join(BLOCKS))

    # ── Bank account format ───────────────────────────────────────────────
    if cleaned["bank_account"] and not re.fullmatch(
        r"\d{9,18}", cleaned["bank_account"]
    ):
        errors.append("bank account number must be 9 to 18 digits")

    # ── IFSC format ───────────────────────────────────────────────────────
    if cleaned["ifsc"] and not re.fullmatch(
        r"[A-Z]{4}0[A-Z0-9]{6}", cleaned["ifsc"]
    ):
        errors.append("IFSC must be 11 characters, e.g. SBIN0003077")

    # ── Date of birth ─────────────────────────────────────────────────────
    dob = parse_dob(data.get("dob"))
    if dob is None:
        errors.append("date of birth must be given as DD/MM/YYYY")
    else:
        cleaned["dob"] = dob
        today = _now().date()

        if age_on(dob, today) < MIN_AGE:
            errors.append(
                "applicant must be %d years of age or above" % MIN_AGE
            )

    # ── Document required ─────────────────────────────────────────────────
    if not cleaned["doc_path"]:
        errors.append("age proof document is required")

    # ── Window ───────────────────────────────────────────────────────────
    if _now() > SCHEME_DEADLINE:
        errors.append("the application window has closed")

    return errors, cleaned


# ---------------------------------------------------------------------------
# active_application — shared check
# ---------------------------------------------------------------------------

def active_application(cur, mobile):
    """The applicant's current live application, if any."""
    cur.execute(
        "SELECT id, application_no, status FROM applications "
        "WHERE mobile = %s AND status IN %s "
        "ORDER BY submitted_at DESC LIMIT 1",
        (mobile, ACTIVE_STATUSES),
    )
    return cur.fetchone()


# ---------------------------------------------------------------------------
# create_application — low-level insert (called inside submit_application)
# ---------------------------------------------------------------------------

def _write_audit(cur, app_id, action, actor, note=""):
    cur.execute(
        "INSERT INTO audit_log (application_id, action, actor, note, at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (app_id, action, actor, note, datetime.now()),
    )


def _hash_password(p):
    import hashlib
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def _new_application_no():
    import random
    return "SSP" + datetime.now().strftime("%y") + str(random.randint(100000, 999999))


def create_application(cur, mobile, cleaned, actor_note=""):
    """Insert a validated application; ensure the status-portal account exists.

    The caller commits. Returns (id, application_no).
    """
    app_no = _new_application_no()
    cur.execute(
        """INSERT INTO applications
           (application_no, applicant_name, mobile, dob, gender, marital_status,
            husband_name, husband_employer, village, block, bank_account, ifsc,
            doc_path, status, submitted_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)
           RETURNING id""",
        (
            app_no, cleaned["applicant_name"], mobile, cleaned["dob"],
            cleaned["gender"], cleaned["marital_status"], cleaned["husband_name"],
            cleaned["husband_employer"], cleaned["village"], cleaned["block"],
            cleaned["bank_account"], cleaned["ifsc"], cleaned["doc_path"],
            datetime.now(),
        ),
    )
    new_id = cur.fetchone()[0]

    # Status portal account; password is DOB as DDMMYYYY per dept. circular
    portal_pass = cleaned["dob"].strftime("%d%m%Y")
    cur.execute(
        "SELECT count(*) FROM portal_users WHERE mobile = %s", (mobile,)
    )
    if cur.fetchone()[0] == 0:
        cur.execute(
            "INSERT INTO portal_users (mobile, password_hash) VALUES (%s,%s)",
            (mobile, _hash_password(portal_pass)),
        )
    _write_audit(cur, new_id, "SUBMIT", "applicant:%s" % mobile, note=actor_note)
    return new_id, app_no


# ---------------------------------------------------------------------------
# submit_application — the ONE path for creating an application
# ---------------------------------------------------------------------------

def submit_application(cur, mobile, data, doc_path, actor_note=""):
    """Validate, lock, check for duplicates, create and audit in one call.

    The caller is responsible for committing (and sending SMS after commit).

    Returns:
        {"ok": True,  "id": int, "application_no": str}
        {"ok": False, "errors": [str]}
    """
    errors, cleaned = validate_application(data, doc_path)
    if errors:
        return {"ok": False, "errors": errors}

    # Advisory lock prevents races on the same mobile number
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (mobile,))

    existing = active_application(cur, mobile)
    if existing:
        return {
            "ok": False,
            "errors": [
                "Application %s (%s) already exists for this mobile number. "
                "A pending application can be withdrawn from the status portal "
                "if you need to apply afresh." % (existing[1], existing[2])
            ],
        }

    # actor_note goes into the audit note field only (provenance, not in actor)
    new_id, app_no = create_application(cur, mobile, cleaned, actor_note=actor_note)
    return {"ok": True, "id": new_id, "application_no": app_no}


# ---------------------------------------------------------------------------
# withdraw_application
# ---------------------------------------------------------------------------

def withdraw_application(cur, mobile, application_no_or_id):
    """Withdraw a PENDING application owned by mobile.

    Accepts either an application_no (str) or an id (int).
    Returns:
        {"ok": True,  "application_no": str, "app_id": int}
        {"ok": False, "error": str}
    """
    # Resolve to id
    if isinstance(application_no_or_id, int):
        cur.execute(
            "SELECT id, application_no, mobile, status FROM applications WHERE id=%s",
            (application_no_or_id,),
        )
    else:
        cur.execute(
            "SELECT id, application_no, mobile, status FROM applications "
            "WHERE application_no=%s",
            (application_no_or_id,),
        )
    row = cur.fetchone()
    if not row:
        return {"ok": False, "error": "application not found"}
    app_id, app_no, app_mobile, status = row
    if app_mobile != mobile:
        return {"ok": False, "error": "application does not belong to this mobile"}
    if status != "PENDING":
        return {"ok": False, "error": "only a pending application can be withdrawn"}

    cur.execute(
        "UPDATE applications SET status='WITHDRAWN', decided_at=%s, "
        "decided_by='APPLICANT' WHERE id=%s AND status='PENDING' RETURNING application_no",
        (datetime.now(), app_id),
    )
    if not cur.fetchone():
        return {"ok": False, "error": "only a pending application can be withdrawn"}
    _write_audit(cur, app_id, "WITHDRAW", "applicant:%s" % mobile)
    return {"ok": True, "application_no": app_no, "app_id": app_id}


# ---------------------------------------------------------------------------
# decide_application — officer approve or reject
# ---------------------------------------------------------------------------

def decide_application(cur, app_id, new_status, actor_username, note):
    """Approve or reject a PENDING application.

    new_status must be 'APPROVED' or 'REJECTED'.
    note is REQUIRED and is written to the audit log.

    Returns:
        {"ok": True,  "application_no": str}
        {"ok": False, "error": str}
    """
    if new_status not in ("APPROVED", "REJECTED"):
        return {"ok": False, "error": "decision must be APPROVED or REJECTED"}

    cur.execute(
        "UPDATE applications SET status=%s, decided_at=%s, decided_by=%s "
        "WHERE id=%s AND status='PENDING' RETURNING application_no",
        (new_status, datetime.now(), actor_username, app_id),
    )
    row = cur.fetchone()
    if not row:
        return {"ok": False, "error": "only a pending application can be decided"}
    _write_audit(
        cur, app_id, new_status, "admin:%s" % actor_username, note
    )
    return {"ok": True, "application_no": row[0]}


# ---------------------------------------------------------------------------
# eligibility_for — used by citizen MCP check_eligibility tool
# ---------------------------------------------------------------------------

def eligibility_for(dob):
    """Return detailed eligibility information for a date of birth.

    dob: date object or 'DD/MM/YYYY' string.
    """
    if isinstance(dob, str):
        dob = parse_dob(dob)
    if dob is None:
        return {"eligible": False, "advice": "Date of birth could not be parsed (use DD/MM/YYYY)."}

    today = _now().date()
    age = age_on(dob, today)

    # When does / did the applicant turn MIN_AGE?
    try:
        turns_60_on = dob.replace(year=dob.year + MIN_AGE)
    except ValueError:
        # Feb 29 edge case
        turns_60_on = dob.replace(year=dob.year + MIN_AGE, day=28)

    window_closes = SCHEME_DEADLINE.date()
    window_open = _now() <= SCHEME_DEADLINE
    eligible = age >= MIN_AGE
    window_closes_before_turning_60 = turns_60_on > window_closes

    if eligible and window_open:
        advice = "Eligible. You may apply now."
    elif eligible and not window_open:
        advice = "Eligible, but the application window has closed."
    elif not eligible and window_open:
        if window_closes_before_turning_60:
            advice = (
                "Not yet %d. You turn %d on %s, which is after the window "
                "closes on %s. Unfortunately you cannot apply in this cycle."
                % (MIN_AGE, MIN_AGE, turns_60_on.strftime("%d %b %Y"),
                   window_closes.strftime("%d %b %Y"))
            )
        else:
            advice = (
                "Not yet %d. You turn %d on %s. "
                "Come back on that date to apply before the window closes."
                % (MIN_AGE, MIN_AGE, turns_60_on.strftime("%d %b %Y"))
            )
    else:
        advice = "Not eligible and the window has closed."

    return {
        "age": age,
        "eligible": eligible,
        "turns_60_on": turns_60_on.isoformat(),
        "window_open": window_open,
        "window_closes_at": SCHEME_DEADLINE.isoformat(timespec="minutes"),
        "window_closes_before_turning_60": window_closes_before_turning_60,
        "advice": advice,
    }


# ---------------------------------------------------------------------------
# verify_checks — officer checklist (BUILD_GUIDE §4.2)
# ---------------------------------------------------------------------------

def verify_checks(cur, app):
    """Run the officer verification checklist against an application.

    `app` is a dict with application fields (from a DB row).
    Returns a dict with boolean per check and a recommendation (never a decision).

    Flags that are hard failures:
      - age < MIN_AGE at submission
      - invalid IFSC format
      - invalid bank account format
      - no document on file
      - invalid block

    Review-only flags (never auto-reject):
      - same name + DOB elsewhere (could be coincidence)
      - same bank account on another active application (couples share accounts)
    """
    mobile = app["mobile"]
    app_id = app["id"]

    checks = {}

    # Age at submission
    dob = app["dob"]
    if isinstance(dob, str):
        dob = parse_dob(dob)
    submitted_at = app.get("submitted_at") or _now()
    if isinstance(submitted_at, str):
        submitted_at = datetime.fromisoformat(submitted_at)
    age_at_submission = age_on(dob, submitted_at.date()) if dob else None
    turns_60 = None
    if dob:
        try:
            turns_60 = dob.replace(year=dob.year + MIN_AGE).isoformat()
        except ValueError:
            turns_60 = dob.replace(year=dob.year + MIN_AGE, day=28).isoformat()
    checks["age_at_submission"] = {
        "ok": age_at_submission is not None and age_at_submission >= MIN_AGE,
        "age_at_submission": age_at_submission,
        "turns_60_on": turns_60,
        "note": (
            "Age at submission: %s. "
            "Applicant turns %d on %s." % (age_at_submission, MIN_AGE, turns_60)
        ) if age_at_submission is not None else "DOB could not be parsed.",
    }

    # IFSC format
    ifsc = (app.get("ifsc") or "").strip().upper()
    checks["ifsc_format"] = {
        "ok": bool(re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc)),
        "ifsc": ifsc,
    }

    # Bank account format
    acct = re.sub(r"[\s-]", "", app.get("bank_account") or "")
    checks["account_format"] = {
        "ok": bool(re.fullmatch(r"\d{9,18}", acct)),
        "account": _mask_account(acct),
    }

    # Document on file
    checks["document_present"] = {
        "ok": bool(app.get("doc_path")),
        "doc_path": app.get("doc_path") or None,
    }

    # Valid block
    checks["block_valid"] = {
        "ok": app.get("block") in BLOCKS,
        "block": app.get("block"),
    }

    # Another active application on this mobile
    cur.execute(
        "SELECT application_no, status FROM applications "
        "WHERE mobile=%s AND status IN %s AND id!=%s",
        (mobile, ACTIVE_STATUSES, app_id),
    )
    other_active = cur.fetchall()
    checks["other_active_application"] = {
        "ok": len(other_active) == 0,
        "other": [{"application_no": r[0], "status": r[1]} for r in other_active],
    }

    # Same bank account on another active application
    cur.execute(
        "SELECT application_no, mobile, status FROM applications "
        "WHERE bank_account=%s AND status IN %s AND id!=%s",
        (acct, ACTIVE_STATUSES, app_id),
    )
    same_acct = cur.fetchall()
    checks["same_account_elsewhere"] = {
        "review_only": True,  # couples share accounts — never auto-reject
        "flagged": len(same_acct) > 0,
        "others": [
            {"application_no": r[0], "mobile": r[1][-4:], "status": r[2]}
            for r in same_acct
        ],
    }

    # Same name + DOB elsewhere (coincidence is common in synthetic data)
    name = app.get("applicant_name", "")
    cur.execute(
        "SELECT application_no, mobile FROM applications "
        "WHERE applicant_name=%s AND dob=%s AND id!=%s LIMIT 5",
        (name, dob, app_id),
    )
    same_person = cur.fetchall()
    checks["same_name_dob_elsewhere"] = {
        "review_only": True,
        "flagged": len(same_person) > 0,
        "count": len(same_person),
    }

    # Days to deemed approval
    submitted_at_dt = app.get("submitted_at") or datetime.now()
    if isinstance(submitted_at_dt, str):
        submitted_at_dt = datetime.fromisoformat(submitted_at_dt)
    days_pending = (datetime.now() - submitted_at_dt).days
    days_to_deemed = max(0, SLA_DAYS - days_pending)
    checks["sla"] = {
        "days_pending": days_pending,
        "days_to_deemed_approval": days_to_deemed,
        "overdue": days_to_deemed == 0,
    }

    # Hard-failure checks (review_only=True ones are advisory)
    hard_fails = [
        k for k, v in checks.items()
        if not v.get("review_only") and not v.get("ok", True)
    ]
    recommendation = "APPROVE" if not hard_fails else "REVIEW"

    return {
        "checks": checks,
        "hard_failures": hard_fails,
        "recommendation": recommendation,
        "note": (
            "No issues found." if not hard_fails
            else "Issues found: " + ", ".join(hard_fails) + ". "
            + "Officer must acknowledge flags in the decision note."
        ),
    }
