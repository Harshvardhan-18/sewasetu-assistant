"""Citizen MCP Server — /citizen/mcp (scope: citizen)."""

import os
import sys

# Ensure mcp_common is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mcp_common import create_server, current_subject

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
CANONICAL_URL = os.environ.get("CANONICAL_URL", f"{PUBLIC_BASE_URL}/citizen/mcp")

app = create_server(
    name="sewasetu-citizen-mcp",
    canonical_url=CANONICAL_URL,
    role="citizen",
    instructions=(
        "You are the official Sewa Setu Citizen Pension Assistant. "
        "You help senior citizens and their family members check eligibility, "
        "prepare drafts, attach age-proof documents, preview, and submit "
        "pension applications under the Assam Old Age Pension Scheme."
    ),
)


@app.tool(name="whoami", description="Echo the authenticated citizen subject")
def whoami() -> str:
    """Return the authenticated citizen subject."""
    sub = current_subject()
    return f"Citizen caller: {sub}"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
