"""
Per-browser-tab session registry for the permit research orchestrator.

Each session_id maps to its own ClaudeSDKClient — its own persistent `claude`
CLI subprocess and conversation history, matching the "per-browser-tab
isolated sessions" chat model. Clients idle longer than IDLE_TIMEOUT are
reaped by a background task so subprocesses don't leak indefinitely.

Identical in shape to research_Agent/backend/session_manager.py.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_agent_sdk import ClaudeSDKClient  # noqa: E402

from agent_config import build_options  # noqa: E402

IDLE_TIMEOUT_SECONDS = 30 * 60
REAP_INTERVAL_SECONDS = 60


class SessionManager:
    def __init__(self) -> None:
        self._clients: dict[str, ClaudeSDKClient] = {}
        self._last_used: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._reaper_task: asyncio.Task | None = None

    async def get_or_create(self, session_id: str) -> ClaudeSDKClient:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            client = self._clients.get(session_id)
            if client is None:
                client = ClaudeSDKClient(options=build_options())
                await client.connect()
                self._clients[session_id] = client
            self._last_used[session_id] = time.monotonic()
            return client

    async def close(self, session_id: str) -> None:
        client = self._clients.pop(session_id, None)
        self._last_used.pop(session_id, None)
        self._locks.pop(session_id, None)
        if client is not None:
            await client.disconnect()

    async def _reap_idle(self) -> None:
        while True:
            await asyncio.sleep(REAP_INTERVAL_SECONDS)
            now = time.monotonic()
            idle_ids = [
                sid
                for sid, last in list(self._last_used.items())
                if now - last > IDLE_TIMEOUT_SECONDS
            ]
            for sid in idle_ids:
                await self.close(sid)

    def start_reaper(self) -> None:
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self._reap_idle())

    async def shutdown(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
        for sid in list(self._clients.keys()):
            await self.close(sid)


sessions = SessionManager()
