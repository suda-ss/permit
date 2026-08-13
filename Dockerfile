# syntax=docker/dockerfile:1

# ---- Stage 1: build the Next.js frontend ----
FROM node:22-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: runtime (Python backend + Node for claude CLI + Next.js standalone) ----
FROM node:22-slim AS runtime

# Python + a compiler toolchain: sentence-transformers/torch and asyncpg's C
# extension need one available at pip-install time on a slim base image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv build-essential \
    && rm -rf /var/lib/apt/lists/*

# claude CLI (spawned as a subprocess by claude_agent_sdk), pre-installed at
# build time so there's no runtime npm-registry dependency or cold-start
# latency on first tool call.
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

# Python deps in a venv (avoids fighting Debian's externally-managed-environment guard).
COPY backend/requirements.txt ./backend/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r backend/requirements.txt
ENV PATH="/opt/venv/bin:${PATH}"

# Pre-download the local embedding model at build time — avoids a slow,
# network-dependent stall on the first vector-ingest call in production.
# Must match tools/embeddings.py's DEFAULT_MODEL / the EMBEDDING_MODEL env
# var used at runtime; change both together, and re-embed existing
# permit_chunks rows if you ever do (different models aren't comparable).
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
ENV EMBEDDING_MODEL=${EMBEDDING_MODEL}
RUN python3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}')"

# App code — layout must match what agent_config.py/session_manager.py
# expect: agent_config.py, events.py, main_agent.md, agents/, tools/, db/,
# and .claude/ all live as siblings of backend/, anchored via Path(__file__),
# not process cwd.
COPY agent_config.py events.py main_agent.md ./
COPY .claude/ ./.claude/
COPY agents/ ./agents/
COPY tools/ ./tools/
COPY db/ ./db/
COPY backend/ ./backend/

# Raw document storage — a local folder inside the container for now (per
# the current design), persisted via the `raw-storage` volume in
# docker-compose.yml so it survives container recreation.
RUN mkdir -p storage/raw

# Next.js standalone output — only the traced node_modules, not the full tree.
COPY --from=frontend-builder /app/frontend/.next/standalone ./frontend/
COPY --from=frontend-builder /app/frontend/.next/static ./frontend/.next/static
COPY --from=frontend-builder /app/frontend/public ./frontend/public

COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Only the frontend port is published — the backend (port 8000) stays
# container-internal, reached only via Next.js's rewrite proxy. This is the
# single port nginx/docker-compose needs to point at.
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD node -e "fetch('http://127.0.0.1:3000/permits/api/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

ENTRYPOINT ["./docker-entrypoint.sh"]
