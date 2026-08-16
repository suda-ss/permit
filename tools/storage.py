"""Raw document storage.

Writes fetched content to storage/raw/<ahj_slug>/<timestamp>.<ext> and
extracts plain text from it, so parsing-agent and vector-ingest-agent never
have to deal with HTML/PDF parsing themselves — they read `extracted_text`
off the `raw_documents` row.

Deliberately a plain local folder for now (per the user's request); swapping
this for S3/blob storage later only requires changing `save_document`, not
any agent prompt or the mcp_tools call sites.
"""

from __future__ import annotations

import io
import os
import re
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
_raw_storage_dir_env = os.environ.get("RAW_STORAGE_DIR")
# Join against HERE even when the env var is set, so a relative value like
# "storage/raw" (as exported by local_setup.sh) still resolves to an
# absolute path under the project root. If the env var is already absolute,
# the "/" join discards HERE automatically (pathlib semantics), so this is
# safe either way.
RAW_DIR = (HERE / _raw_storage_dir_env) if _raw_storage_dir_env else (HERE / "storage" / "raw")

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_WS_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")

_EXTENSION_BY_CONTENT_TYPE = {
    "application/pdf": ".pdf",
    "text/html": ".html",
    "text/plain": ".txt",
}


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "unknown"


def html_to_text(html: str) -> str:
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    text = _INLINE_WS_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def pdf_to_text(content: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages).strip()


def save_document(
    ahj_slug: str, content: bytes | str, content_type: str
) -> tuple[str, str]:
    """Write content under storage/raw/<ahj_slug>/ and return
    (storage_path relative to the project root, extracted_text)."""
    ahj_dir = RAW_DIR / slugify(ahj_slug)
    ahj_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    ext = _EXTENSION_BY_CONTENT_TYPE.get(content_type, ".bin")
    file_path = ahj_dir / f"{timestamp}{ext}"

    if isinstance(content, str):
        file_path.write_text(content, encoding="utf-8")
    else:
        file_path.write_bytes(content)

    if content_type == "application/pdf":
        raw_bytes = content if isinstance(content, bytes) else content.encode("utf-8")
        extracted = pdf_to_text(raw_bytes)
    elif content_type == "text/html":
        raw_text = content if isinstance(content, str) else content.decode("utf-8", errors="ignore")
        extracted = html_to_text(raw_text)
    else:
        extracted = content if isinstance(content, str) else content.decode("utf-8", errors="ignore")

    storage_path = str(file_path.relative_to(HERE))
    return storage_path, extracted
