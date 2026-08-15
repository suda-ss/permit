"""Shared message -> event translation for the permit research orchestrator.

Turns Claude Agent SDK message objects into a small, stable event protocol
used by both the interactive CLI (main.py, which prints events) and the
FastAPI backend (backend/main.py, which streams events as JSON lines to the
browser). Identical in shape and purpose to research_Agent/events.py — kept
as a separate copy so the two projects can evolve independently.

Event shapes:
  {"type": "text_message", "text": str}
  {"type": "tool_call",    "name": str, "input": dict}
  {"type": "action_needed","message": str}
  {"type": "tool_result",  "content": ...}
  {"type": "done",         "cost_usd": float | None}
"""

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)


def classify_message(message) -> list[dict]:
    """Translate one SDK message into zero or more protocol events.

    Tool calls (ToolUseBlock) arrive on AssistantMessage; their results
    (ToolResultBlock) arrive on the *following* UserMessage, not the same
    message — both must be handled.
    """
    events: list[dict] = []

    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock):
                # An SDK TextBlock is a complete assistant message, not a
                # token delta. Preserve that boundary so the UI and stored
                # conversation render each agent update separately.
                events.append({"type": "text_message", "text": block.text})
            elif isinstance(block, ToolUseBlock):
                events.append(
                    {"type": "tool_call", "name": block.name, "input": block.input}
                )
                if block.name.endswith("__authenticate"):
                    events.append(
                        {
                            "type": "action_needed",
                            "message": (
                                "Open the authorization URL above in your "
                                "browser, then send another message to continue."
                            ),
                        }
                    )
    elif isinstance(message, UserMessage):
        content = message.content
        blocks = content if isinstance(content, list) else []
        for block in blocks:
            if isinstance(block, ToolResultBlock):
                events.append({"type": "tool_result", "content": block.content})
    elif isinstance(message, ResultMessage):
        events.append(
            {"type": "done", "cost_usd": getattr(message, "total_cost_usd", None)}
        )

    return events
