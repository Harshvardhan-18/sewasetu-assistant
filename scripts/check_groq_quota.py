import os
import requests

api_key = ""
env_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
if os.path.exists(env_file):
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            if line.startswith("GROQ_API_KEY="):
                api_key = line.split("=", 1)[1].strip().strip("'\"")

headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
}
payload = {
    "model": "openai/gpt-oss-20b",
    "messages": [{"role": "user", "content": "ping"}],
    "max_tokens": 5,
}

r = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)
print(f"Status: {r.status_code}")
for k, v in r.headers.items():
    if "ratelimit" in k.lower():
        print(f"  {k}: {v}")
if r.status_code != 200:
    print(f"Error Body: {r.text[:300]}")
