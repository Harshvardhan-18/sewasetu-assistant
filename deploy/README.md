# Deployment Guide

## Prerequisites

- **VM**: Ubuntu 24.04, 2 vCPU / 4 GB RAM minimum (Azure B2als_v2 or equivalent)
- **Ports**: 22, 80, 443 open in the cloud provider's firewall / NSG
- **Hostname**: Either an Azure DNS label (`<label>.koreacentral.cloudapp.azure.com`) or an sslip.io address (see below)

## Getting a hostname

### Option A: Azure DNS label (recommended)
In the Azure portal, go to your VM's public IP resource → Configuration → set a **DNS name label**.
Your hostname becomes `<label>.koreacentral.cloudapp.azure.com`.

### Option B: sslip.io (no domain needed)
Replace dots in your IP with dashes:
```
# For IP 20.41.121.243:
PUBLIC_HOST=20-41-121-243.sslip.io
```
Caddy will automatically obtain a TLS certificate via the ACME HTTP-01 challenge.

## Step-by-step

### 1. Bootstrap the VM

```bash
ssh azureuser@<your-ip>
# Upload or clone the repo first, then:
sudo bash scripts/bootstrap-vm.sh
# Log out and back in for docker group to take effect
exit
ssh azureuser@<your-ip>
```

### 2. Configure environment

```bash
cd ~/sewasetu-ai-handout   # or wherever you cloned the repo
cp .env.example .env
nano .env
```

Fill in:
```
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<a-strong-password>
PUBLIC_BASE_URL=https://<your-hostname>
PUBLIC_HOST=<your-hostname>
INTERNAL_API_KEY=<random-string-32-chars>
```

> **Note**: `PUBLIC_BASE_URL` must be `https://`, no trailing slash.
> `PUBLIC_HOST` is just the hostname (no scheme).

### 3. Start the stack

```bash
docker compose -f docker-compose.yml -f docker-compose.assistant.yml up -d --build
```

**First boot takes ~1–2 minutes** because PostgreSQL loads the 35 MB seed file (`seed.sql`).
The `scheduler` service runs the deemed-approval job at startup, then nightly at 02:00 IST.

Watch progress:
```bash
docker compose logs -f app db scheduler
```

Wait until `app` reports `Listening at: http://0.0.0.0:8000`.

### 4. Verify

From a machine *outside* the VM:
```bash
bash scripts/smoke_portal.sh <your-hostname>
```

Or manually:
```bash
# Portal loads
curl -sSf https://<your-hostname>/

# OAuth discovery
curl -s https://<your-hostname>/.well-known/oauth-authorization-server | python3 -m json.tool

# JWKS
curl -s https://<your-hostname>/.well-known/jwks.json | python3 -m json.tool

# /internal/* blocked
curl -i https://<your-hostname>/internal/anything
# → should return 404
```

### 5. Check deemed-approval run

```bash
docker compose logs scheduler | grep -i "deemed"
```

Record the number of flipped rows in `docs/DECISIONS.md`.

## Updating

```bash
cd ~/sewasetu-ai-handout
git pull
docker compose -f docker-compose.yml -f docker-compose.assistant.yml up -d --build
```

## Data persistence

- PostgreSQL data is in the `pgdata` Docker volume — survives `docker compose down && up`.
- The seed is loaded **only** on first boot of an empty volume. Subsequent restarts do NOT re-seed.
- To start fresh: `docker compose down -v` (destroys all volumes).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Caddy says "challenge failed" | Ensure ports 80 and 443 are open in Azure NSG and ufw |
| App won't start | Check `docker compose logs app` — usually waiting for DB |
| Seed takes forever | Normal: 35 MB, ~1–2 min on first boot |
| `ERR_CONNECTION_REFUSED` on 443 | Wait for Caddy to get the cert (~10–30 s after first request) |
