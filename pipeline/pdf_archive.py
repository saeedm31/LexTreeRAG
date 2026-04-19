"""
Local PDF archive for LexTreeRAG.

Stores raw PDF bytes downloaded from CELLAR at:
    data/pdfs/{year}/{safe_celex}.pdf

An "inbox" folder (data/pdfs/inbox/) is scanned on app startup.
PDFs placed there are automatically indexed (tree built + moved to archive).
"""
from __future__ import annotations

import io
import os
import re
import shutil
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import anthropic

PDF_ARCHIVE_DIR: str = os.getenv("PDF_ARCHIVE_DIR", "./data/pdfs")
PDF_INBOX_DIR:   str = os.path.join(PDF_ARCHIVE_DIR, "inbox")

_CELEX_YEAR_RE = re.compile(r'^[0-9A-Z](\d{4})[A-Z]')
_CELEX_IN_FILENAME_RE = re.compile(r'([0-9][A-Z0-9]{4,20})', re.IGNORECASE)


# ── helpers ───────────────────────────────────────────────────────────────────

def _year_from_celex(celex: str) -> str:
    """Extract 4-digit year from CELEX string. Returns '0' as default."""
    m = _CELEX_YEAR_RE.match(celex.lstrip("0"))
    if m:
        return m.group(1)
    # Try consolidated format: 02013R0168-20241127 → 2013
    m2 = re.search(r'(\d{4})[A-Z]', celex)
    return m2.group(1) if m2 else "0"


def _safe_celex(celex: str) -> str:
    """Replace characters unsafe for filenames."""
    return re.sub(r"[^\w\-]", "_", celex)


# ── public API ────────────────────────────────────────────────────────────────

def archive_path(celex: str) -> str:
    """Return full path to the archived PDF for *celex*. Creates dir if needed."""
    year = _year_from_celex(celex)
    year_dir = os.path.join(PDF_ARCHIVE_DIR, year)
    os.makedirs(year_dir, exist_ok=True)
    return os.path.join(year_dir, f"{_safe_celex(celex)}.pdf")


def is_archived(celex: str) -> bool:
    """Return True if a PDF for *celex* already exists in the archive."""
    return os.path.isfile(archive_path(celex))


def load_pdf_bytes(celex: str) -> Optional[bytes]:
    """Load raw PDF bytes from the archive. Returns None if not found."""
    path = archive_path(celex)
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return fh.read()


def save_pdf_bytes(celex: str, data: bytes) -> str:
    """Save raw PDF bytes to archive. Returns the saved file path."""
    path = archive_path(celex)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def list_archived() -> list:
    """
    Scan the entire archive directory tree and return a list of dicts:
        [{celex, year, path, size_kb}]
    """
    results = []
    if not os.path.isdir(PDF_ARCHIVE_DIR):
        return results
    for root, _dirs, files in os.walk(PDF_ARCHIVE_DIR):
        # Skip the inbox sub-folder
        if os.path.basename(root) == "inbox":
            continue
        for fname in files:
            if not fname.lower().endswith(".pdf"):
                continue
            fpath = os.path.join(root, fname)
            # Reverse the safe_celex transform (underscores may have been slashes etc.)
            celex = fname[:-4]  # strip .pdf
            year = os.path.basename(root)
            size_kb = round(os.path.getsize(fpath) / 1024, 1)
            results.append({"celex": celex, "year": year,
                             "path": fpath, "size_kb": size_kb})
    results.sort(key=lambda d: d["celex"])
    return results


# ── inbox scanner ─────────────────────────────────────────────────────────────

def scan_inbox(
    cache_dir: str,
    client: "anthropic.Anthropic",
    progress_callback: Optional[object] = None,
) -> list:
    """
    Scan PDF_INBOX_DIR for unprocessed PDFs.

    For each .pdf file found:
      - Detect CELEX from filename (e.g. "32019R2144.pdf", "EU_32019R2144_clean.pdf")
      - If CELEX found and JSON tree already in cache → skip
      - If CELEX found and no cache → extract text, build tree, save JSON,
        move PDF from inbox to pdfs/{year}/{celex}.pdf
      - If no CELEX → ingest as generic upload via doc_ingester

    progress_callback: optional callable(msg: str) for UI progress updates.

    Returns list of {filename, status, celex, nodes} where status is one of
    "indexed", "skipped", "upload", "error".
    """
    os.makedirs(PDF_INBOX_DIR, exist_ok=True)
    results = []

    pdf_files = [f for f in os.listdir(PDF_INBOX_DIR) if f.lower().endswith(".pdf")]
    if not pdf_files:
        return results

    # Import here to avoid circular imports at module load
    from pipeline.tree_builder import build_tree, normalize_pdf_text
    from pipeline.doc_ingester import ingest_uploaded_file

    for fname in pdf_files:
        fpath = os.path.join(PDF_INBOX_DIR, fname)
        if progress_callback:
            progress_callback(f"Scanning inbox: {fname}")

        # Try to detect CELEX from filename
        m = _CELEX_IN_FILENAME_RE.search(fname)
        celex = m.group(1).upper() if m else None

        if celex:
            year = _year_from_celex(celex)
            safe = _safe_celex(celex)
            tree_path = os.path.join(cache_dir, year, f"{safe}.json")

            # Already cached → skip without moving
            if os.path.isfile(tree_path):
                results.append({"filename": fname, "status": "skipped",
                                 "celex": celex, "nodes": 0})
                continue

            try:
                # Extract text from PDF
                import pypdf
                with open(fpath, "rb") as fh:
                    reader = pypdf.PdfReader(fh)
                    pages = [page.extract_text() or "" for page in reader.pages]
                raw_text = "\n\n".join(pages)
                raw_text = normalize_pdf_text(raw_text)

                if len(raw_text.strip()) < 200:
                    raise ValueError("PDF text extraction produced too little content")

                # Build article tree
                tree = build_tree(
                    markdown=raw_text,
                    doc_id=celex,
                    title=celex,
                    year=int(year) if year.isdigit() else 0,
                    date="",
                    url="",
                    cache_dir=cache_dir,
                    client=client,
                )

                # Move PDF from inbox to archive
                dest = archive_path(celex)
                shutil.move(fpath, dest)

                n_nodes = len(tree.get("nodes", []))
                results.append({"filename": fname, "status": "indexed",
                                 "celex": celex, "nodes": n_nodes})

            except Exception as exc:
                results.append({"filename": fname, "status": "error",
                                 "celex": celex, "nodes": 0,
                                 "error": str(exc)})
        else:
            # No CELEX detected — treat as a generic upload
            try:
                with open(fpath, "rb") as fh:
                    pdf_bytes = fh.read()
                tree = ingest_uploaded_file(
                    filename=fname,
                    content=pdf_bytes,
                    cache_dir=os.path.join(cache_dir, "uploads"),
                    client=client,
                )
                n_nodes = len(tree.get("nodes", []))
                results.append({"filename": fname, "status": "upload",
                                 "celex": None, "nodes": n_nodes})
            except Exception as exc:
                results.append({"filename": fname, "status": "error",
                                 "celex": None, "nodes": 0,
                                 "error": str(exc)})

    return results
