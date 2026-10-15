"""Unit tests for result helper envelope (Section 4).

Tests:
- Correct canonical keys: ok, message_for_user, data, problems, next_step, text_summary
- Both structured data and compact text rendering
- Size bounding under ~6,000 characters by truncating/paginating long lists in data
- Pagination metadata injected when lists truncated
"""

import json

from mcp_common.envelope import envelope, render_text_summary


def test_standard_envelope_structure():
    res = envelope(
        ok=True,
        message_for_user="Application submitted successfully.",
        data={"application_no": "APP-2026-00123"},
        problems=[],
        next_step="Keep your application number safe.",
    )

    assert res["ok"] is True
    assert res["message_for_user"] == "Application submitted successfully."
    assert res["data"] == {"application_no": "APP-2026-00123"}
    assert res["problems"] == []
    assert res["next_step"] == "Keep your application number safe."
    assert "text_summary" in res
    assert "[OK] Application submitted successfully." in res["text_summary"]
    assert "Next step: Keep your application number safe." in res["text_summary"]


def test_envelope_with_problems_renders_text_summary():
    problems = [
        {"field": "ifsc", "plain": "IFSC must be 11 characters, e.g. SBIN0003077"},
        {"field": "dob", "plain": "date of birth must be given as DD/MM/YYYY"},
    ]
    res = envelope(
        ok=False,
        message_for_user="Please correct the validation errors.",
        problems=problems,
        next_step="Fix the IFSC and DOB fields.",
    )

    assert res["ok"] is False
    assert len(res["problems"]) == 2
    summary = res["text_summary"]
    assert "[ATTENTION] Please correct the validation errors." in summary
    assert "- ifsc: IFSC must be 11 characters" in summary
    assert "- dob: date of birth must be given" in summary
    assert "Next step: Fix the IFSC and DOB fields." in summary


def test_envelope_bounds_size_under_6000_chars():
    # Construct a huge list of application rows
    large_list = [
        {
            "id": i,
            "application_no": f"APP-2026-{i:06d}",
            "name": f"Citizen Applicant {i}",
            "village": "Majuli Island North Riverside Block",
            "block": "Sonari",
            "audit_trail": [{"action": "SUBMIT", "actor": "applicant:9876543210", "note": "Initial submission"}],
        }
        for i in range(200)
    ]

    res = envelope(
        ok=True,
        message_for_user="Found 200 applications.",
        data={"applications": large_list},
        next_step="Review the listed applications.",
    )

    serialized = json.dumps(res, ensure_ascii=False)
    assert len(serialized) <= 6000

    # Verify pagination metadata was added
    data = res["data"]
    assert len(data["applications"]) < 200
    pagination = data.get("_applications_pagination")
    assert pagination is not None
    assert pagination["total_items"] == 200
    assert pagination["truncated"] is True
