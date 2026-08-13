#!/usr/bin/env bash
set -euo pipefail

# Builds and (re)deploys the permit research agent (app + Postgres/pgvector)
# via Docker Compose. Run this after `git pull` on whatever host is actually
# serving it (e.g. your production box).
#
# Usage: ./deploy.sh
#
# Requires .env with at least POSTGRES_PASSWORD and ANTHROPIC_API_KEY set —
# see .env.example. Headless containers can't do the interactive
# `claude login` OAuth flow, so production always needs ANTHROPIC_API_KEY;
# local dev (running main.py/backend directly on the host, with
# `docker compose up db` for just the database) can rely on your existing
# `claude login` session instead.
#
# The app is bound to 127.0.0.1:3007 only (see docker-compose.yml) — keep
# this PORT in sync with that file if you ever change it. nginx (or your
# reverse proxy) is the only thing that should reach it.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

ENV_FILE="$HERE/.env"
PORT=3007

if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker is not installed or not on PATH." >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "Error: Docker daemon isn't reachable (not running, or you lack" >&2
    echo "permission — try 'sudo', or add your user to the docker group)." >&2
    exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "Error: $ENV_FILE not found." >&2
    echo "Copy .env.example to .env and fill in ANTHROPIC_API_KEY and POSTGRES_PASSWORD (both required)." >&2
    exit 1
fi

echo "==> Building and starting services (db + app)"
docker compose up -d --build

echo "==> Waiting for health check..."
for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${PORT}/permits/api/health" >/dev/null 2>&1; then
        echo "==> Healthy. App is live on http://127.0.0.1:${PORT}/permits"
        exit 0
    fi
    sleep 2
done

echo "Warning: services started but /api/health didn't respond within 60s." >&2
echo "Check logs with: docker compose logs -f app" >&2
exit 1
