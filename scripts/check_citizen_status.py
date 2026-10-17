#!/usr/bin/env python3
"""Check get_my_status for a mobile via citizen MCP."""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.oauth_client import OAuthClient, http_post_json, MODERN_META, MODERN_PROTOCOL_VERSION

def main():
    mobile = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "9990000067"
    host = os.environ.get("HOST", os.environ.get("PUBLIC_BASE_URL", "https://sewasetu-hv.koreacentral.cloudapp.azure.com")).rstrip("/")
    smsgw = os.environ.get("SMSGW_URL", f"{host}/__gateway").rstrip("/")

    client = OAuthClient(portal_base=host, smsgw_base=smsgw)
    tok = client.obtain_citizen_token(mobile, canonical_url=f"{host}/citizen/mcp")

    # Call get_my_status via Modern Streamable HTTP MCP
    headers = {
        "Authorization": f"Bearer {tok.access_token}",
        "Mcp-Method": "tools/call",
        "Accept": "text/event-stream, application/json",
        "Content-Type": "application/json",
    }
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "get_my_status",
            "arguments": {},
            "_meta": MODERN_META,
        },
    }
    status, res, _ = http_post_json(f"{host}/citizen/mcp", payload, headers=headers)
    print(f"GET_MY_STATUS RESULT FOR {mobile}:")
    print(json.dumps(res, indent=2))

if __name__ == "__main__":
    main()
