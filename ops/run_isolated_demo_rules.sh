#!/bin/bash
set -Eeuo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
MODE=${1:-sqlite}
[[ "$MODE" == sqlite || "$MODE" == postgres ]] || { echo "Uso: $0 [sqlite|postgres]"; exit 2; }
RUN=$(mktemp -d /tmp/money-demo-pg.XXXXXX)
PG=${DEMO_PG_BIN:-/opt/homebrew/opt/postgresql@16/bin}
REDIS_SERVER=${DEMO_REDIS_SERVER:-/opt/homebrew/bin/redis-server}
mkdir "$RUN/app" "$RUN/socket"
cleanup() {
 "$PG/pg_ctl" -D "$RUN/pgdata" -m fast stop > /dev/null 2>&1 || true
 if [[ -f "$RUN/redis.pid" ]]; then kill "$(cat "$RUN/redis.pid")" 2>/dev/null || true; fi
 echo "Resultados: $RUN"
}
trap cleanup EXIT
# Archive tracked code only: no .env, production DB or untracked backups.
git -C "$ROOT" archive HEAD | tar -x -C "$RUN/app"
for rel in simulator/opening_charges.py simulator/consumers.py simulator/tests/test_demo_real_catalog_rules.py simulator/tests/test_pending_spread_concurrency.py simulator/tests/test_ledger_rounding_reconciliation01.py simulator/tests/test_o6c1aa_unified_raw_execution_spread_fee.py; do
 cp "$ROOT/$rel" "$RUN/app/$rel"
done
test ! -e "$RUN/app/.env"
if [[ "$MODE" == postgres ]]; then
"$PG/initdb" -D "$RUN/pgdata" -U demo_owner --auth=trust --encoding=UTF8 --locale=C > "$RUN/init.log"
"$PG/pg_ctl" -D "$RUN/pgdata" -l "$RUN/postgres.log" -o "-k $RUN/socket -p 16439 -c listen_addresses=''" start > "$RUN/start.log"
"$PG/createdb" -h "$RUN/socket" -p 16439 -U demo_owner money_demo_pilot
 DB=money_demo_pilot
else
 DB=
fi
"$REDIS_SERVER" --bind 127.0.0.1 --port 16391 --save '' --appendonly no --daemonize yes --pidfile "$RUN/redis.pid" --logfile "$RUN/redis.log" --dir "$RUN"
cd "$RUN/app"
env -i PATH="/opt/homebrew/bin:/usr/bin:/bin" HOME="$RUN" LANG=C LC_ALL=C \
 APP_ENV=development DJANGO_SECRET_KEY=isolated-demo-validation-only \
 DB_NAME="$DB" DB_TEST_NAME=money_demo_pilot_test DB_HOST="$RUN/socket" DB_PORT=16439 DB_USER=demo_owner DB_PASSWORD='' \
 REDIS_URL=redis://127.0.0.1:16391/0 PYTHONDONTWRITEBYTECODE=1 \
 "$ROOT/.venv/bin/python" -B manage.py test simulator.tests.test_demo_real_catalog_rules simulator.tests.test_pending_spread_parity simulator.tests.test_pending_spread_concurrency simulator.tests.test_ledger_rounding_reconciliation01 simulator.tests.test_order_management_v2a simulator.tests.test_o6c1aa_unified_raw_execution_spread_fee --keepdb --noinput --verbosity 1 > "$RUN/tests.log" 2>&1
 tail -7 "$RUN/tests.log"
