"""
Recorded demo answers: real runs saved once with a key, replayed later without one.

A recording keeps what the visitor sees (question, answer, references, confidence,
keywords) and the token counts of every model call, so the cost tracker shows the
real cost of the run. It never contains an API key.
"""

from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime, timezone

DEMO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo", "examples")

_KEEP_META = ("keywords", "citation_keywords", "docs_found", "kw_stats")


def make_record(question: str, answer: str, references: list, confidence: dict | None, meta: dict,
                calls: list, saved_eur: float, answer_model: str, structured: dict | None = None,
                settings: dict | None = None) -> dict:
    """A JSON-ready recording of one finished answer. `calls` is a list of costs.Usage."""
    return {
        "version": 1,
        "question": question,
        "answer": answer,
        "structured": structured,
        "references": references,
        "confidence": confidence,
        "meta": {k: meta.get(k) for k in _KEEP_META if k in meta},
        "calls": [u.to_dict() for u in calls],
        "saved_eur": round(saved_eur or 0.0, 6),
        "answer_model": answer_model,
        "settings": settings or {},
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def file_name(record: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", record["question"].lower()).strip("-")[:60] or "example"
    return f"{slug}.json"


def save(record: dict, folder: str | None = None) -> str:
    folder = folder or DEMO_DIR
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, file_name(record))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=1)
    return path


def is_valid(record) -> bool:
    return (isinstance(record, dict) and record.get("version") == 1 and isinstance(record.get("question"), str)
            and isinstance(record.get("answer"), str) and isinstance(record.get("calls"), list))


def load_all(folder: str | None = None) -> list[dict]:
    """All valid recordings, in file-name order."""
    folder = folder or DEMO_DIR
    out = []
    for path in sorted(glob.glob(os.path.join(folder, "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if is_valid(rec):
            out.append(rec)
    return out


def calls_of(record: dict) -> list:
    from .costs import Usage
    return [Usage.from_dict(d) for d in record.get("calls", [])]


def chunks(text: str, size: int = 24):
    """The answer in small pieces, to replay it like a live stream."""
    for i in range(0, len(text), size):
        yield text[i:i + size]
