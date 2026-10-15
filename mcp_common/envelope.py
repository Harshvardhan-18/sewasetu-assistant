"""Result helper for standard tool responses.

Enforces the canonical tool result envelope:
{
  "ok": true/false,
  "message_for_user": "Short plain sentence(s) the assistant can say almost verbatim.",
  "data": { ... },
  "problems": [ { "field": "ifsc", "plain": "IFSC must be 11 characters, e.g. SBIN0003077" } ],
  "next_step": "What the assistant should do or ask next."
}

Also:
- Renders both structured content and a compact text version.
- Enforces a ~6,000 character maximum length (paginates/truncates long lists in data).
"""

import copy
import json
from typing import Any

MAX_ENVELOPE_CHARS = 6000


def render_text_summary(env: dict[str, Any]) -> str:
    """Render a compact, plain-language text summary suitable for small model assistants."""
    lines = []
    status_label = "OK" if env.get("ok") else "ATTENTION"
    lines.append(f"[{status_label}] {env.get('message_for_user', '')}")

    problems = env.get("problems") or []
    if problems:
        lines.append("Problems to resolve:")
        for p in problems:
            if isinstance(p, dict):
                field = p.get("field", "")
                plain = p.get("plain") or p.get("message") or str(p)
                prefix = f"- {field}: " if field else "- "
                lines.append(f"{prefix}{plain}")
            else:
                lines.append(f"- {p}")

    next_step = env.get("next_step")
    if next_step:
        lines.append(f"Next step: {next_step}")

    return "\n".join(lines)


def _bound_size(result: dict[str, Any], max_chars: int = MAX_ENVELOPE_CHARS) -> dict[str, Any]:
    """Ensure serialized JSON stays comfortably under max_chars by truncating lists in data."""
    serialized = json.dumps(result, ensure_ascii=False)
    if len(serialized) <= max_chars:
        return result

    # Clone data so we don't mutate caller data
    bounded = copy.deepcopy(result)
    data = bounded.get("data")
    if not isinstance(data, dict):
        return bounded

    # Identify lists in data to truncate
    for key, val in list(data.items()):
        if isinstance(val, list) and len(val) > 3:
            total = len(val)
            # Binary search / step-down to fit
            for limit in [10, 5, 2, 1]:
                if limit < total:
                    data[key] = val[:limit]
                    data[f"_{key}_pagination"] = {
                        "total_items": total,
                        "shown_items": limit,
                        "truncated": True,
                        "note": f"Showing first {limit} of {total} items due to size limits.",
                    }
                    if len(json.dumps(bounded, ensure_ascii=False)) <= max_chars:
                        break

    # If still too large, truncate long string values in data
    serialized = json.dumps(bounded, ensure_ascii=False)
    if len(serialized) > max_chars:
        for key, val in list(data.items()):
            if isinstance(val, str) and len(val) > 500:
                data[key] = val[:500] + "... [truncated]"

    return bounded


def envelope(
    ok: bool,
    message_for_user: str,
    data: dict[str, Any] | None = None,
    problems: list[dict[str, Any]] | None = None,
    next_step: str | None = None,
    max_chars: int = MAX_ENVELOPE_CHARS,
) -> dict[str, Any]:
    """Create a standardized tool result envelope with size bounding and text summary."""
    clean_data = dict(data) if data is not None else {}
    clean_problems = list(problems) if problems is not None else []
    clean_next_step = str(next_step) if next_step is not None else ""

    result = {
        "ok": bool(ok),
        "message_for_user": str(message_for_user),
        "data": clean_data,
        "problems": clean_problems,
        "next_step": clean_next_step,
    }

    # Render compact text version
    result["text_summary"] = render_text_summary(result)

    # Size-bound total serialized payload
    return _bound_size(result, max_chars=max_chars)
