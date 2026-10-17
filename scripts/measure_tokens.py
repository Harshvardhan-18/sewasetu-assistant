# -*- coding: utf-8 -*-
"""
scripts/measure_tokens.py — Measures and prints prompt tokens of one request
(server instructions + tool definitions) per server.
"""

import json
import tiktoken
from citizen_mcp.server import CITIZEN_INSTRUCTIONS, app as citizen_app
from officer_mcp.server import OFFICER_INSTRUCTIONS, app as officer_app

enc = tiktoken.get_encoding("cl100k_base")

def get_tools_schema(mcp_server):
    schemas = []
    # ServerAppWrapper wraps mcp_server
    server = getattr(mcp_server, "mcp_server", mcp_server)
    tools = getattr(server._tool_manager, "_tools", {})
    for name, tool in tools.items():
        if name == "whoami":
            continue
        params_schema = tool.parameters.model_dump() if hasattr(tool.parameters, "model_dump") else {}
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": tool.description or "",
                "parameters": params_schema,
            }
        })
    return schemas

def measure_surface(name, instructions, tools_schema):
    neutral_line = (
        "You are an assistant on a government pension portal helping the person you are chatting with. "
        "Follow all scheme rules strictly. Work case-by-case."
    )
    full_system = f"{instructions}\n\n{neutral_line}"
    tools_json = json.dumps(tools_schema, indent=2)

    sys_tokens = len(enc.encode(full_system))
    tools_tokens = len(enc.encode(tools_json))
    total_tokens = sys_tokens + tools_tokens

    print(f"=== {name} Server ===")
    print(f"  System Instructions tokens: {sys_tokens}")
    print(f"  Tools ({len(tools_schema)} tools) Schema tokens: {tools_tokens}")
    print(f"  Total Prompt Surface tokens: {total_tokens}")
    print(f"  Status: {'[BELOW 3,500 CAP]' if total_tokens <= 3500 else '[EXCEEDS 3,500 CAP - TIGHTENING REQUIRED]'}")
    print()
    return total_tokens

if __name__ == "__main__":
    print("Measuring MCP Prompt Token Surface (cl100k_base)...\n")
    c_tools = get_tools_schema(citizen_app)
    c_tokens = measure_surface("Citizen MCP (/citizen/mcp)", CITIZEN_INSTRUCTIONS, c_tools)

    o_tools = get_tools_schema(officer_app)
    o_tokens = measure_surface("Officer MCP (/officer/mcp)", OFFICER_INSTRUCTIONS, o_tools)
