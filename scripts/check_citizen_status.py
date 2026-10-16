#!/usr/bin/env python3
"""Check get_my_status for a mobile via citizen MCP."""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.oauth_smoke import obtain_token, call_tool

def main():
    mobile = sys.argv[1] if len(sys.argv) > 1 else "9990000067"
    host = os.environ.get("HOST", "https://sewasetu-hv.koreacentral.cloudapp.azure.com")
    smsgw = os.environ.get("SMSGW_URL", "http://localhost:8025")

    tok = obtain_token(
        portal_base=host,
        canonical_url=f"{host}/citizen/mcp",
        role="citizen",
        mobile=mobile,
        smsgw_base=smsgw,
    )
    res = call_tool(f"{host}/citizen/mcp", tok.access_token, "get_my_status")
    print(f"GET_MY_STATUS RESULT FOR {mobile}:")
    print(json.dumps(res, indent=2))

if __name__ == "__main__":
    main()
