# SewaSetu AI Assistant — Submission Checklist

## Live Service Endpoints
- **Public Pension Portal:** https://sewasetu-hv.koreacentral.cloudapp.azure.com
- **Citizen MCP Server:** https://sewasetu-hv.koreacentral.cloudapp.azure.com/citizen/mcp
- **Officer MCP Server:** https://sewasetu-hv.koreacentral.cloudapp.azure.com/officer/mcp
- **Eval Dashboard:** https://sewasetu-hv.koreacentral.cloudapp.azure.com/eval-dashboard

## Officer Authentication
- **Username:** `admin`
- **Password:** `22V2mwuK5LVrwFuoKFon!9A`
- **Authentication Method:** OAuth 2.0 Resource Owner Password Credentials (ROPC) exchange via `/oauth/token` with audience `officer_mcp`

## Repository & Collaboration
- **Repository Name:** `sewasetu-assistant`
- **GitHub URL:** https://github.com/Harshvardhan-18/sewasetu-assistant
- **Collaborator Invited:** `harshnisar`

## Deployed Architecture Summary
- **Cloud Infrastructure:** Azure Linux VM (`Standard_B2s`, Ubuntu 24.04 LTS, location `koreacentral`)
- **Reverse Proxy / TLS Edge:** Caddy v2 with automated Let's Encrypt TLS termination and path routing
- **Containers:**
  - `sewasetu-assistant-caddy-1` (Reverse Proxy / TLS termination)
  - `sewasetu-assistant-app-1` (Flask Portal, Gunicorn WSGI)
  - `sewasetu-assistant-mcp-citizen-1` (FastAPI / Streamable HTTP MCP, Uvicorn)
  - `sewasetu-assistant-mcp-officer-1` (FastAPI / Streamable HTTP MCP, Uvicorn)
  - `sewasetu-assistant-eval-dashboard-1` (Flask Dashboard, Port 8080)
  - `sewasetu-assistant-db-1` (PostgreSQL 15)
  - `sewasetu-assistant-scheduler-1` (Background SLA daemon)
  - `sewasetu-assistant-smsgw-1` (Mock SMS Gateway, Port 8025)
- **Security Posture:**
  - Strict JWT validation with audience separation (`citizen_mcp` vs `officer_mcp`).
  - Edge perimeter isolation: direct public access to `/internal/*` blocked at Caddy (HTTP 404).
  - Rate limiting: in-memory token bucket on sensitive auth endpoints.
  - Database sanitation: non-destructive marker mobile allocation (`99900000xx`) leaving baseline 100,000 records untouched.
