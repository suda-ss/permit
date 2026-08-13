"""
Interactive CLI driver for the permit research orchestrator, built on the
Claude Agent SDK. Configuration (agent_config.py) and message-to-event
translation (events.py) are shared with the FastAPI backend in
backend/main.py — this file is just the terminal presentation layer.

Run this directly on the host (not in Docker) for local development so the
`claude` CLI subprocess uses your existing `claude login` OAuth session —
no ANTHROPIC_API_KEY needed. Requires a reachable Postgres with pgvector;
see docker-compose.yml (`docker compose up db`) or run Postgres yourself and
point DATABASE_URL at it.

Requires: pip install -r requirements.txt
"""

import asyncio

from claude_agent_sdk import ClaudeSDKClient

from agent_config import build_options
from events import classify_message


def render_event(event: dict) -> None:
    etype = event["type"]
    if etype == "text_delta":
        print(event["text"], end="", flush=True)
    elif etype == "tool_call":
        print(f"\n[tool call: {event['name']} {event['input']}]")
    elif etype == "action_needed":
        print(f"[action needed: {event['message']}]")
    elif etype == "tool_result":
        print(f"\n[tool result: {event['content']}]")
    elif etype == "done":
        print()
        if event.get("cost_usd"):
            print(f"(turn cost: ${event['cost_usd']:.4f})")


async def main() -> None:
    options = build_options()

    print("Permit research orchestrator ready. Type a request, or 'exit' to quit.\n")

    async with ClaudeSDKClient(options=options) as client:
        while True:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit"}:
                break

            await client.query(user_input)

            print("\nAgent: ", end="")
            async for message in client.receive_response():
                for event in classify_message(message):
                    render_event(event)
            print()


if __name__ == "__main__":
    asyncio.run(main())
