#!/usr/bin/env bash
set -euo pipefail

APP_NAME="Permit Agent"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_DIR="$(cd "$ROOT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$PARENT_DIR/.venv/bin/python}"
PIP_BIN="${PIP_BIN:-$PARENT_DIR/.venv/bin/pip}"
UVICORN_BIN="${UVICORN_BIN:-$PARENT_DIR/.venv/bin/uvicorn}"
DB_ADMIN_URL="${DB_ADMIN_URL:-postgres://permits@127.0.0.1:5433/postgres}"
DATABASE_URL="${DATABASE_URL:-postgres://permits@127.0.0.1:5433/permits}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3050}"
LOG_DIR="$ROOT_DIR/.local/logs"
PID_DIR="$ROOT_DIR/.local/pids"

cd "$ROOT_DIR"
mkdir -p "$LOG_DIR" "$PID_DIR" storage/raw

ensure_python() {
  if [ ! -x "$PYTHON_BIN" ]; then
    python3 -m venv "$PARENT_DIR/.venv"
  fi
  "$PIP_BIN" install -r requirements.txt >/dev/null
}

ensure_node() {
  if [ ! -d frontend/node_modules ]; then
    npm --prefix frontend install
  fi
}

write_env() {
  set_env() {
    local key="$1"
    local value="$2"
    local temp_file
    temp_file="20 20 12 61 79 80 81 98 701 33 100 204 250 395 398 399 400mktemp)"
    if [ -f .env ]; then
      grep -v "^$key=" .env > "$temp_file" || true
    fi
    printf "%s=%s\n" "$key" "$value" >> "$temp_file"
    mv "$temp_file" .env
  }

  if [ ! -f .env ]; then
    cp .env.example .env
  fi
  set_env DATABASE_URL "$DATABASE_URL"
  set_env AUTH_COOKIE_SECURE false
  set_env APP_BASE_PATH /permits
  set_env NEXT_PUBLIC_APP_BASE_PATH /permits
}

setup_db() {
  "$PYTHON_BIN" - <<'PY'
import asyncio, os
from pathlib import Path
from urllib.parse import urlparse
import asyncpg

admin_url=os.environ['DB_ADMIN_URL']
db_url=os.environ['DATABASE_URL']
db_name=urlparse(db_url).path.lstrip('/')
async def main():
    admin=await asyncpg.connect(admin_url)
    try:
        exists=await admin.fetchval('SELECT 1 FROM pg_database WHERE datname=$1', db_name)
        if not exists:
            await admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin.close()
    conn=await asyncpg.connect(db_url)
    try:
        await conn.execute(Path('db/schema.sql').read_text())
    finally:
        await conn.close()
asyncio.run(main())
PY
}

stop_port() {
  local port="$1"
  local pids
  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    kill $pids 2>/dev/null || true
  fi
}

start_app() {
  stop_port "$BACKEND_PORT"
  stop_port "$FRONTEND_PORT"
  export DATABASE_URL
  export RAW_STORAGE_DIR="storage/raw"
  nohup "$UVICORN_BIN" backend.main:app --host 127.0.0.1 --port "$BACKEND_PORT" >"$LOG_DIR/backend.log" 2>&1 & echo $! >"$PID_DIR/backend.pid"
  nohup npm --prefix frontend run dev -- --hostname 127.0.0.1 --port "$FRONTEND_PORT" >"$LOG_DIR/frontend.log" 2>&1 & echo $! >"$PID_DIR/frontend.pid"
}

stop_app() {
  for file in "$PID_DIR"/*.pid; do
    [ -f "$file" ] || continue
    kill "$(cat "$file")" 2>/dev/null || true
    rm -f "$file"
  done
  stop_port "$BACKEND_PORT"
  stop_port "$FRONTEND_PORT"
}

status_app() {
  curl -fsS "http://127.0.0.1:$BACKEND_PORT/api/health" >/dev/null && echo "$APP_NAME backend: ok" || echo "$APP_NAME backend: not ready"
  curl -fsS "http://127.0.0.1:$FRONTEND_PORT/permits" >/dev/null && echo "$APP_NAME frontend: ok" || echo "$APP_NAME frontend: not ready"
}

case "${1:-start}" in
  start)
    export DB_ADMIN_URL DATABASE_URL
    ensure_python
    ensure_node
    write_env
    setup_db
    start_app
    sleep 2
    status_app
    echo "Frontend: http://localhost:$FRONTEND_PORT/permits"
    echo "Backend:  http://localhost:$BACKEND_PORT"
    echo "Logs:     $LOG_DIR"
    ;;
  stop) stop_app ;;
  restart) stop_app; "$0" start ;;
  status) status_app ;;
  *) echo "Usage: ./local_setup.sh [start|stop|restart|status]"; exit 1 ;;
esac
