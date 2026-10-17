import os

path = "/home/azureuser/sewasetu-assistant/.env"
lines_to_add = [
    "EVAL_ASSISTANT_BASE_URL=https://api.groq.com/openai/v1\n",
    "EVAL_ASSISTANT_MODEL=openai/gpt-oss-20b\n",
    "EVAL_SIMULATOR_BASE_URL=https://api.groq.com/openai/v1\n",
    "EVAL_SIMULATOR_MODEL=openai/gpt-oss-120b\n",
    "EVAL_JUDGE_MODEL=openai/gpt-oss-120b\n",
]

if os.path.exists(path):
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    with open(path, "a", encoding="utf-8") as f:
        for line in lines_to_add:
            var = line.split("=")[0]
            if var not in content:
                f.write(line)
    print("Configured EVAL_* variables in .env successfully.")
else:
    print(f"File not found: {path}")
