"""
FastAPI backend for the permit research orchestrator chat webapp.

Exposes:
  POST /api/chat    - streams the orchestrator's response as newline-delimited
                       JSON events (see events.py for the event shapes)
  GET  /api/health  - liveness check for Docker/nginx

Session identity (session_id) is supplied by the client (one per browser
tab — see frontend) and mapped to a persistent ClaudeSDKClient by
session_manager.SessionManager, so each tab gets an isolated conversation.

Unlike research_Agent's backend, no client-side RAG injection happens here —
the orchestrator's own find_ahj/get_structured_permit_data/vector_search
tool calls (see agent_config.py, tools/mcp_tools.py) are the cache-check
step, driven by .claude/skills/permit-research/SKILL.md.

A single POST /api/chat call can stay open for several minutes: chat_loop's
run_turn() automatically re-prompts the orchestrator roughly once a minute
for status while it's still calling tools (e.g. waiting on a delegated
subagent), so the response stream keeps producing events instead of ending
the moment the orchestrator says "I'll follow up" and goes quiet.
"""

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from backend.auth import google_callback, google_start, login, logout, me, register, require_user  # noqa: E402
from backend.session_manager import sessions  # noqa: E402
from chat_loop import run_turn  # noqa: E402
from tools.db import close_pool  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    sessions.start_reaper()
    yield
    await close_pool()
    await sessions.shutdown()


app = FastAPI(title="Permit Research Orchestrator API", lifespan=lifespan)


class ChatRequest(BaseModel):
    session_id: str
    message: str


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


app.add_api_route("/api/auth/me", me, methods=["GET"])
app.add_api_route("/api/auth/register", register, methods=["POST"])
app.add_api_route("/api/auth/login", login, methods=["POST"])
app.add_api_route("/api/auth/logout", logout, methods=["POST"])
app.add_api_route("/api/auth/google/start", google_start, methods=["GET"])
app.add_api_route("/api/auth/google/callback", google_callback, methods=["GET"])


@app.post("/api/chat")
async def chat(chat_request: ChatRequest, request: Request) -> StreamingResponse:
    user = await require_user(request)

    if not chat_request.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")

    scoped_session_id = f"auth-user:{user['id']}:{chat_request.session_id}"
    client = await sessions.get_or_create(scoped_session_id)

    async def event_stream():
        try:
            async for event in run_turn(client, chat_request.message):
                yield json.dumps(event) + "\n"
        except Exception as exc:  # surface to the chat UI instead of a bare 500
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")
