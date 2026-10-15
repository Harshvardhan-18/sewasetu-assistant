"""Officer MCP Server — /officer/mcp (scope: officer).

Accepts ordinary `officer:*` tokens **and** `kind=program` tokens issued to
the eval harness; both are configured in PortalTokenVerifier.
"""

import os
import sys

# Ensure mcp_common is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mcp_common import create_server, current_subject

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
CANONICAL_URL = os.environ.get("CANONICAL_URL", f"{PUBLIC_BASE_URL}/officer/mcp")

app = create_server(
    name="sewasetu-officer-mcp",
    canonical_url=CANONICAL_URL,
    role="officer",
    instructions=(
        "You are the official Sewa Setu Officer Assistant. "
        "You help pension officers review the queue of pending applications, "
        "inspect individual cases (eligibility checks, document verification, "
        "IFSC/account validation, deemed-approval countdowns), and record "
        "approve/reject decisions with audit-compliant notes."
    ),
)


@app.tool(name="whoami", description="Echo the authenticated officer subject")
def whoami() -> str:
    """Return the authenticated officer subject."""
    sub = current_subject()
    return f"Officer caller: {sub}"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
