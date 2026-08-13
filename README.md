# Permit Research Agent

An agentic permit-research application that finds authority-having-jurisdiction (AHJ) sources, extracts structured permit requirements, stores source material and embeddings in PostgreSQL/pgvector, and presents results through a streaming web chat.

## Architecture

- FastAPI backend using the Claude Agent SDK
- Next.js frontend
- PostgreSQL 16 with pgvector
- Four specialized agents for source discovery, parsing, vector ingestion, and report compilation
- Docker Compose deployment with persistent database and raw-document volumes

## Local setup

1. Copy `.env.example` to `.env` and fill in the required values.
2. Install Python dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Start PostgreSQL/pgvector with `docker compose up -d db` or provide a compatible `DATABASE_URL`.
4. Run the backend:

   ```bash
   uvicorn backend.main:app --reload --port 8000
   ```

5. In another terminal, start the frontend:

   ```bash
   cd frontend
   npm ci
   npm run dev
   ```

The application is served under `/permits`.

## Docker deployment

After configuring `.env`, run:

```bash
./deploy.sh
```

The Compose deployment binds the application to `127.0.0.1:3007`; use the included nginx configuration as a starting point for a public reverse proxy.

## Verification

```bash
python3 -m compileall -q .
sh -n deploy.sh docker-entrypoint.sh
cd frontend && npm run build
```

There is currently no automated test suite. A full integration check requires Docker, valid Claude authentication, and a reachable PostgreSQL/pgvector database.
