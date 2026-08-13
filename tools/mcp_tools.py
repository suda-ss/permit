"""Agent-callable Postgres/pgvector/storage tools for the permit research agent.

Everything the four subagents and the main orchestrator do to the knowledge
base goes through these tools — never raw SQL in a prompt. Read tools
(find_ahj, get_structured_permit_data, vector_search) are shared by the
orchestrator and compile-agent; write tools are scoped to whichever
subagent owns that stage (see agent_config.py for the per-agent tool lists).

Embedding computation happens here, inside Python, not something the model
does — mirrors research_Agent/backend/vector_store.py's isolation of the
embedding implementation from agent/ingestion logic.
"""

from __future__ import annotations

import base64
import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from tools import storage
from tools.db import get_pool
from tools.embeddings import embed, embed_batch

CHUNK_MAX_CHARS = 1200
CHUNK_OVERLAP = 180


def _text(payload) -> dict:
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, indent=2, default=str, ensure_ascii=False)}
        ]
    }


async def _get_or_create_permit_type(conn, name: str, category: str | None = None) -> int:
    name = name.strip()
    row = await conn.fetchrow("SELECT id FROM permit_type WHERE name ILIKE $1", name)
    if row:
        return row["id"]
    row = await conn.fetchrow(
        "INSERT INTO permit_type (name, category) VALUES ($1, $2) RETURNING id",
        name, category,
    )
    return row["id"]


async def _touch_ahj_verified(conn, ahj_id: int) -> None:
    await conn.execute(
        "UPDATE ahj SET last_verified_at = now(), updated_at = now() WHERE id = $1", ahj_id
    )


def _chunk_text(text: str, max_chars: int = CHUNK_MAX_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind(". ", start, end))
            if boundary > start + max_chars // 2:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


# ---------------------------------------------------------------------------
# Read tools — main orchestrator + compile-agent
# ---------------------------------------------------------------------------


@tool(
    "find_ahj",
    "Fuzzy-search existing AHJs by name, city, or county so the caller can "
    "decide whether a jurisdiction is already known before delegating a "
    "fetch. Returns up to 5 candidate matches, best first.",
    {"query": str},
)
async def find_ahj(args: dict) -> dict:
    pool = await get_pool()
    query = args["query"].strip()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, slug, name, jurisdiction_type, state, website_url,
                   portal_url, contact_info, last_verified_at,
                   similarity(name, $1) AS match_score
            FROM ahj
            WHERE name ILIKE '%' || $1 || '%'
               OR EXISTS (
                    SELECT 1 FROM unnest(covers) AS c
                    WHERE c ILIKE '%' || $1 || '%' OR $1 ILIKE '%' || c || '%'
               )
            ORDER BY match_score DESC
            LIMIT 5
            """,
            query,
        )
    if not rows:
        return _text({"matches": [], "message": "No matching AHJ found in the database."})
    return _text({"matches": [dict(row) for row in rows]})


@tool(
    "get_structured_permit_data",
    "Read all structured permit data (fees, timelines, deadlines, required "
    "documents, submission rules) known for an AHJ + permit type, plus when "
    "the AHJ was last verified. Use this to decide cache hit/miss and as "
    "the primary source for the final report.",
    {"ahj_id": int, "permit_type": str},
)
async def get_structured_permit_data(args: dict) -> dict:
    pool = await get_pool()
    ahj_id = args["ahj_id"]
    permit_type_name = args["permit_type"].strip()
    async with pool.acquire() as conn:
        ahj_row = await conn.fetchrow("SELECT * FROM ahj WHERE id = $1", ahj_id)
        if not ahj_row:
            return _text({"error": f"No AHJ with id {ahj_id}"})
        pt_row = await conn.fetchrow("SELECT id FROM permit_type WHERE name ILIKE $1", permit_type_name)
        if not pt_row:
            return _text(
                {
                    "ahj": dict(ahj_row),
                    "permit_type": permit_type_name,
                    "found": False,
                    "message": "This permit type has no structured data yet for this AHJ.",
                }
            )
        permit_type_id = pt_row["id"]
        fees = await conn.fetch(
            "SELECT fee_name, amount, unit, calculation_basis, source_url FROM permit_fees "
            "WHERE ahj_id=$1 AND permit_type_id=$2",
            ahj_id, permit_type_id,
        )
        timelines = await conn.fetch(
            "SELECT review_type, duration_days, notes, source_url FROM permit_timelines "
            "WHERE ahj_id=$1 AND permit_type_id=$2",
            ahj_id, permit_type_id,
        )
        deadlines = await conn.fetch(
            "SELECT deadline_type, description, rule, source_url FROM permit_deadlines "
            "WHERE ahj_id=$1 AND permit_type_id=$2",
            ahj_id, permit_type_id,
        )
        documents = await conn.fetch(
            "SELECT document_name, form_number, is_required, download_url, source_url FROM permit_documents "
            "WHERE ahj_id=$1 AND permit_type_id=$2",
            ahj_id, permit_type_id,
        )
        rules = await conn.fetch(
            "SELECT methods, steps, portal_url, source_url FROM submission_rules "
            "WHERE ahj_id=$1 AND permit_type_id=$2",
            ahj_id, permit_type_id,
        )
    found = bool(fees or timelines or deadlines or documents or rules)
    return _text(
        {
            "ahj": dict(ahj_row),
            "permit_type": permit_type_name,
            "found": found,
            "fees": [dict(r) for r in fees],
            "timelines": [dict(r) for r in timelines],
            "deadlines": [dict(r) for r in deadlines],
            "documents": [dict(r) for r in documents],
            "submission_rules": [dict(r) for r in rules],
        }
    )


@tool(
    "vector_search",
    "Semantic search over unstructured permit content (narrative "
    "requirements, exemptions, caveats, code excerpts) for an AHJ, "
    "optionally scoped to a permit type. Use to fill in nuance the "
    "structured tables don't capture.",
    {"ahj_id": int, "permit_type": str, "query": str, "limit": int},
)
async def vector_search(args: dict) -> dict:
    pool = await get_pool()
    ahj_id = args["ahj_id"]
    permit_type_name = (args.get("permit_type") or "").strip()
    query = args["query"]
    limit = max(1, min(args.get("limit", 5), 20))
    query_vector = embed(query)
    async with pool.acquire() as conn:
        pt_id = None
        if permit_type_name:
            pt_row = await conn.fetchrow("SELECT id FROM permit_type WHERE name ILIKE $1", permit_type_name)
            pt_id = pt_row["id"] if pt_row else None
        if pt_id is not None:
            rows = await conn.fetch(
                """
                SELECT section, chunk_text, 1 - (embedding <=> $1) AS score
                FROM permit_chunks
                WHERE ahj_id = $2 AND (permit_type_id = $3 OR permit_type_id IS NULL)
                ORDER BY embedding <=> $1
                LIMIT $4
                """,
                query_vector, ahj_id, pt_id, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT section, chunk_text, 1 - (embedding <=> $1) AS score
                FROM permit_chunks
                WHERE ahj_id = $2
                ORDER BY embedding <=> $1
                LIMIT $3
                """,
                query_vector, ahj_id, limit,
            )
    return _text({"matches": [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# Write tools — ahj-fetch-agent
# ---------------------------------------------------------------------------


@tool(
    "upsert_ahj",
    "Create or update an AHJ record. Use when a jurisdiction isn't already "
    "in the database (find_ahj found nothing usable). 'covers' should list "
    "city/county names this AHJ has authority over, for future address-based "
    "lookups.",
    {
        "name": str,
        "jurisdiction_type": str,
        "state": str,
        "website_url": str,
        "portal_url": str,
        "contact_info": str,
        "covers": list,
    },
)
async def upsert_ahj(args: dict) -> dict:
    pool = await get_pool()
    name = args["name"].strip()
    state = args["state"].strip()
    slug = storage.slugify(f"{name}-{state}")
    covers = args.get("covers") or []
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO ahj (slug, name, jurisdiction_type, state, covers, website_url, portal_url, contact_info)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (slug) DO UPDATE SET
                name = EXCLUDED.name,
                jurisdiction_type = EXCLUDED.jurisdiction_type,
                website_url = COALESCE(EXCLUDED.website_url, ahj.website_url),
                portal_url = COALESCE(EXCLUDED.portal_url, ahj.portal_url),
                contact_info = COALESCE(EXCLUDED.contact_info, ahj.contact_info),
                covers = EXCLUDED.covers,
                updated_at = now()
            RETURNING id, slug
            """,
            slug, name, args["jurisdiction_type"], state, covers,
            args.get("website_url"), args.get("portal_url"), args.get("contact_info"),
        )
    return _text({"ahj_id": row["id"], "slug": row["slug"]})


@tool(
    "save_raw_document",
    "Save a fetched page or file to raw storage (storage/raw/<ahj_slug>/) "
    "and record it against an AHJ. Pass HTML/plain text directly in "
    "'content'; for PDFs or other binary files, base64-encode and set "
    "is_base64=true. Returns extracted plain text so you can confirm what "
    "was captured before moving on.",
    {"ahj_id": int, "source_url": str, "content": str, "content_type": str, "is_base64": bool},
)
async def save_raw_document(args: dict) -> dict:
    pool = await get_pool()
    ahj_id = args["ahj_id"]
    async with pool.acquire() as conn:
        ahj_row = await conn.fetchrow("SELECT slug FROM ahj WHERE id = $1", ahj_id)
        if not ahj_row:
            return _text({"error": f"No AHJ with id {ahj_id}"})
        content_type = args.get("content_type") or "text/plain"
        raw_content: bytes | str = args["content"]
        if args.get("is_base64"):
            raw_content = base64.b64decode(raw_content)
        storage_path, extracted_text = storage.save_document(ahj_row["slug"], raw_content, content_type)
        row = await conn.fetchrow(
            """
            INSERT INTO raw_documents (ahj_id, source_url, storage_path, content_type, extracted_text)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            ahj_id, args["source_url"], storage_path, content_type, extracted_text,
        )
    return _text(
        {
            "document_id": row["id"],
            "storage_path": storage_path,
            "extracted_text_preview": extracted_text[:800],
            "extracted_text_length": len(extracted_text),
        }
    )


# ---------------------------------------------------------------------------
# Shared — parsing-agent + vector-ingest-agent
# ---------------------------------------------------------------------------


@tool(
    "list_unprocessed_documents",
    "List raw documents for an AHJ not yet processed for a given stage. "
    "stage must be 'structured' (for parsing-agent) or 'vector' (for "
    "vector-ingest-agent). Returns each document's extracted_text to work from.",
    {"ahj_id": int, "stage": str},
)
async def list_unprocessed_documents(args: dict) -> dict:
    pool = await get_pool()
    ahj_id = args["ahj_id"]
    column = "processed_structured" if args["stage"] == "structured" else "processed_vector"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, source_url, storage_path, content_type, extracted_text, fetched_at
            FROM raw_documents
            WHERE ahj_id = $1 AND NOT {column}
            ORDER BY fetched_at
            """,
            ahj_id,
        )
    return _text({"documents": [dict(r) for r in rows]})


@tool(
    "mark_document_processed",
    "Mark a raw document as processed for a given stage ('structured' or "
    "'vector'). Call once you've fully extracted what you need from it.",
    {"document_id": int, "stage": str},
)
async def mark_document_processed(args: dict) -> dict:
    pool = await get_pool()
    column = "processed_structured" if args["stage"] == "structured" else "processed_vector"
    async with pool.acquire() as conn:
        await conn.execute(f"UPDATE raw_documents SET {column} = true WHERE id = $1", args["document_id"])
    return _text({"ok": True})


# ---------------------------------------------------------------------------
# Write tools — parsing-agent
# ---------------------------------------------------------------------------


@tool(
    "upsert_permit_fees",
    "Replace the entire fee schedule for an AHJ + permit type. Each item in "
    "'fees': {fee_name, amount (number or null), unit, calculation_basis, "
    "source_url}. Only include fees explicitly stated in the source.",
    {"ahj_id": int, "permit_type": str, "fees": list},
)
async def upsert_permit_fees(args: dict) -> dict:
    pool = await get_pool()
    ahj_id = args["ahj_id"]
    async with pool.acquire() as conn, conn.transaction():
        pt_id = await _get_or_create_permit_type(conn, args["permit_type"])
        await conn.execute("DELETE FROM permit_fees WHERE ahj_id=$1 AND permit_type_id=$2", ahj_id, pt_id)
        for fee in args.get("fees", []):
            await conn.execute(
                """
                INSERT INTO permit_fees (ahj_id, permit_type_id, fee_name, amount, unit, calculation_basis, source_url)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                ahj_id, pt_id, fee.get("fee_name"), fee.get("amount"), fee.get("unit"),
                fee.get("calculation_basis"), fee.get("source_url"),
            )
        await _touch_ahj_verified(conn, ahj_id)
    return _text({"ok": True, "count": len(args.get("fees", []))})


@tool(
    "upsert_permit_timeline",
    "Replace review-timeline entries for an AHJ + permit type. Each item in "
    "'timelines': {review_type, duration_days (integer or null), notes, source_url}.",
    {"ahj_id": int, "permit_type": str, "timelines": list},
)
async def upsert_permit_timeline(args: dict) -> dict:
    pool = await get_pool()
    ahj_id = args["ahj_id"]
    async with pool.acquire() as conn, conn.transaction():
        pt_id = await _get_or_create_permit_type(conn, args["permit_type"])
        await conn.execute("DELETE FROM permit_timelines WHERE ahj_id=$1 AND permit_type_id=$2", ahj_id, pt_id)
        for entry in args.get("timelines", []):
            await conn.execute(
                """
                INSERT INTO permit_timelines (ahj_id, permit_type_id, review_type, duration_days, notes, source_url)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                ahj_id, pt_id, entry.get("review_type"), entry.get("duration_days"),
                entry.get("notes"), entry.get("source_url"),
            )
        await _touch_ahj_verified(conn, ahj_id)
    return _text({"ok": True, "count": len(args.get("timelines", []))})


@tool(
    "upsert_permit_deadlines",
    "Replace deadline entries for an AHJ + permit type. Each item in "
    "'deadlines': {deadline_type, description, rule, source_url}.",
    {"ahj_id": int, "permit_type": str, "deadlines": list},
)
async def upsert_permit_deadlines(args: dict) -> dict:
    pool = await get_pool()
    ahj_id = args["ahj_id"]
    async with pool.acquire() as conn, conn.transaction():
        pt_id = await _get_or_create_permit_type(conn, args["permit_type"])
        await conn.execute("DELETE FROM permit_deadlines WHERE ahj_id=$1 AND permit_type_id=$2", ahj_id, pt_id)
        for entry in args.get("deadlines", []):
            await conn.execute(
                """
                INSERT INTO permit_deadlines (ahj_id, permit_type_id, deadline_type, description, rule, source_url)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                ahj_id, pt_id, entry.get("deadline_type"), entry.get("description"),
                entry.get("rule"), entry.get("source_url"),
            )
        await _touch_ahj_verified(conn, ahj_id)
    return _text({"ok": True, "count": len(args.get("deadlines", []))})


@tool(
    "upsert_permit_documents",
    "Replace required-document/form entries for an AHJ + permit type. Each "
    "item in 'documents': {document_name, form_number, is_required (bool), "
    "download_url, source_url}.",
    {"ahj_id": int, "permit_type": str, "documents": list},
)
async def upsert_permit_documents(args: dict) -> dict:
    pool = await get_pool()
    ahj_id = args["ahj_id"]
    async with pool.acquire() as conn, conn.transaction():
        pt_id = await _get_or_create_permit_type(conn, args["permit_type"])
        await conn.execute("DELETE FROM permit_documents WHERE ahj_id=$1 AND permit_type_id=$2", ahj_id, pt_id)
        for entry in args.get("documents", []):
            await conn.execute(
                """
                INSERT INTO permit_documents (ahj_id, permit_type_id, document_name, form_number, is_required, download_url, source_url)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                ahj_id, pt_id, entry.get("document_name"), entry.get("form_number"),
                entry.get("is_required", True), entry.get("download_url"), entry.get("source_url"),
            )
        await _touch_ahj_verified(conn, ahj_id)
    return _text({"ok": True, "count": len(args.get("documents", []))})


@tool(
    "upsert_submission_rules",
    "Replace the submission process for an AHJ + permit type: how and in "
    "what order to submit. 'methods' e.g. ['online_portal','mail','in_person']; "
    "'steps' an ordered list of short step descriptions.",
    {"ahj_id": int, "permit_type": str, "methods": list, "steps": list, "portal_url": str, "source_url": str},
)
async def upsert_submission_rules(args: dict) -> dict:
    pool = await get_pool()
    ahj_id = args["ahj_id"]
    async with pool.acquire() as conn, conn.transaction():
        pt_id = await _get_or_create_permit_type(conn, args["permit_type"])
        await conn.execute("DELETE FROM submission_rules WHERE ahj_id=$1 AND permit_type_id=$2", ahj_id, pt_id)
        await conn.execute(
            """
            INSERT INTO submission_rules (ahj_id, permit_type_id, methods, steps, portal_url, source_url)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            ahj_id, pt_id, args.get("methods", []), args.get("steps", []),
            args.get("portal_url"), args.get("source_url"),
        )
        await _touch_ahj_verified(conn, ahj_id)
    return _text({"ok": True})


# ---------------------------------------------------------------------------
# Write tool — vector-ingest-agent
# ---------------------------------------------------------------------------


@tool(
    "ingest_unstructured_chunks",
    "Chunk and embed narrative/unstructured text from one raw document and "
    "store it for semantic search (vector_search). Call once per raw "
    "document, or once per logical section for very long documents. "
    "'permit_type' may be omitted for content that applies to the AHJ generally.",
    {"ahj_id": int, "permit_type": str, "raw_document_id": int, "text": str, "section": str},
)
async def ingest_unstructured_chunks(args: dict) -> dict:
    pool = await get_pool()
    ahj_id = args["ahj_id"]
    raw_document_id = args["raw_document_id"]
    section = args.get("section") or ""
    chunks = _chunk_text(args["text"])
    if not chunks:
        return _text({"ok": True, "count": 0})
    vectors = embed_batch(chunks)
    async with pool.acquire() as conn:
        pt_id = None
        permit_type_name = (args.get("permit_type") or "").strip()
        if permit_type_name:
            pt_id = await _get_or_create_permit_type(conn, permit_type_name)
        async with conn.transaction():
            for chunk, vector in zip(chunks, vectors):
                await conn.execute(
                    """
                    INSERT INTO permit_chunks (ahj_id, permit_type_id, raw_document_id, section, chunk_text, embedding)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    ahj_id, pt_id, raw_document_id, section, chunk, vector,
                )
    return _text({"ok": True, "count": len(chunks)})


permit_db_server = create_sdk_mcp_server(
    name="permit-db",
    version="1.0.0",
    tools=[
        find_ahj,
        get_structured_permit_data,
        vector_search,
        upsert_ahj,
        save_raw_document,
        list_unprocessed_documents,
        mark_document_processed,
        upsert_permit_fees,
        upsert_permit_timeline,
        upsert_permit_deadlines,
        upsert_permit_documents,
        upsert_submission_rules,
        ingest_unstructured_chunks,
    ],
)
