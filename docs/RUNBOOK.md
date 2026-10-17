# SewaSetu Assistant — Operator & Grader Runbook

## 1. System Overview & Endpoints

The SewaSetu Old Age Pension AI Assistant is deployed on an Azure Virtual Machine behind a Caddy reverse proxy providing automated TLS termination and edge routing.

| Service / Endpoint | Description | Public URL | Internal Route |
| :--- | :--- | :--- | :--- |
| **Citizen Portal** | Legacy Flask web portal | `https://HOST/` | `app:8000` |
| **Citizen MCP** | Citizen streamable HTTP server | `https://HOST/citizen/mcp` | `mcp-citizen:8080` |
| **Officer MCP** | Officer streamable HTTP server | `https://HOST/officer/mcp` | `mcp-officer:8080` |
| **Eval Dashboard** | Public verification dashboard | `https://HOST/eval-dashboard` | `eval-dashboard:8080` |
| **Dashboard Health** | Healthcheck endpoint | `https://HOST/eval-dashboard/healthz` | `eval-dashboard:8080` |
| **SMS Gateway** | Verification OTP interface | `https://HOST/__gateway/` | `smsgw:8025` |
| **Internal API** | Microservice integration | Blocked from outside | `app:8000/internal/*` |

---

## 2. VM Access & Service Operations

### 2.1 SSH Access
```bash
ssh -i hvazure.pem azureuser@sewasetu-hv.koreacentral.cloudapp.azure.com
cd /home/azureuser/sewasetu-assistant
source /home/azureuser/venv/bin/activate
```

### 2.2 Docker Service Management
```bash
# View running containers and health status
docker compose -f docker-compose.yml -f docker-compose.assistant.yml ps

# View live service logs
docker compose -f docker-compose.yml -f docker-compose.assistant.yml logs -f --tail=50

# Restart or recreate a specific service
docker compose -f docker-compose.yml -f docker-compose.assistant.yml up -d --no-deps <service-name>
```

---

## 3. Grader Quick-Start: Connecting Real Claude Code

Graders can connect the official Claude Code CLI directly to both MCP servers:

### Step 1: Register Endpoints with Claude Code
```bash
claude mcp add --transport http sewasetu-citizen https://sewasetu-hv.koreacentral.cloudapp.azure.com/citizen/mcp
claude mcp add --transport http sewasetu-officer https://sewasetu-hv.koreacentral.cloudapp.azure.com/officer/mcp
```

### Step 2: Authenticate in Browser
Open `claude` and run `/mcp`:
1. **Citizen Authentication**: Enter applicant mobile number (e.g. `9990000001`). Retrieve OTP from the SMS gateway console at `https://sewasetu-hv.koreacentral.cloudapp.azure.com/__gateway/`.
2. **Officer Authentication**: Log in with officer administrative credentials (`admin` / configured password).

### Step 3: Run Interactive Test Conversations
- **Citizen Flow**: *"I am Kamala Devi Das, 70 years old, from Dhemaji Pathar. Help me apply for pension."*
- **Officer Flow**: *"Summarise the pending applications in Sonari block."*

---

## 4. Running the Automated Evaluation Suite

### 4.1 Deterministic Security Suite (S1–S7)
Zero LLM cost; asserts exact tool sets, role isolation, audience verification, and injection immunity:
```bash
python -m evals.security
```

### 4.2 Single Persona Execution (with backoff & pacing)
```bash
python -m evals.run --persona C02 --reps 1 --label "c02-test" --pace-seconds 2
```

### 4.3 Full Evaluation Suite
```bash
# Run in background to protect against SSH disconnects
nohup python -m evals.run --suite all --reps 1 --resume --label "final-run" --pace-seconds 2 > evals/runs/suite.log 2>&1 &
```

### 4.4 Automated Unit & Integration Tests (134 tests)
```bash
pytest tests/
```

---

## 5. Automated Maintenance, Backups & Healthchecks

### 5.1 Crontab Configuration
Active crontab jobs running as `azureuser`:
```cron
# Minute-by-minute system health monitoring
* * * * * /bin/bash /home/azureuser/sewasetu-assistant/scripts/healthcheck.sh >/dev/null 2>&1

# Nightly rotating database backup at 02:00 UTC (keeps 7 days)
0 2 * * * /bin/bash /home/azureuser/sewasetu-assistant/scripts/backup_db.sh >/dev/null 2>&1
```

### 5.2 Manual Healthcheck Execution
```bash
bash scripts/healthcheck.sh
# Check log output
cat /tmp/sewasetu-healthcheck.log
```

### 5.3 Manual Database Backup & Restore
```bash
# Create backup
sudo bash scripts/backup_db.sh

# Restore from a backup archive
gunzip -c /var/backups/sewasetu/sewasetu_YYYYMMDD_HHMMSS.sql.gz | docker exec -i sewasetu-assistant-db-1 psql -U sewasetu sewasetu
```

---

## 6. Data Hygiene & Marker Reset

Before handing over the environment or concluding evaluation sessions, purge all test marker mobile rows (`9990000001`–`9990000099`):
```bash
python scripts/reset_marker_data.py --all
```
This guarantees zero residual test applications, drafts, or temporary session grants in the production PostgreSQL database.
