#!/usr/bin/env bash
# smoke_portal.sh — acceptance checks for Prompt 1
#
# Usage:
#   bash scripts/smoke_portal.sh <hostname>
#
# Example:
#   bash scripts/smoke_portal.sh sewasetu.koreacentral.cloudapp.azure.com
#
# Runs from OUTSIDE the VM; requires curl and jq.

set -euo pipefail

HOST="${1:?Usage: smoke_portal.sh <hostname>}"
BASE="https://${HOST}"
PASS=0
FAIL=0

green() { printf "\033[32m✓ %s\033[0m\n" "$1"; }
red()   { printf "\033[31m✗ %s\033[0m\n" "$1"; }

check() {
    local desc="$1"
    shift
    if "$@"; then
        green "$desc"
        PASS=$((PASS + 1))
    else
        red "$desc"
        FAIL=$((FAIL + 1))
    fi
}

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  Smoke tests for ${BASE}"
echo "═══════════════════════════════════════════════════════"
echo ""

# ── 1. Portal loads ────────────────────────────────────────
check "Homepage loads (HTTP 200)" \
    bash -c "curl -sSf '${BASE}/' -o /dev/null"

check "/apply page loads" \
    bash -c "curl -sSf '${BASE}/apply' -o /dev/null"

# ── 2. OAuth discovery ─────────────────────────────────────
check "OAuth discovery returns issuer == PUBLIC_BASE_URL (https, no trailing slash)" \
    bash -c "
        ISSUER=\$(curl -sS '${BASE}/.well-known/oauth-authorization-server' | jq -r '.issuer')
        [ \"\$ISSUER\" = '${BASE}' ]
    "

check "JWKS endpoint returns at least one key" \
    bash -c "
        KEYS=\$(curl -sS '${BASE}/.well-known/jwks.json' | jq '.keys | length')
        [ \"\$KEYS\" -ge 1 ]
    "

# ── 3. /internal/* blocked ─────────────────────────────────
check "/internal/anything returns 404" \
    bash -c "
        STATUS=\$(curl -sS -o /dev/null -w '%{http_code}' '${BASE}/internal/anything')
        [ \"\$STATUS\" = '404' ]
    "

# ── 4. Admin login page loads ──────────────────────────────
check "/admin login page loads" \
    bash -c "curl -sSf '${BASE}/admin' -o /dev/null -L"

# ── 5. SMS gateway console is reachable ────────────────────
check "/__gateway/ (SMS console) is reachable" \
    bash -c "curl -sSf '${BASE}/__gateway/' -o /dev/null"

# ── 6. Stub health endpoints ───────────────────────────────
check "mcp-citizen stub /citizen/mcp responds" \
    bash -c "
        STATUS=\$(curl -sS -o /dev/null -w '%{http_code}' '${BASE}/citizen/mcp')
        [ \"\$STATUS\" = '501' ] || [ \"\$STATUS\" = '200' ]
    "

check "mcp-officer stub /officer/mcp responds" \
    bash -c "
        STATUS=\$(curl -sS -o /dev/null -w '%{http_code}' '${BASE}/officer/mcp')
        [ \"\$STATUS\" = '501' ] || [ \"\$STATUS\" = '200' ]
    "

check "eval-dashboard stub /eval-dashboard responds" \
    bash -c "
        STATUS=\$(curl -sS -o /dev/null -w '%{http_code}' '${BASE}/eval-dashboard')
        [ \"\$STATUS\" = '501' ] || [ \"\$STATUS\" = '200' ]
    "

# ── Summary ────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════"
echo "  Results: ${PASS} passed, ${FAIL} failed"
echo "═══════════════════════════════════════════════════════"
echo ""

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
