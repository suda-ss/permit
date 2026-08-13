#!/bin/sh
set -e

# Backend runs in the background on loopback only — never exposed outside
# the container. Next.js is the process the container actually publishes
# (port 3000), proxying /api/* to the backend via next.config.js rewrites.
/opt/venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!

cleanup() {
    kill "$BACKEND_PID" 2>/dev/null || true
    kill "$FRONTEND_PID" 2>/dev/null || true
}
# This script is PID 1 (no exec below), so it's the actual signal target —
# `docker stop` reaches this trap, which then stops both children instead
# of leaving the backend orphaned.
trap cleanup EXIT INT TERM

cd frontend
# Docker auto-sets HOSTNAME to the container ID, and Next's standalone
# server.js binds to $HOSTNAME when set instead of 0.0.0.0 — leaving it
# listening only on the container's internal docker-network IP, not
# loopback. That breaks the Dockerfile HEALTHCHECK (runs inside the
# container, hits 127.0.0.1) even though the externally published port
# still works via Docker's NAT. Override it explicitly.
HOSTNAME=0.0.0.0 node server.js &
FRONTEND_PID=$!

wait "$FRONTEND_PID"
