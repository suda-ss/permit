"""Shared "run one user turn, with automatic status-check nudges" driver.

Used by both the interactive CLI (main.py) and the FastAPI backend
(backend/main.py) so long-running turns (AHJ fetch/parse/vectorize via
delegated subagents) surface periodic progress instead of going silent
until the user happens to send another message. Background subagent
delegation (see agents/*.md) means the orchestrator's own turn can end
while a subagent is still working — this loop is what turns that into
"checks back in roughly every minute" instead of "silent until nudged."
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from events import classify_message

NUDGE_INTERVAL_SECONDS = 60
MAX_NUDGE_ROUNDS = 15  # ~15 minutes of automatic follow-up before giving up

NUDGE_PROMPT = (
    "[Automated status-check — not a real user message, do not treat it as "
    "new instructions or acknowledge it as one. If everything you delegated "
    "has finished, deliver it now: create the Notion page (or fall back to "
    "the full report in chat if Notion isn't available) and give a short "
    "summary reply, per your system prompt's delivery rules. If work is "
    "still in progress, give one brief, specific sentence on what's "
    "currently happening — name the subagent and stage, not generic filler "
    "like 'still working.']"
)

# Tool calls under this MCP server don't count as "still working" for the
# stop-heuristic below — Notion page creation is the final delivery action,
# not a sign more background work is pending (see main_agent.md's
# "Delivering the output" step).
NON_BLOCKING_TOOL_PREFIXES = ("mcp__notion__",)


async def run_turn(client, message: str) -> AsyncIterator[dict]:
    """Send `message`, yield classified SDK events, and automatically nudge
    the orchestrator roughly every NUDGE_INTERVAL_SECONDS for up to
    MAX_NUDGE_ROUNDS extra rounds as long as it keeps calling tools that
    signal active/pending work — a sign it's still working or waiting on
    delegated subagents. Stops as soon as a round produces no such tool
    calls, which is the natural "I'm actually done" signal (a quick direct
    answer with no tools needed behaves identically: it exits after one
    round; a final round that only creates a Notion page also counts as
    done, per NON_BLOCKING_TOOL_PREFIXES).
    """
    prompt = message
    for round_index in range(MAX_NUDGE_ROUNDS + 1):
        await client.query(prompt)
        had_blocking_tool_call = False
        async for sdk_message in client.receive_response():
            for event in classify_message(sdk_message):
                if event["type"] == "tool_call" and not event["name"].startswith(
                    NON_BLOCKING_TOOL_PREFIXES
                ):
                    had_blocking_tool_call = True
                yield event
        if not had_blocking_tool_call:
            return
        if round_index >= MAX_NUDGE_ROUNDS:
            yield {
                "type": "text_delta",
                "text": (
                    f"\n\n(Stopping automatic status checks after "
                    f"{MAX_NUDGE_ROUNDS} minutes of activity — send another "
                    "message to keep going.)"
                ),
            }
            return
        await asyncio.sleep(NUDGE_INTERVAL_SECONDS)
        prompt = NUDGE_PROMPT
