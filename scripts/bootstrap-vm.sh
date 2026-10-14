#!/usr/bin/env bash
# bootstrap-vm.sh — one-time setup for Ubuntu 24.04 on Azure
#
# Run as root (or with sudo) on a fresh VM:
#   curl -sSL https://raw.githubusercontent.com/…/scripts/bootstrap-vm.sh | sudo bash
#   — or —
#   sudo bash scripts/bootstrap-vm.sh
#
# What it does:
#   1. Installs Docker CE + compose plugin
#   2. Adds the 'azureuser' to the docker group
#   3. Configures ufw to allow 22, 80, 443 (no deny rules that could block these)
#   4. Enables unattended-upgrades for security patches
#
# What it does NOT do:
#   - Clone the repo (you do that)
#   - Create/copy .env (you do that)
#   - Start the services (you do that with docker compose)

set -euo pipefail

echo "=== [1/4] Installing Docker CE + compose plugin ==="
apt-get update
apt-get install -y ca-certificates curl gnupg

install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
fi

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  tee /etc/apt/sources.list.d/docker.list > /dev/null

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

echo "=== [2/4] Adding azureuser to the docker group ==="
usermod -aG docker azureuser || true

echo "=== [3/4] Configuring ufw ==="
ufw allow 22/tcp   comment "SSH"
ufw allow 80/tcp   comment "HTTP"
ufw allow 443/tcp  comment "HTTPS"
ufw allow 443/udp  comment "HTTP/3 QUIC"
# Enable ufw non-interactively (default policy: deny incoming, allow outgoing)
ufw --force enable
ufw status verbose

echo "=== [4/4] Enabling unattended-upgrades ==="
apt-get install -y unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades

echo ""
echo "=== Bootstrap complete ==="
echo "Next steps:"
echo "  1. Log out and back in (so the docker group takes effect), or run: newgrp docker"
echo "  2. Clone your repo"
echo "  3. Copy .env.example to .env and fill in the values"
echo "  4. Run: docker compose -f docker-compose.yml -f docker-compose.assistant.yml up -d --build"
echo ""
