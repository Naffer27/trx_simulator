#!/usr/bin/env bash
set -euo pipefail

PROJ_DIR="$HOME/Desktop/trx_simulator"
VENV_DIR="$PROJ_DIR/venv"

cd "$PROJ_DIR"
source "$VENV_DIR/bin/activate"

# WS FastAPI en background
python - <<'PY' &
import uvicorn
uvicorn.run("websocket_server:app", host="127.0.0.1", port=8001, reload=False)
PY
WS_PID=$!

cleanup() { echo; echo "⏹  Stopping…"; kill "$WS_PID" 2>/dev/null || true; }
trap cleanup EXIT

echo "✅ WS en ws://127.0.0.1:8001/ws/trading/"
echo "🌐 Abre: http://127.0.0.1:8000/clean-dashboard/"
echo

python manage.py runserver 127.0.0.1:8000
