import os
from evals.run import resolve_backend_config
import openai

cfg = resolve_backend_config("assistant")
client = openai.OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"])

test_models = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
]

for m in test_models:
    try:
        resp = client.chat.completions.create(
            model=m,
            max_tokens=10,
            messages=[{"role": "user", "content": "hi"}],
        )
        print(f"[OK] {m}: {resp.choices[0].message.content.strip()[:30]}")
    except Exception as e:
        print(f"[ERR] {m}: {e}")
