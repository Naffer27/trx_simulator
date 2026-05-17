#!/usr/bin/env bash
# ============================================================
# healthcheck.sh — Verify all subsystems are alive
#
# Returns 0 if healthy, 1 if any subsystem is degraded.
# Used by deploy.sh and can be called independently.
#
# Usage:
#   bash deploy/scripts/healthcheck.sh
#   bash deploy/scripts/healthcheck.sh https://staging.yourdomain.com
# ============================================================
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:8001}"
HEALTH_URL="$BASE_URL/api/health/"
TIMEOUT=10

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
ok()   { log "  ✓ $*"; }
fail() { log "  ✗ FAIL: $*"; FAILED=1; }

FAILED=0

log "Health check → $HEALTH_URL"

# ── HTTP health endpoint ──────────────────────────────────────
RESPONSE=$(curl -sf --max-time "$TIMEOUT" \
    -H "Accept: application/json" \
    "$HEALTH_URL" 2>&1) || { fail "HTTP request failed (connection refused or timeout)"; FAILED=1; }

if [ $FAILED -eq 0 ]; then
    STATUS=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','unknown'))" 2>/dev/null || echo "parse_error")
    if [ "$STATUS" = "ok" ]; then
        ok "API health: $STATUS"
    else
        fail "API health status=$STATUS — response: $RESPONSE"
    fi
fi

# ── systemd services ─────────────────────────────────────────
for svc in daphne celery-worker celery-beat; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        ok "systemd $svc: active"
    else
        fail "systemd $svc: NOT active"
    fi
done

# ── Redis ─────────────────────────────────────────────────────
REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
REDIS_HOST=$(echo "$REDIS_URL" | sed 's|redis://[^@]*@||' | cut -d: -f1 | sed 's|redis://||')
REDIS_PORT=$(echo "$REDIS_URL" | cut -d: -f3 | cut -d/ -f1)
REDIS_PORT="${REDIS_PORT:-6379}"

if redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" ping 2>/dev/null | grep -q PONG; then
    ok "Redis: PONG"
else
    fail "Redis: no PONG from $REDIS_HOST:$REDIS_PORT"
fi

# ── PostgreSQL ───────────────────────────────────────────────
if command -v psql &>/dev/null; then
    DB="${DB_NAME:-trx_sim_staging}"
    DB_USER="${DB_USER:-trx_sim}"
    if psql -U "$DB_USER" -d "$DB" -c "SELECT 1" &>/dev/null; then
        ok "PostgreSQL: connected"
    else
        fail "PostgreSQL: cannot connect to $DB as $DB_USER"
    fi
else
    log "  ? PostgreSQL: psql not found — skipping"
fi

# ── Nginx ─────────────────────────────────────────────────────
if systemctl is-active --quiet nginx 2>/dev/null; then
    ok "Nginx: active"
else
    log "  ? Nginx: not checked (may not be installed here)"
fi

echo ""
if [ $FAILED -eq 0 ]; then
    log "All health checks PASSED"
    exit 0
else
    log "One or more health checks FAILED"
    exit 1
fi
