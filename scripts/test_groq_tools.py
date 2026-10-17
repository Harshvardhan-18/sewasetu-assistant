import os
from evals.run import resolve_backend_config
import openai

cfg = resolve_backend_config("assistant")
client = openai.OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"])

dummy_tools = [
    {
        "type": "function",
        "function": {
            "name": "check_eligibility",
            "description": "Checks pension age eligibility for DOB",
            "parameters": {
                "type": "object",
                "properties": {
                    "dob": {"type": "string", "description": "Date of birth in DD/MM/YYYY format"}
                },
                "required": ["dob"]
            }
        }
    }
]

models_to_test = ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"]

for m in models_to_test:
    print(f"\nTesting tool calling on {m}...")
    try:
        resp = client.chat.completions.create(
            model=m,
            messages=[{"role": "user", "content": "Please check my eligibility, my DOB is 15/08/1955"}],
            tools=dummy_tools,
            tool_choice="auto",
        )
        msg = resp.choices[0].message
        print(f"Content: {msg.content}")
        print(f"Tool calls: {msg.tool_calls}")
        if msg.tool_calls:
            for tc in msg.tool_calls:
                print(f"  -> Called {tc.function.name} with args {tc.function.arguments}")
    except Exception as e:
        print(f"Error on {m}: {e}")
