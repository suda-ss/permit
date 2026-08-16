"""
FastAPI backend for the permit research orchestrator chat webapp.

Exposes:
  POST /api/chat    - starts a durable background agent run
  GET  /api/health  - liveness check for Docker/nginx

Session identity (session_id) is supplied by the client (one per browser
tab — see frontend) and mapped to a persistent ClaudeSDKClient by
session_manager.SessionManager, so each tab gets an isolated conversation.

Unlike research_Agent's backend, no client-side RAG injection happens here —
the orchestrator's own find_ahj/get_structured_permit_data/vector_search
tool calls (see agent_config.py, tools/mcp_tools.py) are the cache-check
step, driven by .claude/skills/permit-research/SKILL.md.

Runs continue after the browser leaves the page. chat_loop automatically
re-prompts the orchestrator every two minutes, and every agent summary is
persisted before the conversation polling endpoint exposes it to the UI.
"""

import asyncio
import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from backend.auth import google_callback, google_start, login, logout, me, register, require_user  # noqa: E402
from backend.session_manager import sessions  # noqa: E402
from chat_loop import run_turn  # noqa: E402
from tools.db import close_pool  # noqa: E402
from tools.db import get_pool  # noqa: E402


background_tasks: set[asyncio.Task] = set()
STATUS_INTERVAL_SECONDS = int(os.getenv("CHAT_STATUS_INTERVAL_SECONDS", "120"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    sessions.start_reaper()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE permit_chat_runs
                  SET status='failed', error='Server restarted before this run completed',
                      completed_at=now()
                WHERE status='running'"""
        )
    yield
    for task in list(background_tasks):
        task.cancel()
    if background_tasks:
        await asyncio.gather(*background_tasks, return_exceptions=True)
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


@app.patch("/api/conversations/{conversation_id}")
async def rename_conversation(
    conversation_id: uuid.UUID, payload: ConversationRequest, request: Request
) -> dict:
    user = await require_user(request)
    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title must not be empty")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE permit_conversations
                  SET title=$1, updated_at=now()
                WHERE id=$2 AND user_id=$3
                RETURNING id, title, created_at, updated_at""",
            title[:120],
            conversation_id,
            user["id"],
        )
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")
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


@app.get("/api/conversations/{conversation_id}/status")
async def conversation_status(conversation_id: uuid.UUID, request: Request) -> dict:
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
        run = await conn.fetchrow(
            """SELECT id, status, error, started_at, completed_at
                 FROM permit_chat_runs
                WHERE conversation_id=$1 AND user_id=$2
                ORDER BY started_at DESC LIMIT 1""",
            conversation_id,
            user["id"],
        )
    return {"running": bool(run and run["status"] == "running"), "run": dict(run) if run else None}


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: uuid.UUID, request: Request) -> dict:
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        running = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM permit_chat_runs WHERE conversation_id=$1 AND status='running')",
            conversation_id,
        )
        if running:
            raise HTTPException(status_code=409, detail="Wait for the active agent run to finish before deleting this conversation")
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
async def chat(chat_request: ChatRequest, request: Request) -> dict:
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
        running = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM permit_chat_runs WHERE conversation_id=$1 AND status='running')",
            chat_request.conversation_id,
        )
        if running:
            raise HTTPException(status_code=409, detail="An agent is already working in this conversation")
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
        run_id = await conn.fetchval(
            """INSERT INTO permit_chat_runs (conversation_id, user_id, status)
                 VALUES ($1, $2, 'running') RETURNING id""",
            chat_request.conversation_id,
            user["id"],
        )
        await conn.execute(
            """INSERT INTO permit_conversation_messages
                   (conversation_id, user_id, role, content)
                 VALUES ($1, $2, 'assistant', $3)""",
            chat_request.conversation_id,
            user["id"],
            "Permit Agent is working on your request.\n"
            "I’ll post another update here within two minutes.",
        )

    async def execute_run() -> None:
        stage = "reviewing the request and planning the permit research"
        run_finished = asyncio.Event()

        async def persist_assistant(content: str) -> None:
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO permit_conversation_messages
                           (conversation_id, user_id, role, content)
                         VALUES ($1, $2, 'assistant', $3)""",
                    chat_request.conversation_id,
                    user["id"],
                    content,
                )
                await conn.execute(
                    "UPDATE permit_conversations SET updated_at=now() WHERE id=$1",
                    chat_request.conversation_id,
                )

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(STATUS_INTERVAL_SECONDS)
                if run_finished.is_set():
                    return
                summary = (
                    f"Permit Agent is working: {stage}.\n"
                    "The background run is active; another update will appear within two minutes."
                )
                await persist_assistant(summary)

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            scoped_session_id = f"auth-user:{user['id']}:{chat_request.conversation_id}"
            client = await sessions.get_or_create(scoped_session_id)
            async for event in run_turn(client, chat_request.message):
                event_type = event.get("type")
                content = ""
                if event_type in {"text_message", "text_delta"}:
                    content = event.get("text", "").strip()
                elif event_type == "action_needed":
                    content = event.get("message", "").strip()
                elif event_type == "tool_call":
                    name = str(event.get("name") or "research")
                    tool_input = event.get("input") or {}
                    if name in {"Agent", "Task"} and isinstance(tool_input, dict):
                        specialist = (
                            tool_input.get("subagent_type")
                            or tool_input.get("description")
                            or "a specialist subagent"
                        )
                        stage = f"coordinating {specialist}"
                    else:
                        stage = f"completing the {name} stage"
                if content:
                    await persist_assistant(content)
            run_finished.set()
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE permit_chat_runs SET status='completed', completed_at=now()
                         WHERE id=$1 AND status='running'""",
                    run_id,
                )
        except Exception as exc:
            run_finished.set()
            error_text = f"Agent run failed: {exc}"
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO permit_conversation_messages
                           (conversation_id, user_id, role, content)
                         VALUES ($1, $2, 'assistant', $3)""",
                    chat_request.conversation_id,
                    user["id"],
                    error_text,
                )
                await conn.execute(
                    """UPDATE permit_chat_runs
                          SET status='failed', error=$1, completed_at=now()
                        WHERE id=$2""",
                    str(exc),
                    run_id,
                )
        finally:
            run_finished.set()
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    task = asyncio.create_task(execute_run())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return {"accepted": True, "run_id": str(run_id)}
