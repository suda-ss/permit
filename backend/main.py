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
import uuid
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
from tools.db import get_pool  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    sessions.start_reaper()
    yield
    await close_pool()
    await sessions.shutdown()


app = FastAPI(title="Permit Research Orchestrator API", lifespan=lifespan)


class ChatRequest(BaseModel):
    conversation_id: uuid.UUID
    message: str

class ConversationRequest(BaseModel):
    title: str | None = None


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


app.add_api_route("/api/auth/me", me, methods=["GET"])
app.add_api_route("/api/auth/register", register, methods=["POST"])
app.add_api_route("/api/auth/login", login, methods=["POST"])
app.add_api_route("/api/auth/logout", logout, methods=["POST"])
app.add_api_route("/api/auth/google/start", google_start, methods=["GET"])
app.add_api_route("/api/auth/google/callback", google_callback, methods=["GET"])

@app.get("/api/conversations")
async def list_conversations(request: Request) -> dict:
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, created_at, updated_at
              FROM permit_conversations
             WHERE user_id = $1
             ORDER BY updated_at DESC
            """,
            user["id"],
        )
    return {"conversations": [dict(row) for row in rows]}


@app.post("/api/conversations")
async def create_conversation(payload: ConversationRequest, request: Request) -> dict:
    user = await require_user(request)
    title = (payload.title or "New conversation").strip() or "New conversation"
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO permit_conversations (user_id, title)
            VALUES ($1, $2)
            RETURNING id, title, created_at, updated_at
            """,
            user["id"],
            title,
        )
    return dict(row)


@app.get("/api/conversations/{conversation_id}/messages")
async def conversation_messages(conversation_id: uuid.UUID, request: Request) -> dict:
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM permit_conversations WHERE id=$1 AND user_id=$2)",
            conversation_id,
            user["id"],
        )
        if not exists:
            raise HTTPException(status_code=404, detail="Conversation not found")
        rows = await conn.fetch(
            """
            SELECT role, content, created_at
              FROM permit_conversation_messages
             WHERE conversation_id=$1 AND user_id=$2
             ORDER BY created_at, id
            """,
            conversation_id,
            user["id"],
        )
    return {"messages": [dict(row) for row in rows]}


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: uuid.UUID, request: Request) -> dict:
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        deleted = await conn.fetchval(
            "DELETE FROM permit_conversations WHERE id=$1 AND user_id=$2 RETURNING id",
            conversation_id,
            user["id"],
        )
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await sessions.close(f"auth-user:{user['id']}:{conversation_id}")
    return {"deleted": True, "conversation_id": str(deleted)}


@app.post("/api/chat")
async def chat(chat_request: ChatRequest, request: Request) -> StreamingResponse:
    user = await require_user(request)

    if not chat_request.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")

    pool = await get_pool()
    async with pool.acquire() as conn:
        conversation = await conn.fetchrow(
            "SELECT id, title FROM permit_conversations WHERE id=$1 AND user_id=$2",
            chat_request.conversation_id,
            user["id"],
        )
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        history_exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM permit_conversation_messages WHERE conversation_id=$1 AND user_id=$2)",
            chat_request.conversation_id,
            user["id"],
        )
        await conn.execute(
            """
            INSERT INTO permit_conversation_messages (conversation_id, user_id, role, content)
            VALUES ($1, $2, 'user', $3)
            """,
            chat_request.conversation_id,
            user["id"],
            chat_request.message.strip(),
        )
        if not history_exists:
            await conn.execute(
                "UPDATE permit_conversations SET title=$1, updated_at=now() WHERE id=$2",
                chat_request.message.strip()[:80],
                chat_request.conversation_id,
            )
        else:
            await conn.execute(
                "UPDATE permit_conversations SET updated_at=now() WHERE id=$1",
                chat_request.conversation_id,
            )

    scoped_session_id = f"auth-user:{user['id']}:{chat_request.conversation_id}"
    client = await sessions.get_or_create(scoped_session_id)

    async def event_stream():
        assistant_text = ""
        try:
            async for event in run_turn(client, chat_request.message):
                if event.get("type") == "text_delta":
                    assistant_text += event.get("text", "")
                yield json.dumps(event) + "\n"
        except Exception as exc:  # surface to the chat UI instead of a bare 500
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"
        finally:
            if assistant_text.strip():
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO permit_conversation_messages (conversation_id, user_id, role, content)
                        VALUES ($1, $2, 'assistant', $3)
                        """,
                        chat_request.conversation_id,
                        user["id"],
                        assistant_text.strip(),
                    )
                    await conn.execute(
                        "UPDATE permit_conversations SET updated_at=now() WHERE id=$1",
                        chat_request.conversation_id,
                    )

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")
