"""
Shared Claude Agent SDK configuration for the permit research orchestrator.

Used by both the interactive CLI (main.py) and the FastAPI backend
(backend/main.py) so the two never drift out of sync.

Subagents are defined here as AgentDefinitions and handed to the SDK
directly via ClaudeAgentOptions.agents — not discovered from
.claude/agents/*.md on disk. Each subagent's system prompt lives in its own
file under agents/ (plain prose, no frontmatter); the structural fields
(description, tools, model) that actually drive registration stay here in
Python, next to where they're used. The skill
(.claude/skills/permit-research/SKILL.md) is still filesystem-discovered via
setting_sources, since the SDK has no equivalent inline-skill option.

Mirrors research_Agent/agent_config.py's shape; see that file for the
pattern this was modeled on.
"""

from pathlib import Path

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions
from dotenv import load_dotenv

from tools.mcp_tools import permit_db_server

HERE = Path(__file__).parent
AGENTS_DIR = HERE / "agents"

# Populates os.environ from .env for local runs (CLI, bare uvicorn). In
# Docker this is a no-op fallback — the container's real env vars (from
# --env-file/env_file:) are already set and load_dotenv() never overrides an
# existing value.
load_dotenv(HERE / ".env")

# The dict key under ClaudeAgentOptions.mcp_servers below ("permitdb") is
# what determines each tool's "mcp__permitdb__<tool_name>" name — not the
# `name=` passed to create_sdk_mcp_server() in tools/mcp_tools.py.
DB_READ_TOOLS = [
    "mcp__permitdb__find_ahj",
    "mcp__permitdb__get_structured_permit_data",
    "mcp__permitdb__vector_search",
]

# Built-in tools available to the orchestrator. Subagents get their own,
# narrower lists below.
ALLOWED_TOOLS = [
    "Task",  # required — lets the orchestrator delegate to subagents
    *DB_READ_TOOLS,
]


def _agent_prompt(name: str) -> str:
    return (AGENTS_DIR / f"{name}.md").read_text()


AGENTS: dict[str, AgentDefinition] = {
    "ahj-fetch-agent": AgentDefinition(
        description=(
            "Finds a jurisdiction's (AHJ's) official permitting website or "
            "portal, crawls it, and saves raw source documents. Use on a "
            "cache miss — when find_ahj/get_structured_permit_data show the "
            "AHJ is missing or its data is stale."
        ),
        tools=[
            "WebSearch",
            "WebFetch",
            "Bash",
            "mcp__permitdb__find_ahj",
            "mcp__permitdb__upsert_ahj",
            "mcp__permitdb__save_raw_document",
        ],
        model="sonnet",
        prompt=_agent_prompt("ahj-fetch-agent"),
    ),
    "parsing-agent": AgentDefinition(
        description=(
            "Extracts structured permit fields — fees, timelines, "
            "deadlines, required documents/forms, submission rules — from "
            "raw AHJ documents into Postgres. Use after ahj-fetch-agent has "
            "saved new raw documents."
        ),
        tools=[
            "Read",
            "Grep",
            "mcp__permitdb__list_unprocessed_documents",
            "mcp__permitdb__upsert_permit_fees",
            "mcp__permitdb__upsert_permit_timeline",
            "mcp__permitdb__upsert_permit_deadlines",
            "mcp__permitdb__upsert_permit_documents",
            "mcp__permitdb__upsert_submission_rules",
            "mcp__permitdb__mark_document_processed",
        ],
        model="sonnet",
        prompt=_agent_prompt("parsing-agent"),
    ),
    "vector-ingest-agent": AgentDefinition(
        description=(
            "Embeds narrative/unstructured content from raw AHJ documents "
            "into pgvector for semantic search. Runs in parallel with "
            "parsing-agent on the same raw documents."
        ),
        tools=[
            "Read",
            "mcp__permitdb__list_unprocessed_documents",
            "mcp__permitdb__ingest_unstructured_chunks",
            "mcp__permitdb__mark_document_processed",
        ],
        model="sonnet",
        prompt=_agent_prompt("vector-ingest-agent"),
    ),
    "compile-agent": AgentDefinition(
        description=(
            "Reads structured + vector permit data for an AHJ/permit type "
            "and produces the final user-facing report. Read-only — always "
            "the last step, on both cache hits and cache misses."
        ),
        tools=[
            "mcp__permitdb__get_structured_permit_data",
            "mcp__permitdb__vector_search",
        ],
        model="sonnet",
        prompt=_agent_prompt("compile-agent"),
    ),
}


def build_options() -> ClaudeAgentOptions:
    system_prompt = (HERE / "main_agent.md").read_text()

    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        cwd=str(HERE),
        # Skill (.claude/skills/*/SKILL.md) is filesystem-discovered.
        # Subagents are NOT — they come from `agents=AGENTS` above, whose
        # prompt text is loaded from agents/*.md.
        setting_sources=["project"],
        agents=AGENTS,
        allowed_tools=ALLOWED_TOOLS,
        # This is a fully headless multi-agent backend — no one is present to
        # interactively approve WebSearch/WebFetch/Bash prompts turn-by-turn
        # (confirmed by testing: "acceptEdits" only auto-approves file edits,
        # leaving ahj-fetch-agent's WebSearch calls permanently stuck on an
        # unanswerable permission prompt). allowed_tools above is already the
        # real access-control boundary; bypassPermissions just stops the CLI
        # from *also* pausing for interactive confirmation on top of that.
        permission_mode="bypassPermissions",
        # Declared explicitly rather than relying on .mcp.json auto-discovery:
        # the SDK only forwards MCP servers to the CLI subprocess when
        # they're passed here, and a headless subprocess has no way to
        # answer the interactive "trust this server?" prompt .mcp.json
        # auto-discovery would otherwise trigger.
        mcp_servers={"permitdb": permit_db_server},
    )
