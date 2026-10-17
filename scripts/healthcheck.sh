#!/usr/bin/env bash
# scripts/healthcheck.sh — Automated minute-by-minute system healthcheck script.
#
# Probes:
# 1. Citizen Portal (/) -> 200
# 2. Citizen MCP (/citizen/mcp) -> 401 with WWW-Authenticate
# 3. Officer MCP (/officer/mcp) -> 401 with WWW-Authenticate
# 4. Eval Dashboard (/eval-dashboard) -> 200
# 5. Eval Dashboard Healthz (/eval-dashboard/healthz) -> 200
# 6. SMS Gateway (/__gateway/) -> 200

set -euo pipefail

HOST="${PUBLIC_BASE_URL:-https://sewasetu-hv.koreacentral.cloudapp.azure.com}"
LOG_FILE="${HEALTHCHECK_LOG:-/var/log/sewasetu-healthcheck.log}"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

check_endpoint() {
    local name="$1"
    local url="$2"
    local expected_code="$3"
    local extra_header="${4:-}"

    local code
    if [ -n "$extra_header" ]; then
        code=$(curl -k -s -o /dev/null -w "%{http_code}" -H "$extra_header" "$url" || echo "000")
    else
        code=$(curl -k -s -o /dev/null -w "%{http_code}" "$url" || echo "000")
    fi

    if [ "$code" = "$expected_code" ]; then
        echo "[$TIMESTAMP] OK   $name: HTTP $code (expected $expected_code)" | tee -a "$LOG_FILE"
        return 0
    else
        echo "[$TIMESTAMP] FAIL $name: HTTP $code (expected $expected_code)" | tee -a "$LOG_FILE"
        return 1
    fi
}

echo "--- SewaSetu Healthcheck: $TIMESTAMP ---" >> "$LOG_FILE"

failures=0

check_endpoint "Portal UI" "$HOST/" "200" || failures=$((failures + 1))
check_endpoint "Citizen MCP" "$HOST/citizen/mcp" "401" || failures=$((failures + 1))
check_endpoint "Officer MCP" "$HOST/officer/mcp" "401" || failures=$((failures + 1))
check_endpoint "Eval Dashboard" "$HOST/eval-dashboard" "200" || failures=$((failures + 1))
check_endpoint "Dashboard Healthz" "$HOST/eval-dashboard/healthz" "200" || failures=$((failures + 1))
check_endpoint "SMS Gateway" "$HOST/__gateway/" "200" || failures=$((failures + 1))

if [ "$failures" -eq 0 ]; then
    echo "[$TIMESTAMP] HEALTHY: All 6 endpoints verified." | tee -a "$LOG_FILE"
    exit 0
else
    echo "[$TIMESTAMP] DEGRADED: $failures endpoint(s) failed." | tee -a "$LOG_FILE"
    exit 1
fi
