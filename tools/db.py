"""Postgres connection pool + schema bootstrap.

One asyncpg pool shared by every tool in mcp_tools.py. The pgvector codec is
registered on every new connection so `permit_chunks.embedding` can be read
and written as plain Python lists, matching tools/embeddings.py's output.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import asyncpg
from pgvector.asyncpg import register_vector

HERE = Path(__file__).resolve().parent.parent
SCHEMA_PATH = HERE / "db" / "schema.sql"

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    await register_vector(conn)
    # asyncpg doesn't decode jsonb by default; submission_rules.steps relies
    # on getting/setting a plain Python list/dict, not a JSON string.
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog", format="text"
    )


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = os.environ["DATABASE_URL"]
        bootstrap_conn = await asyncpg.connect(dsn)
        try:
            await bootstrap_conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        finally:
            await bootstrap_conn.close()

        _pool = await asyncpg.create_pool(dsn, init=_init_connection, min_size=1, max_size=10)
        async with _pool.acquire() as conn:
            await conn.execute(SCHEMA_PATH.read_text())
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
