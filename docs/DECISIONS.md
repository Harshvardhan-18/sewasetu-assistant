# Design Decisions Log

A dated line for every non-obvious decision and why.

---

## 2026-10-14 — Prompt 1: Repo baseline, VM & HTTPS

### Deployment: layer on top of the original docker-compose.yml
We use `docker-compose.assistant.yml` as an override file rather than editing
`docker-compose.yml`. This keeps the handout untouched and makes the diff between
"what we were given" and "what we added" obvious.

### Caddy as the TLS edge
Caddy auto-obtains and renews HTTPS certificates (via Let's Encrypt / ZeroSSL)
with no configuration beyond a hostname. It also makes it trivial to block
`/internal/*` at the edge.

### Stub MCP / dashboard services
mcp-citizen, mcp-officer and eval-dashboard are stubbed with a minimal Python
HTTP server that serves `/healthz`. This lets us validate the full Caddy routing
and compose layering before any real logic is written.

### Deemed-approval first-boot numbers
*(To be filled after first deployment — run `docker compose logs scheduler` and
record how many PENDING rows the startup deemed-approval job flipped.)*

<!-- Expect ~547 of the 700 seeded PENDING rows to flip (those older than 15 days
     as of today). See BUILD_GUIDE §2 finding 9. -->
