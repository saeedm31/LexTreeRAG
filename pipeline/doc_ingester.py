"""
Ingest user-uploaded PDF or plain-text files into a reasoning tree.

Produces the same tree dict format as tree_builder.py so the rest of
the pipeline (tree_navigator, answer generation) works completely unchanged.

Persistence:
  Trees are saved to  data/cache/uploads/{safe_filename}.json
  and auto-loaded on every session start — so uploaded files survive
  browser refreshes and server restarts.

Splitting strategy:
  1. Try to split on "Article N" headings (same regex as tree_builder).
  2. If no Article structure is found, split into paragraph-aware chunks
     so that the slim-tree navigator still has meaningful sections to reason over.
"""

import io
import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

import anthropic

from .tree_builder import parse_articles, summarise_articles

# Characters per chunk when no Article headings are found
_CHUNK_SIZE = 2_000

# Sub-directory inside the main cache dir reserved for uploaded files
UPLOAD_SUBDIR = "uploads"


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _safe_name(filename: str) -> str:
    """Sanitise a filename into a safe JSON cache key."""
    return re.sub(r"[^\w\-.]", "_", filename)


def upload_cache_path(cache_dir: str, filename: str) -> str:
    """Return the full path to the JSON cache file for an uploaded document."""
    upload_dir = os.path.join(cache_dir, UPLOAD_SUBDIR)
    os.makedirs(upload_dir, exist_ok=True)
    return os.path.join(upload_dir, f"{_safe_name(filename)}.json")


def _load_cached_upload(path: str) -> Optional[dict]:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _save_upload_tree(tree: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tree, f, ensure_ascii=False, indent=2)


def load_all_upload_trees(cache_dir: str) -> list[dict]:
    """
    Load every previously saved upload tree from disk.
    Called at app startup so archives are available without re-uploading.
    """
    upload_dir = os.path.join(cache_dir, UPLOAD_SUBDIR)
    if not os.path.isdir(upload_dir):
        return []
    trees = []
    for fname in sorted(os.listdir(upload_dir)):
        if fname.endswith(".json"):
            try:
                with open(os.path.join(upload_dir, fname), "r", encoding="utf-8") as f:
                    trees.append(json.load(f))
            except Exception:
                pass
    return trees


def delete_upload_tree(cache_dir: str, filename: str) -> bool:
    """Delete a single upload cache file. Returns True if removed."""
    path = upload_cache_path(cache_dir, filename)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


# ── Text extraction helpers ───────────────────────────────────────────────────

def _extract_pdf_text(file_bytes: bytes) -> str:
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        pages  = [page.extract_text() or "" for page in reader.pages]
        text   = "\n\n".join(pages)
        return re.sub(r"\n{3,}", "\n\n", text).strip()
    except Exception as exc:
        print(f"[doc_ingester] PDF extraction error: {exc}")
        return ""


def _paragraph_chunks(text: str, chunk_size: int = _CHUNK_SIZE) -> list[dict]:
    """
    Split plain text into paragraph-aware chunks.
    Each chunk becomes one node in the reasoning tree.
    """
    paragraphs = re.split(r"\n{2,}", text)
    chunks: list[dict] = []
    current = ""
    idx     = 1

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 2 > chunk_size and current:
            chunks.append({
                "id":     f"chunk_{idx}",
                "number": str(idx),
                "title":  f"Section {idx}",
                "text":   current.strip()[:4000],
            })
            idx    += 1
            current = para
        else:
            current = (current + "\n\n" + para) if current else para

    if current.strip():
        chunks.append({
            "id":     f"chunk_{idx}",
            "number": str(idx),
            "title":  f"Section {idx}",
            "text":   current.strip()[:4000],
        })

    return chunks or [{
        "id": "chunk_1", "number": "1",
        "title": "Full Document", "text": text[:4000],
    }]


def _split_content(text: str) -> list[dict]:
    """Try article-based splitting first; fall back to paragraph chunks."""
    nodes = parse_articles(text)
    # parse_articles returns a single "doc_body" node when no Article headers found
    if len(nodes) == 1 and nodes[0]["id"] == "doc_body":
        return _paragraph_chunks(text)
    return nodes


# ── Public API ────────────────────────────────────────────────────────────────

def build_upload_tree(
    filename: str,
    text: str,
    client: anthropic.Anthropic,
) -> dict:
    """
    Convert raw text into a reasoning tree dict (same schema as tree_builder output).
    Summaries are generated by Claude Haiku, same as for EUR-Lex documents.
    """
    nodes = _split_content(text)
    nodes_with_summaries = summarise_articles(nodes, filename, client)

    return {
        "doc_id":     f"upload::{filename}",
        "title":      filename,
        "year":       0,
        "date":       "",
        "url":        "",
        "source":     "upload",
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        "nodes":      nodes_with_summaries,
    }


def ingest_uploaded_file(
    filename: str,
    file_bytes: bytes,
    client: anthropic.Anthropic,
    cache_dir: str = "./data/cache",
) -> Optional[dict]:
    """
    Parse a PDF/TXT/MD file into a reasoning tree, save it to disk, and return it.

    On subsequent calls with the same filename the cached tree is returned
    immediately — no re-parsing, no LLM calls.

    Returns None if the file cannot be parsed or is too short.
    """
    cache_path = upload_cache_path(cache_dir, filename)

    # Return cached tree if it already exists
    cached = _load_cached_upload(cache_path)
    if cached:
        return cached

    # Extract raw text
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext == "pdf":
        text = _extract_pdf_text(file_bytes)
    elif ext in ("txt", "md"):
        try:
            text = file_bytes.decode("utf-8", errors="replace")
        except Exception:
            return None
    else:
        return None

    text = text.strip()
    if len(text) < 50:
        return None

    # Build tree, persist, and return
    tree = build_upload_tree(filename, text, client)
    _save_upload_tree(tree, cache_path)
    return tree
