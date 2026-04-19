"""
Parse a EUR-Lex Markdown document into a hierarchical Article tree,
generate a 1-sentence summary for every node using Claude Haiku,
and persist the result as a local JSON cache file.

Tree structure (saved to data/cache/{year}/{celex}.json):
{
  "doc_id":     "32016R0679",
  "title":      "GDPR ...",
  "year":       2016,
  "date":       "2016-04-27",
  "url":        "https://eur-lex.europa.eu/...",
  "indexed_at": "2024-01-01T12:00:00",
  "nodes": [
    {
      "id":      "art_1",
      "number":  "1",
      "title":   "Subject-matter and objectives",
      "summary": "Establishes the regulation's scope ...",
      "text":    "1. This Regulation lays down rules ..."
    },
    ...
  ]
}
"""

import os
import re
import json
import time
from datetime import datetime, timezone
from typing import Optional

import anthropic
from pipeline.clause_ranker import obligation_score, list_score

# ── Article parsing ───────────────────────────────────────────────────────────

# Matches: "Article 1", "Article 2a", "ARTICLE 12", "## Article 3 Subject-matter"
# Captures: group(1)=number, group(2)=optional inline title
_ART_HEADER = re.compile(
    r"^(?:##*\s*)?(?:ARTICLE|Article)\s+(\d+\w*)(?:\s+(?![-–])(.+))?\s*$",
    re.MULTILINE,
)

# Matches annex headers: "ANNEX I", "Annex II", "ANNEX", "Annex — Title"
_ANNEX_HEADER = re.compile(
    r"^(?:##*\s*)?(?:ANNEX|Annex)\s*([IVX0-9]*[A-Za-z]?)\s*$",
    re.MULTILINE,
)

# Short heading immediately following the article number line
_ART_TITLE_HINT = re.compile(r"^(?:##*\s*)?([A-Z][^\n]{3,80})\s*$")

# Sub-chunking thresholds
SUBCHUNK_THRESHOLD = 5_000   # chars — split articles longer than this
SUBCHUNK_SIZE      = 3_000   # target chars per sub-chunk


# ── PDF text normalization ────────────────────────────────────────────────────

def normalize_pdf_text(text: str) -> str:
    """
    Fix common pypdf extraction artifacts that break article parsing.

    pypdf often produces inter-character spaces: "Ar ticle 5", "A r t i c l e  5".
    It also produces hyphenated line breaks from PDF column layout.
    """
    # Fix hyphenated line breaks: "regu-\nlation" → "regulation"
    text = re.sub(r'(\w)-\n(\w)', r'\1\2', text)

    # Fix inter-character spaces in "Article" keyword (most common artifact)
    # Handles: "Ar ticle", "Ar\nticle", "A r t i c l e"
    text = re.sub(r'A\s*r\s*t\s*i\s*c\s*l\s*e(?=\s+\d)', 'Article', text)

    # Fix "ANNEX" with inter-character spaces
    text = re.sub(r'A\s*N\s*N\s*E\s*X(?=[\s\nIVX0-9])', 'ANNEX', text)

    # Normalize double-spaces on article header lines
    text = re.sub(r'^(Article|ARTICLE)\s{2,}', r'\1 ', text, flags=re.MULTILINE)

    return text


# ── Sub-chunking for long articles ───────────────────────────────────────────

def _subchunk_article(
    art_id: str,
    art_number: str,
    art_title: str,
    text: str,
) -> list:
    """
    Split a long article (>SUBCHUNK_THRESHOLD chars) into navigable sub-nodes.

    Returns [parent_node, sub_node_1, sub_node_2, ...].

    Parent node: short preview text only (first paragraph), carries is_parent=True.
    Sub-nodes: actual content chunks, numbered art_N_1, art_N_2, etc.
    If splitting yields only 1 chunk, returns a single flat node (no split).
    """
    # Split at blank lines or at numbered paragraph starts
    paras = re.split(r'\n{2,}|\n(?=\d+\.\s)', text.strip())
    paras = [p.strip() for p in paras if p.strip()]

    # Group paragraphs into ~SUBCHUNK_SIZE chunks
    chunks: list = []
    current = ""
    for para in paras:
        if len(current) + len(para) + 2 > SUBCHUNK_SIZE and current:
            chunks.append(current.strip())
            current = para
        else:
            current = (current + "\n\n" + para) if current else para
    if current.strip():
        chunks.append(current.strip())

    if len(chunks) <= 1:
        # Not worth splitting
        return [{
            "id":               art_id,
            "number":           art_number,
            "title":            art_title,
            "text":             text[:6000],
            "obligation_score": obligation_score(text),
            "list_score":       list_score(text),
        }]

    # Parent node — preview text only, no full content (avoids duplication in slim tree)
    parent: dict = {
        "id":               art_id,
        "number":           art_number,
        "title":            art_title,
        "text":             paras[0][:500] if paras else "",
        "is_parent":        True,
        "sub_count":        len(chunks),
        "obligation_score": obligation_score(text),
        "list_score":       list_score(text),
    }

    sub_nodes = []
    for k, chunk in enumerate(chunks, 1):
        sub_nodes.append({
            "id":               f"{art_id}_{k}",
            "number":           f"{art_number}.{k}",
            "title":            f"{art_title} (part {k}/{len(chunks)})",
            "text":             chunk,
            "parent_id":        art_id,
            "obligation_score": obligation_score(chunk),
            "list_score":       list_score(chunk),
        })

    return [parent] + sub_nodes


# ── Better doc_body fallback: paragraph-aware chunking ───────────────────────

def _find_operative_boundary(text: str) -> int:
    """
    Find the position where the preamble ends and operative articles begin.
    Looks for "HAS ADOPTED THIS REGULATION:" / "HAVE ADOPTED THIS REGULATION:"
    or falls back to the first Article 1 occurrence.
    Returns character offset or 0 if not found.
    """
    adoption_m = re.search(
        r'(?:HAS|HAVE)\s+ADOPTED\s+THIS\s+(?:REGULATION|DIRECTIVE|DECISION)',
        text,
        re.IGNORECASE,
    )
    if adoption_m:
        return adoption_m.end()
    art1_m = re.search(r'^(?:Article|ARTICLE)\s+1\b', text, re.MULTILINE)
    if art1_m:
        return art1_m.start()
    return 0


def _paragraph_fallback_chunks(text: str, chunk_size: int = 3_000) -> list:
    """
    When no Article headers are found, chunk the full document into
    paragraph-aware sections rather than a single 6000-char truncated node.

    Separates preamble recitals from operative text where possible.
    Returns a list of node dicts (no summaries yet).
    """
    boundary = _find_operative_boundary(text)
    preamble  = text[:boundary].strip() if boundary else ""
    operative = text[boundary:].strip() if boundary else text.strip()

    nodes = []

    if preamble:
        nodes.append({
            "id":               "preamble",
            "number":           "0",
            "title":            "Preamble and Recitals",
            "text":             preamble[:4_000],
            "obligation_score": 0,
            "list_score":       0,
        })

    # Chunk operative text at paragraph boundaries
    paras = re.split(r'\n{2,}', operative)
    chunk_idx = 1
    current = ""
    for para in paras:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 2 > chunk_size and current:
            nodes.append({
                "id":               f"chunk_{chunk_idx}",
                "number":           str(chunk_idx),
                "title":            f"Section {chunk_idx}",
                "text":             current.strip(),
                "obligation_score": obligation_score(current),
                "list_score":       list_score(current),
            })
            chunk_idx += 1
            current = para
        else:
            current = (current + "\n\n" + para) if current else para

    if current.strip():
        nodes.append({
            "id":               f"chunk_{chunk_idx}",
            "number":           str(chunk_idx),
            "title":            f"Section {chunk_idx}",
            "text":             current.strip(),
            "obligation_score": obligation_score(current),
            "list_score":       list_score(current),
        })

    if not nodes:
        # Ultimate fallback — at least return something
        nodes = [{
            "id":               "doc_body",
            "number":           "0",
            "title":            "Full Document",
            "text":             text[:6_000],
            "obligation_score": 0,
            "list_score":       0,
        }]

    return nodes


# ── Article parsing ───────────────────────────────────────────────────────────

def parse_articles(markdown: str) -> list:
    """
    Split a Markdown document into per-article chunks.
    Returns a list of raw article dicts (no summaries yet):
        [{"id", "number", "title", "text", "obligation_score", "list_score"}, ...]

    Long articles (>SUBCHUNK_THRESHOLD chars) are split into sub-nodes.
    When no Article headers are found, falls back to paragraph chunking.
    """
    matches = list(_ART_HEADER.finditer(markdown))
    if not matches:
        return _paragraph_fallback_chunks(markdown)

    articles = []
    for idx, m in enumerate(matches):
        art_num   = m.group(1)
        art_start = m.end()
        art_end   = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown)

        body = markdown[art_start:art_end].strip()

        # Prefer inline title from regex group(2), else look at first line of body
        inline_title = m.group(2)
        if inline_title:
            art_title = inline_title.strip()
        else:
            art_title = f"Article {art_num}"
            first_line_m = _ART_TITLE_HINT.match(body.split("\n")[0].strip()) if body else None
            if first_line_m:
                candidate = first_line_m.group(1).strip("*_ ")
                if not re.match(r"^\d+\.", candidate):
                    art_title = candidate
                    body = "\n".join(body.split("\n")[1:]).strip()

        # Sub-chunk long articles; cap short ones at 6000 chars
        if len(body) > SUBCHUNK_THRESHOLD:
            articles.extend(_subchunk_article(f"art_{art_num}", art_num, art_title, body))
        else:
            articles.append({
                "id":               f"art_{art_num}",
                "number":           art_num,
                "title":            art_title,
                "text":             body[:6_000],
                "obligation_score": obligation_score(body),
                "list_score":       list_score(body),
            })

    # ── Parse annexes as additional nodes ────────────────────────────────────
    annex_matches = list(_ANNEX_HEADER.finditer(markdown))
    for idx, m in enumerate(annex_matches):
        annex_id  = m.group(1).strip() or str(idx + 1)
        ann_start = m.end()
        ann_end   = (annex_matches[idx + 1].start()
                     if idx + 1 < len(annex_matches) else len(markdown))
        body = markdown[ann_start:ann_end].strip()
        if len(body) < 30:
            continue

        ann_title = f"Annex {annex_id}"
        first_line = body.split("\n")[0].strip().strip("*_ ")
        if first_line and not re.match(r"^\d+\.", first_line) and len(first_line) < 100:
            ann_title = first_line
            body = "\n".join(body.split("\n")[1:]).strip()

        if len(body) > SUBCHUNK_THRESHOLD:
            annex_articles = _subchunk_article(
                f"annex_{annex_id.lower().replace(' ', '_')}",
                f"Annex {annex_id}",
                ann_title,
                body,
            )
            articles.extend(annex_articles)
        else:
            articles.append({
                "id":               f"annex_{annex_id.lower().replace(' ', '_')}",
                "number":           f"Annex {annex_id}",
                "title":            ann_title,
                "text":             body[:6_000],
                "obligation_score": obligation_score(body),
                "list_score":       list_score(body),
            })

    return articles


# ── Claude Haiku summarisation ────────────────────────────────────────────────

_SUMMARY_BATCH = 12   # articles per API call (to limit cost)


def _summarise_batch(
    articles: list,
    doc_title: str,
    client: anthropic.Anthropic,
) -> list:
    """
    Ask Claude Haiku to write a 1-sentence summary for each article in the batch.
    Returns a list of summary strings in the same order as *articles*.
    """
    numbered = "\n\n".join(
        f"[{i+1}] Article {a['number']} – {a['title']}\n{a['text'][:800]}"
        for i, a in enumerate(articles)
    )

    prompt = f"""You are a legal analyst summarising articles from an EU legal document.

Document: {doc_title}

Below are {len(articles)} articles. For each one write EXACTLY ONE sentence (max 25 words) that captures its core legal obligation or subject-matter. No introductory text — just the numbered sentences.

{numbered}

Return ONLY a JSON array of {len(articles)} strings, e.g.:
["Summary 1.", "Summary 2.", ...]"""

    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        summaries = json.loads(raw)
        if isinstance(summaries, list) and len(summaries) == len(articles):
            return [str(s) for s in summaries]
    except json.JSONDecodeError:
        pass

    return [f"Covers {a['title']}." for a in articles]


def summarise_articles(
    articles: list,
    doc_title: str,
    client: anthropic.Anthropic,
) -> list:
    """
    Attach a 'summary' field to every article dict.
    Processes in batches to keep token costs low.
    """
    result = []
    for i in range(0, len(articles), _SUMMARY_BATCH):
        batch = articles[i : i + _SUMMARY_BATCH]
        summaries = _summarise_batch(batch, doc_title, client)
        for art, summary in zip(batch, summaries):
            result.append({**art, "summary": summary})
        if i + _SUMMARY_BATCH < len(articles):
            time.sleep(0.5)
    return result


# ── Cache helpers ─────────────────────────────────────────────────────────────

def cache_path(cache_dir: str, year: int, celex: str) -> str:
    year_dir = os.path.join(cache_dir, str(year))
    os.makedirs(year_dir, exist_ok=True)
    safe = re.sub(r"[^\w\-]", "_", celex)
    return os.path.join(year_dir, f"{safe}.json")


def load_tree(path: str) -> Optional[dict]:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_tree(tree: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tree, f, ensure_ascii=False, indent=2)


# ── Public entry point ────────────────────────────────────────────────────────

def build_tree(
    markdown: str,
    doc_id: str,
    title: str,
    year: int,
    date: str,
    url: str,
    cache_dir: str,
    client: anthropic.Anthropic,
    # Legacy positional-call compat: accept celex as first arg too
    celex: Optional[str] = None,
) -> dict:
    """
    Build (or load from cache) a reasoning tree for one EUR-Lex document.

    Accepts both call signatures:
      build_tree(celex, title, date, url, markdown, cache_dir, client)   ← old
      build_tree(markdown, doc_id, title, year, date, url, cache_dir, client) ← new

    Steps:
      1. Check local JSON cache — return immediately if found.
      2. Normalize PDF text artifacts (inter-char spaces, hyphenated line breaks).
      3. Parse Markdown into Article nodes (with sub-chunking for long articles).
      4. Call Claude Haiku to summarise every node.
      5. Save JSON to cache and return the tree.
    """
    # Handle legacy call: build_tree(celex, title, date, url, markdown, cache_dir, client)
    # In old signature: markdown is 5th arg (doc_id slot = celex, year slot = date string)
    # Detect by checking if doc_id looks like a date (YYYY-MM-DD) or a CELEX
    _actual_celex = celex or doc_id
    _actual_year  = year if isinstance(year, int) else (int(str(year)[:4]) if str(year)[:4].isdigit() else 0)

    path = cache_path(cache_dir, _actual_year, _actual_celex)

    cached = load_tree(path)
    if cached:
        return cached

    # Normalize PDF extraction artifacts before parsing
    normalized = normalize_pdf_text(markdown)

    # Parse articles from normalized Markdown
    raw_articles = parse_articles(normalized)

    # Generate summaries
    articles_with_summaries = summarise_articles(raw_articles, title, client)

    tree = {
        "doc_id":     _actual_celex,
        "title":      title,
        "year":       _actual_year,
        "date":       date,
        "url":        url,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        "nodes":      articles_with_summaries,
    }

    save_tree(tree, path)
    return tree
