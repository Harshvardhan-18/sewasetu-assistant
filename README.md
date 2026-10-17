# SewaSetu Old Age Pension AI Assistant

An open-standards, Model Context Protocol (MCP) enabled assistant for the Department of Social Welfare, Government of Purvanchal. Built to assist senior citizens and block welfare officers with old-age pension applications under strict statutory compliance.

The assistant connects to the live portal as its single system of record, ensuring the AI can never grant an entitlement that the physical counter or official paper form would reject.

```mermaid
flowchart TD
    subgraph Clients["Clients & Assistants"]
        C_AI["Citizen AI Assistant<br/>(Streamable HTTP / MCP)"]
        O_AI["Officer AI Assistant<br/>(Streamable HTTP / MCP)"]
        Browser["Citizen & Officer Browsers"]
    end

    subgraph Edge["Edge Layer (Caddy Reverse Proxy & TLS)"]
        Caddy["Caddy v2 (Port 80/443)<br/>Automatic TLS & Route Protection"]
    end

    subgraph MCP["MCP Services (FastAPI + MCP SDK)"]
        CitizenMCP["Citizen MCP Server (:8001)<br/>Audience: /citizen/mcp<br/>10 Citizen Tools"]
        OfficerMCP["Officer MCP Server (:8002)<br/>Audience: /officer/mcp<br/>7 Officer Tools"]
    end

    subgraph PortalStack["Core Portal & Database"]
        Flask["Flask Portal & Auth Server (:8000)<br/>RFC 8414 / RFC 9728 Discovery<br/>RS256 JWT Issuer & Session Token Bucket"]
        DB[(PostgreSQL 15<br/>100,000 Production Seed Records)]
        SLA["Nightly SLA Daemon<br/>(15-Day Statutory Deemed Approval)"]
        SMS["Simulated SMS Gateway (:8025)"]
    end

    subgraph EvalStack["Evaluation System"]
        Dashboard["Eval Dashboard (:8080)<br/>Static Runs & Live Transcript Viewer"]
        Harness["Eval Runner (evals.run)<br/>MCP SDK Loop + Groq Models"]
    end

    C_AI -->|Bearer citizen:mobile| Caddy
    O_AI -->|Bearer officer:username| Caddy
    Browser --> Caddy

    Caddy -->|/citizen/mcp| CitizenMCP
    Caddy -->|/officer/mcp| OfficerMCP
    Caddy -->|/, /status, /admin, /oauth/*| Flask
    Caddy -->|/eval-dashboard| Dashboard

    CitizenMCP -->|Verify JWT via JWKS| Flask
    OfficerMCP -->|Verify JWT via JWKS| Flask
    CitizenMCP -->|apply_for_pension_core()| DB
    OfficerMCP -->|decide_application()| DB
    Flask --> DB
    SLA --> DB
    Flask --> SMS
    Harness -.->|Evaluates| CitizenMCP
    Harness -.->|Evaluates| OfficerMCP
```

---

## Live Endpoints

- **Public Pension Portal:** `https://sewasetu-hv.koreacentral.cloudapp.azure.com`
- **Citizen MCP Server:** `https://sewasetu-hv.koreacentral.cloudapp.azure.com/citizen/mcp`
- **Officer MCP Server:** `https://sewasetu-hv.koreacentral.cloudapp.azure.com/officer/mcp`
- **Evaluation Dashboard:** `https://sewasetu-hv.koreacentral.cloudapp.azure.com/eval-dashboard`
- **OAuth Discovery:** `https://sewasetu-hv.koreacentral.cloudapp.azure.com/.well-known/oauth-authorization-server`
- **Protected Resource Discovery:** `https://sewasetu-hv.koreacentral.cloudapp.azure.com/.well-known/oauth-protected-resource`

---

## Running Locally

### 1. Requirements & Stack Boot
Ensure Docker and Docker Compose are installed. Clone the repository and boot the stack:

```bash
cp .env.example .env
docker compose up --build -d
```

### 2. Service Access
- Web Portal: `http://localhost:8000`
- Citizen MCP: `http://localhost:8001/citizen/mcp`
- Officer MCP: `http://localhost:8002/officer/mcp`
- Eval Dashboard: `http://localhost:8080/eval-dashboard`
- SMS Gateway: `http://localhost:8025`

### 3. Running Unit & Integration Tests
```bash
pytest tests/
```

---

## Running Evaluations

The evaluation harness implements synthetic persona testing using the Model Context Protocol:

```bash
# Run security and tool surface isolation tests
python -m evals.security

# Run a single persona smoke test
python -m evals.run --persona C01 --reps 1 --label c01-smoke

# Run the 8 core baseline personas with pace and resume
python -m evals.run --personas C01,C02,C03,C07,C08,C16,O1,O3 --reps 1 --resume --label core-baseline --pace-seconds 2
```

> [!NOTE]
> **Evaluation Backend Notice**: In accordance with project configuration, evaluations run via the official MCP Python SDK client loop against open models on Groq (such as `openai/gpt-oss-20b`) rather than Claude Code, because no Anthropic API key is configured. The harness converts MCP tool definitions to standard OpenAI-compatible function-calling schemas, serving as a direct open-standard proxy for small-model tool-calling benchmarks.

---

## Directory Map

```text
sewasetu-assistant/
├── app/                  # Flask portal, OAuth2 authorization server, database models, and services
│   ├── services/         # Domain logic (apply_for_pension_core, status_queries, officer_actions)
│   ├── templates/        # Portal web templates (citizen form, status tracking, officer admin)
│   └── scripts/          # Statutory deemed-approval scheduler job
├── citizen_mcp/          # Citizen MCP server (10 outcome-oriented tools, citizen OAuth audience)
├── officer_mcp/          # Officer MCP server (7 administrative review tools, officer OAuth audience)
├── mcp_common/           # Shared MCP utilities (JWT verification, rate-limiting, error handling)
├── evals/                # Evaluation framework
│   ├── personas/         # YAML specs for 19 counter personas and 3 held-out test cases
│   ├── runs/             # JSON execution records, transcripts, and evaluation artifacts
│   ├── run.py            # Headless simulator and assistant execution harness with backoff
│   └── security.py       # Deterministic suite for OAuth token isolation and role boundaries
├── eval_dashboard/       # Web dashboard visualizing test runs, coverage matrix, and transcripts
├── deploy/               # Deployment configurations (Caddyfile, Dockerfiles, compose overrides)
├── docs/                 # Architectural specifications, counter notes, and submission docs
├── scripts/              # Backup utilities, DB reset scripts, healthchecks, and smoke tests
└── tests/                # Unit and end-to-end integration test suites
```
