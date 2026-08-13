"""Local embedding model wrapper.

Loaded once as a module-level singleton (the model is ~130MB and loading it
per-call would be far too slow). Used by mcp_tools.py for both ingestion
(vector-ingest-agent) and query time (vector_search), so embeddings stay
comparable — never swap EMBEDDING_MODEL without re-embedding existing rows.
"""

from __future__ import annotations

import os
import threading

_model = None
_model_lock = threading.Lock()

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIMENSIONS = 384  # must match db/schema.sql's permit_chunks.embedding column


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer

                name = os.environ.get("EMBEDDING_MODEL", DEFAULT_MODEL)
                _model = SentenceTransformer(name, device="cpu")
    return _model


def embed(text: str) -> list[float]:
    return _get_model().encode(text, normalize_embeddings=True).tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    return _get_model().encode(texts, normalize_embeddings=True).tolist()
