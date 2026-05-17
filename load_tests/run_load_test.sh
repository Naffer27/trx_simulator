#!/usr/bin/env bash
# ============================================================
# run_load_test.sh — Convenience runner for trx_sim load tests
#
# Runs either Locust (HTTP) or the asyncio WS load test.
# Always checks prerequisites before starting.
#
# Usage:
#   bash load_tests/run_load_test.sh --type http
#   bash load_tests/run_load_test.sh --type ws --scenario connect --users 20
#   bash load_tests/run_load_test.sh --type ws --scenario burst --users 30 --orders 20
#   bash load_tests/run_load_test.sh --type ws --scenario sustained --users 10 --duration 120
#   bash load_tests/run_load_test.sh --type http --headless --users 50 --spawn-rate 5 --run-time 3m
#
# Prerequisites:
#   pip install -r load_tests/requirements-load-test.txt
#   LOAD_TEST_MODE=True in .env (bypasses rate limiting)
#   Test users created (see locustfile.py header for command)
# ============================================================
set -euo pipefail

HOST="${HOST:-http://127.0.0.1:8001}"
TYPE="http"
HEADLESS=0
USERS=20
SPAWN_RATE=5
RUN_TIME="5m"
RESULTS_DIR="load_tests/results"

# WS-specific
WS_SCENARIO="connect"
WS_TICKS=30
WS_ORDERS=12
WS_RECONNECTS=5
WS_DURATION=60

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
die()  { log "ERROR: $*"; exit 1; }
usage() {
    grep '^# ' "$0" | sed 's/^# //'
    exit 0
}

# ── Parse args ───────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --type)        TYPE="$2";        shift 2 ;;
        --host)        HOST="$2";        shift 2 ;;
        --users)       USERS="$2";       shift 2 ;;
        --headless)    HEADLESS=1;       shift   ;;
        --spawn-rate)  SPAWN_RATE="$2";  shift 2 ;;
        --run-time)    RUN_TIME="$2";    shift 2 ;;
        --scenario)    WS_SCENARIO="$2"; shift 2 ;;
        --ticks)       WS_TICKS="$2";    shift 2 ;;
        --orders)      WS_ORDERS="$2";   shift 2 ;;
        --reconnects)  WS_RECONNECTS="$2"; shift 2 ;;
        --duration)    WS_DURATION="$2"; shift 2 ;;
        --help|-h)     usage ;;
        *) die "Unknown option: $1" ;;
    esac
done

# ── Prerequisite checks ──────────────────────────────────────

log "Checking prerequisites..."

# Python environment
if ! python3 -c "import aiohttp, websockets" 2>/dev/null; then
    die "Missing load test packages. Run: pip install -r load_tests/requirements-load-test.txt"
fi

if [ "$TYPE" = "http" ]; then
    if ! python3 -c "import locust" 2>/dev/null; then
        die "Locust not installed. Run: pip install -r load_tests/requirements-load-test.txt"
    fi
fi

# Server reachability
if ! curl -sf --max-time 5 "$HOST/api/health/" > /dev/null 2>&1; then
    die "Server not reachable at $HOST — is Daphne running?"
fi

log "  ✓ server reachable: $HOST"

# LOAD_TEST_MODE check (warn only — don't block)
HEALTH_RESP=$(curl -sf --max-time 5 "$HOST/api/health/" 2>/dev/null || echo "{}")
if echo "$HEALTH_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
# If rate_limit_bypassed key is present and False, warn
bypass = d.get('rate_limit_bypassed', None)
if bypass is False:
    sys.exit(1)
" 2>/dev/null; then
    :
else
    log "  ⚠ WARNING: LOAD_TEST_MODE may not be set — rate limiting is active."
    log "    Add LOAD_TEST_MODE=True to .env and restart Daphne before running load tests."
fi

# ── Create results dir ───────────────────────────────────────
mkdir -p "$RESULTS_DIR"
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')

# ── Run HTTP load test (Locust) ───────────────────────────────
if [ "$TYPE" = "http" ]; then
    log "Starting HTTP load test (Locust)..."
    log "  host=$HOST  users=$USERS  spawn-rate=$SPAWN_RATE  run-time=$RUN_TIME"

    REPORT_HTML="$RESULTS_DIR/http_${TIMESTAMP}.html"
    REPORT_CSV="$RESULTS_DIR/http_${TIMESTAMP}"

    if [ $HEADLESS -eq 1 ]; then
        locust \
            -f load_tests/locustfile.py \
            --host "$HOST" \
            --users "$USERS" \
            --spawn-rate "$SPAWN_RATE" \
            --run-time "$RUN_TIME" \
            --headless \
            --html "$REPORT_HTML" \
            --csv "$REPORT_CSV"
        log "Report saved: $REPORT_HTML"
        log "CSV saved:    ${REPORT_CSV}_stats.csv"
    else
        log "  Starting Locust web UI on http://localhost:8089 ..."
        log "  Press Ctrl+C to stop."
        locust \
            -f load_tests/locustfile.py \
            --host "$HOST"
    fi

# ── Run WebSocket load test ───────────────────────────────────
elif [ "$TYPE" = "ws" ]; then
    log "Starting WebSocket load test..."
    log "  host=$HOST  scenario=$WS_SCENARIO  users=$USERS"

    EXTRA_ARGS=""
    case "$WS_SCENARIO" in
        connect)   EXTRA_ARGS="--ticks $WS_TICKS" ;;
        burst)     EXTRA_ARGS="--orders $WS_ORDERS" ;;
        reconnect) EXTRA_ARGS="--reconnects $WS_RECONNECTS" ;;
        sustained) EXTRA_ARGS="--duration $WS_DURATION" ;;
    esac

    OUTFILE="$RESULTS_DIR/ws_${WS_SCENARIO}_${TIMESTAMP}.txt"

    python3 load_tests/ws_load.py \
        --host "$HOST" \
        --users "$USERS" \
        --scenario "$WS_SCENARIO" \
        $EXTRA_ARGS \
        2>&1 | tee "$OUTFILE"

    log "Output saved: $OUTFILE"

else
    die "Unknown --type '$TYPE'. Use: http | ws"
fi

log "Load test complete."
