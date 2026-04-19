"""
LexTreeRAG — Streamlit Web UI
==============================
Run with:
    conda activate rag_app
    streamlit run app.py
"""

from __future__ import annotations
import os
import json
import glob
import time
import threading
from pathlib import Path
from datetime import datetime

import streamlit as st
from dotenv import load_dotenv
import anthropic

from pipeline.keyword_extractor import extract_keywords_and_year
from pipeline.eurolex_client    import search_eurlex, fetch_document_markdown, \
                                       extract_cited_regulations, fetch_by_citation, \
                                       fetch_documents_parallel, follow_citation_links
from pipeline.tree_builder      import build_tree
from pipeline.tree_navigator    import navigate_tree
from pipeline.gemini_search     import get_gemini_client, validate_gemini_key, \
                                       gemini_answer_stream, gemini_answer
from pipeline.doc_ingester      import (
    ingest_uploaded_file, load_all_upload_trees, delete_upload_tree
)
from pipeline.query_classifier  import classify_question
from pipeline.answer_verifier   import compute_confidence
from pipeline.clause_ranker     import (
    rerank_nodes, has_answer_signal, obligation_expansion_keywords
)
from pipeline.pdf_archive       import scan_inbox, list_archived

load_dotenv()

# ── page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="LexTreeRAG",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── global CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  .ref-box  { background:#1e2535; border-radius:8px; padding:12px 16px;
              margin-bottom:8px; border-left:3px solid #4a9eff; font-size:.85rem; }
  .celex-tag{ background:#2a3650; border-radius:4px; padding:2px 7px;
              font-family:monospace; color:#7dd3fc; font-weight:600; }
  .year-tag { color:#a3e635; font-weight:600; }
  .stChatMessage p { font-size: .95rem; }
</style>
""", unsafe_allow_html=True)

# ── session state defaults ─────────────────────────────────────────────────────
def _init():
    defaults = {
        "history":        [],      # [{role, content, references, meta}]
        "references":     [],      # references from the last answer
        "client":         None,
        "api_key_ok":     False,
        "gemini_client":  None,
        "gemini_key_ok":  False,
        "gemini_model":   "",      # detected at validation time
        "gemini_last_q":  "",      # last question sent to Gemini
        "gemini_last_ans":"",      # last Gemini answer text
        "gemini_refresh": False,   # flag: re-run Gemini for last question
        "upload_trees":        [],     # reasoning trees built from uploaded files
        "uploads_loaded_once": False,  # guard: load archive only on first run
        "prev_selected_nodes": [],     # conversation memory: last answer's selected nodes
        "last_query_type":     {},     # classification of last question
        "structured_mode":     False,  # structured JSON output toggle
        "kw_stats":            [],     # keyword hit stats from last query
        "kw_docs_found":       0,      # total docs scanned for stats
        "last_question":       "",     # question for focus re-run
        "focus_rerun_q":       None,   # pending focus re-run question
        "focus_rerun_kws":     [],     # keywords to emphasise in re-run
        "pdf_inbox_scanned":   False,  # guard: scan inbox only once per session
        "discovery_mode":      False,  # keyword discovery UI active
        "discovery_suggested": [],     # suggested keywords from last low-confidence answer
        "retry_count":         0,      # number of retries for last query
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init()

CACHE_DIR = os.getenv("CACHE_DIR", "./data/cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# ── Auto-load upload archive on first run of each session ─────────────────────
if not st.session_state.uploads_loaded_once:
    st.session_state.upload_trees        = load_all_upload_trees(CACHE_DIR)
    st.session_state.uploads_loaded_once = True

# ── Scan PDF inbox on first run (auto-index PDFs placed in data/pdfs/inbox/) ──
if not st.session_state.pdf_inbox_scanned and st.session_state.client:
    try:
        _inbox_results = scan_inbox(CACHE_DIR, st.session_state.client)
        _n_indexed = sum(1 for r in _inbox_results if r["status"] == "indexed")
        if _n_indexed:
            st.sidebar.success(f"Auto-indexed {_n_indexed} PDF(s) from inbox")
    except Exception:
        pass
    st.session_state.pdf_inbox_scanned = True

# ── CELEX sector definitions ───────────────────────────────────────────────────
SECTORS = {
    "3": "3 · Legislation (Regulations, Directives, Decisions)",
    "0": "0 · Consolidated acts",
    "1": "1 · Treaties",
    "2": "2 · International agreements",
    "4": "4 · Complementary legislation",
    "5": "5 · Preparatory acts & working documents",
    "6": "6 · Case-law (CJEU)",
    "7": "7 · National transposition measures",
    "8": "8 · National case-law on EU law",
    "9": "9 · Parliamentary questions",
    "C": "C · OJ C-series (other official documents)",
    "E": "E · EFTA documents",
}

# ── Anthropic client ───────────────────────────────────────────────────────────
def _get_client(api_key: str) -> anthropic.Anthropic | None:
    if not api_key:
        return None
    try:
        c = anthropic.Anthropic(api_key=api_key)
        # Quick ping to validate key
        c.models.list()
        return c
    except Exception:
        return None


# ── Cache helpers ──────────────────────────────────────────────────────────────
def list_cached_trees() -> list[dict]:
    pattern = os.path.join(CACHE_DIR, "**", "*.json")
    rows = []
    for f in sorted(glob.glob(pattern, recursive=True)):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
            rows.append({
                "Select":    False,
                "CELEX":     d.get("doc_id", "?"),
                "Title":     d.get("title", "?")[:70],
                "Year":      str(d.get("year", "?")),
                "Articles":  len(d.get("nodes", [])),
                "Cached":    d.get("indexed_at", "?")[:10],
                "_file":     f,
            })
        except Exception:
            pass
    return rows


def delete_trees(files: list[str]) -> int:
    removed = 0
    for f in files:
        try:
            os.remove(f)
            removed += 1
        except Exception:
            pass
    return removed


# ── Multi-year search ──────────────────────────────────────────────────────────
def search_years(
    keywords:  list[str],
    year:      int,
    delta:     int,
    max_laws:  int,
    sectors:   tuple[str, ...] = ("3",),
) -> list[dict]:
    """Search year-delta … year+delta, deduplicate by CELEX."""
    seen  = set()
    docs  = []
    years = list(range(year - delta, year + delta + 1))
    per_y = max(1, max_laws // len(years)) if years else max_laws

    for y in years:
        try:
            found = search_eurlex(keywords, y, max_results=per_y, sectors=sectors)
            for d in found:
                if d["celex"] not in seen:
                    seen.add(d["celex"])
                    docs.append(d)
        except Exception as exc:
            st.warning(f"Search error for year {y}: {exc}")
    return docs[:max_laws]


# ── Answer streamer ────────────────────────────────────────────────────────────
_ANSWER_SYSTEM = """\
You are an expert EU legal analyst. Answer based ONLY on the EUR-Lex articles provided.

STRICT RULES — follow in this order:

1. TARGET EXACT PROVISIONS
   Prefer specific legal clauses, paragraphs, and annexes over general/framework articles.
   Do NOT stop at high-level articles (e.g. Article 1 – scope, definitions).
   Prioritise text containing: "shall", "must", "required to", "shall provide", "obliged to".

2. LIST-FIRST EXTRACTION (CRITICAL)
   If the question asks for requirements, documents, conditions, or steps:
   → Return a structured numbered or bulleted list.
   → Extract ALL items explicitly — e.g. (a), (b), (c) — do NOT summarise or collapse them.
   → Never replace a list with a general explanation.
   → Look for obligation phrasing: "documentation required", "evidence to substantiate",
     "holder shall have available", "proof of".

3. AVOID CONTEXT DRIFT
   Do NOT cite broad framework or scope articles if a precise provision exists.
   Ignore meta-text ("Commission may adopt", "delegated acts", "implementing acts")
   unless it is the only text available and directly relevant.

4. EXACT MATCH PRIORITY
   If the question names a regulation number (e.g. 2019/631) or article number
   (e.g. Article 73), cite that exact source. Do not substitute a related article.

5. NO FALSE "MISSING INFO"
   Only say "information not available" if it is truly absent from the provided text.
   If a relevant clause or list exists → you MUST use it.

6. NO SPECULATION (CRITICAL)
   If the exact answer is NOT present in the retrieved text:
   → DO NOT guess, infer, or extrapolate from related provisions.
   → DO NOT describe what an article "likely contains" or "probably covers".
   → DO NOT paraphrase beyond what the text explicitly states.
   → State clearly: "This information is not in the retrieved articles."
   Every factual claim must be traceable to a verbatim or near-verbatim passage.

7. SELF-CHECK BEFORE ANSWERING
   • Does the answer directly respond to the question?
   • If a list is required → is it fully extracted and included?
   • Is every statement traceable to a specific retrieved passage (not inferred)?
   • Is the answer based on a specific clause (not general context)?
   If any check fails → revise the answer or state the information is not available.

8. OUTPUT FORMAT
   Clear, concise, structured. Use bullet points or numbered lists where applicable.
   Cite the specific Article number and regulation name for every claim.
   End with a '## References' section listing each article used.
"""

_ANSWER_SYSTEM_STRUCTURED = """\
You are an expert EU legal analyst. Answer based ONLY on the EUR-Lex articles provided.

STRICT RULES:
1. TARGET EXACT PROVISIONS — prefer "shall"/"must"/"required to"/"shall provide" clauses.
   Prefer specific paragraphs and annexes over high-level framework articles.
2. LIST-FIRST — for requirements, conditions, or steps: extract ALL items (a),(b),(c) explicitly.
   Look for obligation phrasing: "documentation required", "evidence to substantiate",
   "holder shall have available". Never summarise a list into a sentence.
3. AVOID CONTEXT DRIFT — skip broad framework articles if a specific provision exists.
   Ignore "Commission may adopt", "delegated acts", "implementing acts" unless nothing more specific exists.
4. EXACT MATCH — if question names a regulation or article number, cite exactly that source.
5. NO FALSE GAPS — only omit information if it is truly absent from the provided text.
6. NO SPECULATION (CRITICAL) — if the exact answer is not in the retrieved text:
   DO NOT guess, infer, or describe what an article "likely contains".
   DO NOT paraphrase beyond what the text explicitly states.
   Every value in the JSON must be traceable to a verbatim passage.
   Use "caveats" to record anything genuinely absent.
7. SELF-CHECK — verify every statement is grounded in the article text before including it.
   If the answer relies only on general/scope articles, mark caveats accordingly.

Return your answer as a single valid JSON object (omit keys that are not applicable):
{
  "summary":     "2-3 sentence plain-English overview — directly answering the question",
  "obligations": ["exact obligation from text (cite article)", "..."],
  "rights":      ["exact right from text (cite article)"],
  "definitions": {"term": "verbatim or near-verbatim definition from the article"},
  "exceptions":  ["exact exception or derogation (cite article)"],
  "procedure":   ["step 1 (cite article)", "step 2"],
  "references":  [
    {
      "celex":     "CELEX number",
      "article":   "Article X — title",
      "quote":     "verbatim key phrase from the article",
      "relevance": "why this article directly answers the question"
    }
  ],
  "caveats": "only if information is genuinely absent from the retrieved articles"
}
Return ONLY the JSON object — no markdown fences, no prose before or after.
"""


def _build_context(selected: list[dict]) -> str:
    parts = []
    for i, n in enumerate(selected, 1):
        parts.append(
            f"[{i}] {n.get('doc_title', n.get('doc_id', '?'))} ({n.get('year', '?')}) — "
            f"Article {n.get('title', n.get('node_id', '?'))}\n"
            f"CELEX: {n.get('doc_id', '?')}  |  {n.get('doc_url', '')}\n\n"
            f"{n.get('text', '')}"
        )
    return "\n\n---\n\n".join(parts)


def _paragraph_limit_rule(max_paragraphs: int) -> str:
    return (
        f"\n\n9. ANSWER LENGTH (HARD LIMIT)\n"
        f"   Your complete answer — excluding the '## References' section — "
        f"must contain AT MOST {max_paragraphs} paragraph(s).\n"
        f"   A paragraph is any block of prose or a list block separated by a blank line.\n"
        f"   Prioritise the most critical legal information. Cut context and caveats first."
    )


def answer_stream(question: str, selected: list[dict], client,
                  max_paragraphs: int | None = None):
    """Generator: yields text tokens from Claude Opus streaming (prose mode)."""
    context = _build_context(selected)
    prompt  = (
        f"Use the EUR-Lex articles below to answer the question.\n\n"
        f"QUESTION: {question}\n\n"
        f"ARTICLES:\n{context}"
    )
    system = _ANSWER_SYSTEM
    if max_paragraphs is not None:
        system = system + _paragraph_limit_rule(max_paragraphs)

    with client.messages.stream(
        model      = "claude-opus-4-6",
        max_tokens = 4096,
        thinking   = {"type": "adaptive"},
        system     = system,
        messages   = [{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            yield text


def answer_structured(question: str, selected: list[dict], client,
                      max_paragraphs: int | None = None) -> tuple[dict, str]:
    """
    Non-streaming structured answer (JSON mode).
    Returns (parsed_dict, raw_json_string).
    Falls back to {"summary": raw_text, "references": []} on parse failure.
    """
    import re as _re
    context = _build_context(selected)
    prompt  = (
        f"Use the EUR-Lex articles below to answer the question.\n\n"
        f"QUESTION: {question}\n\n"
        f"ARTICLES:\n{context}"
    )
    system = _ANSWER_SYSTEM_STRUCTURED
    if max_paragraphs is not None:
        system = system + (
            f"\n\nANSWER LENGTH LIMIT: The 'summary' field must be at most "
            f"{max_paragraphs} sentence(s). Each list (obligations, rights, "
            f"exceptions, procedure) must have at most {max_paragraphs} item(s) total. "
            f"Prioritise the most critical legal information."
        )
    resp = client.messages.create(
        model      = "claude-opus-4-6",
        max_tokens = 4096,
        thinking   = {"type": "adaptive"},
        system     = system,
        messages   = [{"role": "user", "content": prompt}],
    )
    raw = resp.content[-1].text.strip()
    raw = _re.sub(r"^```(?:json)?\s*", "", raw)
    raw = _re.sub(r"\s*```$",          "", raw)
    try:
        return json.loads(raw), raw
    except Exception:
        return {"summary": raw, "references": []}, raw


def answer_prose(question: str, selected: list[dict], client,
                 max_paragraphs: int | None = None) -> str:
    """Non-streaming prose answer (used for silent retry attempts)."""
    context = _build_context(selected)
    prompt  = (
        f"Use the EUR-Lex articles below to answer the question.\n\n"
        f"QUESTION: {question}\n\n"
        f"ARTICLES:\n{context}"
    )
    system = _ANSWER_SYSTEM
    if max_paragraphs is not None:
        system = system + _paragraph_limit_rule(max_paragraphs)
    resp = client.messages.create(
        model      = "claude-opus-4-6",
        max_tokens = 4096,
        thinking   = {"type": "adaptive"},
        system     = system,
        messages   = [{"role": "user", "content": prompt}],
    )
    return resp.content[-1].text.strip()


def _render_structured_answer(data: dict) -> None:
    """Render a structured JSON answer as Streamlit components."""
    # Summary
    if data.get("summary"):
        st.info(data["summary"])

    # Obligations / Rights / Exceptions / Procedure
    _SECTION_ICONS = {
        "obligations": ("📋 Obligations",  "orange"),
        "rights":      ("✅ Rights",        "green"),
        "exceptions":  ("⚠️ Exceptions",   "red"),
        "procedure":   ("🔢 Procedure",    "blue"),
    }
    for key, (label, _) in _SECTION_ICONS.items():
        items = data.get(key, [])
        if items:
            st.markdown(f"**{label}**")
            for item in items:
                st.markdown(f"- {item}")

    # Definitions
    defs = data.get("definitions", {})
    if defs:
        st.markdown("**📖 Definitions**")
        for term, definition in defs.items():
            st.markdown(f"- **{term}**: {definition}")

    # References
    refs = data.get("references", [])
    if refs:
        st.markdown("**📎 References**")
        for r in refs:
            with st.expander(
                f"{r.get('celex','?')} — {r.get('article','?')}",
                expanded=False,
            ):
                if r.get("quote"):
                    st.markdown(f"> *\"{r['quote']}\"*")
                if r.get("relevance"):
                    st.caption(r["relevance"])

    # Caveats
    if data.get("caveats"):
        st.caption(f"⚠️ {data['caveats']}")


# ── Keyword article hit statistics ────────────────────────────────────────────

def keyword_article_stats(
    keywords: list[str],
    trees: list[dict],
) -> list[dict]:
    """
    For each keyword count how many unique article nodes and how many documents
    contain it (case-insensitive substring match across text + title + summary).

    Returns rows sorted by article count descending:
        [{"Keyword": "...", "Articles": N, "Docs": M, "Coverage %": X.X}, ...]
    """
    total = sum(len(t.get("nodes", [])) for t in trees)
    rows = []
    for kw in keywords:
        kw_l = kw.lower()
        art_hits = 0
        doc_hits = 0
        for tree in trees:
            doc_matched = False
            for node in tree.get("nodes", []):
                haystack = " ".join([
                    node.get("text", ""),
                    node.get("title", ""),
                    node.get("summary", ""),
                ]).lower()
                if kw_l in haystack:
                    art_hits += 1
                    doc_matched = True
            if doc_matched:
                doc_hits += 1
        rows.append({
            "Keyword":    kw,
            "Articles":   art_hits,
            "Docs":       doc_hits,
            "Coverage %": round(art_hits / total * 100, 1) if total else 0.0,
        })
    return sorted(rows, key=lambda r: r["Articles"], reverse=True)


# ── Within-document article expansion ─────────────────────────────────────────

_SCOPE_QUESTION_TOKENS = {
    "scope", "categor", "types of", "which vehicle", "what vehicle",
    "what falls under", "what does this cover", "what entities",
    "what applies", "covered by", "in scope", "subject to",
}

def _is_scope_question(question: str) -> bool:
    q = question.lower()
    return any(tok in q for tok in _SCOPE_QUESTION_TOKENS)


def _expand_within_document(
    raw_selected: list[dict],
    trees: list[dict],
    max_extra: int = 4,
) -> list[dict]:
    """
    When navigation returns only generic/low-signal articles, also include
    the next few articles from the same documents so the final answer LLM
    has a wider view without triggering a full SPARQL retry.

    Specifically useful when:
    - Article 1 (Subject-matter) is selected but the answer is in Article 2 (Scope)
    - The navigator undershoots due to conservative scoring rules
    """
    if not raw_selected:
        return raw_selected

    tree_index = {t["doc_id"]: t for t in trees}
    already: set[tuple] = {(n["doc_id"], n["node_id"]) for n in raw_selected}
    extras: list[dict] = []

    for node in raw_selected:
        doc = tree_index.get(node["doc_id"])
        if not doc:
            continue
        nodes = doc["nodes"]
        idx = next((i for i, n in enumerate(nodes) if n["id"] == node["node_id"]), None)
        if idx is None:
            continue
        # Include up to max_extra adjacent articles (next ones in the document)
        for j in range(idx + 1, min(idx + max_extra + 1, len(nodes))):
            neighbor = nodes[j]
            key = (node["doc_id"], neighbor["id"])
            if key in already:
                continue
            already.add(key)
            extras.append({
                "doc_id":          node["doc_id"],
                "node_id":         neighbor["id"],
                "title":           neighbor.get("title", ""),
                "summary":         neighbor.get("summary", ""),
                "doc_title":       doc.get("title", ""),
                "year":            doc.get("year", ""),
                "doc_url":         doc.get("url", ""),
                "text":            neighbor.get("text", ""),
                "relevance_score": 6,   # lower confidence than navigator-selected
            })
            if len(extras) >= max_extra:
                break

    return raw_selected + extras


# ── Keyword discovery UI ──────────────────────────────────────────────────────

def _render_keyword_discovery(
    question: str,
    confidence: dict,
    kw_stats: list[dict],
) -> None:
    """
    Render keyword discovery UI when confidence is low after all retries.
    Shows missing aspects, keyword hit stats, suggested terms, and a re-run button.
    """
    st.divider()
    st.markdown(
        f"**⚠️ Low confidence after 3 search attempts** "
        f"(best score: {confidence['score']}%)"
    )

    missing = confidence.get("missing_aspects", [])
    if missing:
        st.markdown("**Missing aspects:**")
        for m in missing:
            st.markdown(f"- {m}")

    if kw_stats:
        st.markdown("**📊 Keyword coverage in retrieved articles:**")
        _cols = ["Keyword", "Articles", "Docs", "Coverage %"]
        _html = "<table style='width:100%;font-size:0.85em;border-collapse:collapse'>"
        _html += "<tr>" + "".join(
            f"<th style='text-align:left;padding:4px 8px;border-bottom:1px solid #ddd'>{c}</th>"
            for c in _cols
        ) + "</tr>"
        for _r in kw_stats:
            _html += "<tr>" + "".join(
                f"<td style='padding:3px 8px;border-bottom:1px solid #eee'>{_r.get(c,'')}</td>"
                for c in _cols
            ) + "</tr>"
        _html += "</table>"
        st.markdown(_html, unsafe_allow_html=True)

    suggested = confidence.get("suggested_keywords", [])
    _disc_sel: list[str] = []
    if suggested:
        st.markdown("**🔍 Suggested search terms from the AI:**")
        _disc_sel = st.multiselect(
            "Select terms to add:",
            options=suggested,
            default=[],
            key="disc_suggested_kws",
        )

    _disc_custom = st.text_input(
        "Add your own keywords (comma-separated):",
        key="disc_custom_kws",
        placeholder="e.g. cybersecurity, type-approval, motorcycle",
    )

    _extra_kws = list(_disc_sel)
    if _disc_custom:
        _extra_kws += [k.strip() for k in _disc_custom.split(",") if k.strip()]

    if st.button(
        "🔍 Re-run with these keywords",
        key="disc_rerun_btn",
        disabled=not _extra_kws,
    ):
        st.session_state.focus_rerun_q   = question
        st.session_state.focus_rerun_kws = _extra_kws
        st.rerun()

    st.caption(
        "The answer below is the best result found. "
        "Add more specific terms above and re-run to improve coverage."
    )
    st.divider()


# ── Pipeline runner ────────────────────────────────────────────────────────────
def run_query(
    question: str,
    sidebar_year: int | None,
    delta: int,
    max_laws: int,
    client,
    sectors: tuple[str, ...] = ("3",),
    upload_trees: list | None = None,
    source_mode: str = "EUR-Lex only",
    prev_selected: list | None = None,    # conversation memory
    sub_questions: list[str] | None = None,  # for complex question decomposition
    focus_keywords: list[str] | None = None,  # user-selected keyword emphasis
) -> dict:
    """
    Full pipeline. source_mode controls what is searched:
      "EUR-Lex only"     — live EUR-Lex SPARQL search, no uploads used
      "EUR-Lex + uploads"— EUR-Lex search merged with uploaded document trees
      "Uploads only"     — answer exclusively from uploaded documents; no network call
    """
    upload_trees    = upload_trees or []
    uploads_only    = source_mode == "Uploads only"
    include_uploads = source_mode in ("EUR-Lex + uploads", "Uploads only")
    prev_selected   = prev_selected or []

    # ── Step 1: keyword extraction (always needed for tree navigation) ─────────
    meta            = extract_keywords_and_year(question, client)
    keywords        = meta["keywords"]
    kw_year         = meta.get("year")
    exact_signals   = meta.get("exact_signals", [])
    query_variants  = meta.get("query_variants", [keywords])

    all_keywords      = keywords
    citation_keywords = []
    docs: list[dict]  = []
    trees: list[dict] = []

    # ── Steps 2-3: EUR-Lex search + tree building (skipped in uploads-only) ───
    if not uploads_only:
        # Citation extraction (regex) — e.g. "2018/858", "715/2007"
        citations         = extract_cited_regulations(question)
        citation_keywords = [c["keyword"] for c in citations]
        citation_years    = list({c["year"] for c in citations})
        all_keywords      = citation_keywords + [k for k in keywords if k not in citation_keywords]

        # Direct CELEX lookup for explicit citations
        seen_celex: set[str] = set()
        for c in citations:
            doc = fetch_by_citation(c, sectors=sectors)
            if doc and doc["celex"] not in seen_celex:
                seen_celex.add(doc["celex"])
                docs.append(doc)

        # Build list of specific years to search
        years_to_search: list[int] = []
        if sidebar_year is not None:
            years_to_search.append(sidebar_year)
        if kw_year and kw_year not in years_to_search:
            years_to_search.append(kw_year)
        for cy in citation_years:
            if cy not in years_to_search:
                years_to_search.append(cy)

        # Year-specific keyword searches (with ± delta)
        remaining = max_laws - len(docs)
        if remaining > 0 and years_to_search:
            per_year = max(2, remaining // len(years_to_search))
            for search_year in years_to_search:
                found = search_years(all_keywords, search_year, delta,
                                     per_year, sectors=sectors)
                for d in found:
                    if d["celex"] not in seen_celex:
                        seen_celex.add(d["celex"])
                        docs.append(d)

        # Year-agnostic search — catches laws registered in a different year
        remaining = max_laws - len(docs)
        if remaining > 0:
            agnostic_limit = min(remaining, max(2, max_laws // 2))
            try:
                agnostic_docs = search_eurlex(
                    all_keywords, year=None,
                    max_results=agnostic_limit, sectors=sectors,
                )
                for d in agnostic_docs:
                    if d["celex"] not in seen_celex:
                        seen_celex.add(d["celex"])
                        docs.append(d)
            except Exception as exc:
                st.warning(f"Year-agnostic search error: {exc}")

        # Multi-query variant passes — run synonym/obligation variants if still
        # short on documents (#4 multi-query expansion, #10 semantic synonyms)
        remaining = max_laws - len(docs)
        if remaining > 0 and len(query_variants) > 1:
            for variant_kws in query_variants[1:]:   # skip variant 0 (already done)
                if remaining <= 0:
                    break
                try:
                    variant_docs = search_eurlex(
                        variant_kws, year=None,
                        max_results=min(remaining, 3), sectors=sectors,
                    )
                    for d in variant_docs:
                        if d["celex"] not in seen_celex:
                            seen_celex.add(d["celex"])
                            docs.append(d)
                            remaining -= 1
                except Exception:
                    pass

        docs = docs[:max_laws]

        if not docs and not include_uploads:
            if years_to_search:
                yr_str = ", ".join(str(y) for y in years_to_search) + " + year-agnostic"
            else:
                yr_str = "year-agnostic only"
            _zero_kws = list(dict.fromkeys((exact_signals or []) + all_keywords))
            return {
                "answer":    f"No EUR-Lex documents found ({yr_str}) "
                             f"with keywords: {all_keywords}.",
                "references": [],
                "keywords":  all_keywords,
                "docs_found": 0,
                "confidence": None,
                "kw_stats":  [{"Keyword": k, "Articles": 0, "Docs": 0, "Coverage %": 0.0}
                               for k in _zero_kws],
            }

        # Build EUR-Lex reasoning trees (parallel fetch)
        if docs:
            prog = st.progress(0, text="Fetching documents in parallel…")
            fetched_pairs = fetch_documents_parallel(docs, max_workers=3)
            for i, (doc, md) in enumerate(fetched_pairs):
                prog.progress((i + 1) / len(fetched_pairs),
                              text=f"Building tree: {doc['celex']}…")
                if not md:
                    continue
                _yr = int(doc["date"][:4]) if doc.get("date", "")[:4].isdigit() else 0
                tree = build_tree(
                    markdown=md, doc_id=doc["celex"], title=doc["title"],
                    year=_yr, date=doc.get("date", ""), url=doc.get("url", ""),
                    cache_dir=CACHE_DIR, client=client,
                )
                trees.append(tree)
            prog.empty()

    # ── Merge uploaded trees ───────────────────────────────────────────────────
    if include_uploads:
        trees.extend(upload_trees)

    if not trees:
        _zero_kws = list(dict.fromkeys((exact_signals or []) + all_keywords))
        _zero_stats = [{"Keyword": k, "Articles": 0, "Docs": 0, "Coverage %": 0.0}
                       for k in _zero_kws]
        if uploads_only:
            return {
                "answer":    "No uploaded documents found. Please upload at least one file.",
                "references": [],
                "keywords":  all_keywords,
                "docs_found": 0,
                "confidence": None,
                "kw_stats":  _zero_stats,
            }
        return {
            "answer":    "Could not retrieve document content from EUR-Lex.",
            "references": [],
            "keywords":  all_keywords,
            "docs_found": len(docs),
            "confidence": None,
            "kw_stats":  _zero_stats,
        }

    # ── Step 4: navigate + rerank + iterative retry ───────────────────────────
    _MAX_RETRIES = 1          # one deep-retry after initial generic result
    selected:  list[dict] = []
    retry_kws: list[str]  = list(all_keywords)

    for _attempt in range(_MAX_RETRIES + 1):
        # ── 4a. Navigate (LLM tree walk) ──────────────────────────────────────
        if sub_questions and len(sub_questions) > 1:
            raw_selected: list[dict] = []
            seen_nids: set[tuple]    = set()
            for sq in sub_questions:
                for node in navigate_tree(sq, trees, client,
                                          context_nodes=prev_selected,
                                          exact_signals=exact_signals,
                                          focus_keywords=focus_keywords):
                    key = (node["doc_id"], node["node_id"])
                    if key not in seen_nids:
                        seen_nids.add(key)
                        raw_selected.append(node)
            raw_selected = raw_selected[:12]
        else:
            raw_selected = navigate_tree(question, trees, client,
                                         context_nodes=prev_selected,
                                         exact_signals=exact_signals,
                                         focus_keywords=focus_keywords)

        # ── 4b. Citation link following ────────────────────────────────────────
        if raw_selected and not uploads_only:
            seen_celex_set = {t["doc_id"] for t in trees}
            linked = follow_citation_links(raw_selected, seen_celex_set, sectors=sectors)
            if linked:
                linked_pairs  = fetch_documents_parallel(linked, max_workers=2)
                new_trees_cnt = 0
                for doc, md in linked_pairs:
                    if not md:
                        continue
                    _yr = int(doc["date"][:4]) if doc.get("date", "")[:4].isdigit() else 0
                    trees.append(build_tree(
                        markdown=md, doc_id=doc["celex"], title=doc["title"],
                        year=_yr, date=doc.get("date", ""), url=doc.get("url", ""),
                        cache_dir=CACHE_DIR, client=client,
                    ))
                    new_trees_cnt += 1
                # Re-navigate with expanded tree set if new documents were added
                if new_trees_cnt > 0:
                    if sub_questions and len(sub_questions) > 1:
                        raw_selected = []
                        seen_nids    = set()
                        for sq in sub_questions:
                            for node in navigate_tree(sq, trees, client,
                                                      context_nodes=prev_selected,
                                                      exact_signals=exact_signals):
                                key = (node["doc_id"], node["node_id"])
                                if key not in seen_nids:
                                    seen_nids.add(key)
                                    raw_selected.append(node)
                        raw_selected = raw_selected[:12]
                    else:
                        raw_selected = navigate_tree(question, trees, client,
                                                     context_nodes=prev_selected,
                                                     exact_signals=exact_signals)

        # ── 4c. Python-side reranking (obligation + list scores, generic penalty)
        selected = rerank_nodes(raw_selected)

        # ── 4d. Within-document expansion ────────────────────────────────────
        # When the navigator only returned generic articles (e.g. Art. 1 Subject-
        # matter) OR when the question is explicitly about scope/categories, pull
        # in the next few articles from the same documents so the answer LLM can
        # find Art. 2 (Scope) / Art. 3 (Definitions) without a full SPARQL retry.
        if not has_answer_signal(selected) or _is_scope_question(question):
            expanded = _expand_within_document(raw_selected, trees, max_extra=4)
            if len(expanded) > len(raw_selected):
                selected = rerank_nodes(expanded)

        # ── 4e. Answer validation gate ────────────────────────────────────────
        # If all selected nodes are generic (no obligation/list signals) AND we
        # have retries left AND we are not in uploads-only mode → retry with
        # obligation-expanded keywords (Point #7).
        if has_answer_signal(selected) or uploads_only or _attempt >= _MAX_RETRIES:
            break

        # Build an obligation-expanded keyword set for the retry pass
        retry_kws = obligation_expansion_keywords(question, all_keywords)
        if not uploads_only:
            retry_docs: list[dict] = []
            retry_seen: set[str]   = {t["doc_id"] for t in trees}
            try:
                for rd in search_eurlex(retry_kws, year=None,
                                        max_results=3, sectors=sectors):
                    if rd["celex"] not in retry_seen:
                        retry_seen.add(rd["celex"])
                        retry_docs.append(rd)
            except Exception:
                pass
            if retry_docs:
                retry_pairs = fetch_documents_parallel(retry_docs, max_workers=2)
                for rdoc, rmd in retry_pairs:
                    if rmd:
                        _yr = int(rdoc["date"][:4]) if rdoc.get("date", "")[:4].isdigit() else 0
                        trees.append(build_tree(
                            markdown=rmd, doc_id=rdoc["celex"], title=rdoc["title"],
                            year=_yr, date=rdoc.get("date", ""), url=rdoc.get("url", ""),
                            cache_dir=CACHE_DIR, client=client,
                        ))

    # ── Keyword hit statistics (computed once after all trees are final) ─────────
    _stat_kws  = list(dict.fromkeys((exact_signals or []) + all_keywords))
    _kw_stats  = keyword_article_stats(_stat_kws, trees) if trees else []
    _docs_total = len(docs) + len(upload_trees)

    # ── Point #9: Hard failure rule ───────────────────────────────────────────
    if not selected:
        return {
            "answer":    "The documents do not appear to contain information "
                         "relevant to your question.",
            "references": [],
            "keywords":   all_keywords,
            "docs_found": _docs_total,
            "confidence": None,
            "kw_stats":   _kw_stats,
        }

    # Hard failure only when Python signals AND LLM navigator both indicate
    # no relevant content was found.  has_answer_signal() already trusts
    # navigator scores ≥ 7 as a fallback, so this only fires when everything
    # genuinely comes up empty.
    if not has_answer_signal(selected):
        # Soft-fail: still show references so the user knows what was retrieved
        return {
            "answer": (
                "The retrieved articles address this regulation area but none "
                "contains a specific clause directly answering the question "
                f"(checked after {_MAX_RETRIES + 1} retrieval attempt(s)). "
                "Try rephrasing the question with the specific article number or "
                "obligation keyword (e.g. 'Article 5 2019/2144 shall be equipped')."
            ),
            "references": [
                {
                    "CELEX":   s.get("doc_id", "?"),
                    "Article": f"Article {s.get('title', s.get('node_id', '?'))}",
                    "Summary": s.get("summary", "")[:120],
                    "URL":     s.get("doc_url", ""),
                    "Year":    str(s.get("year", "?")) if s.get("year") else "uploaded",
                }
                for s in selected
            ],
            "keywords":   all_keywords,
            "docs_found": _docs_total,
            "confidence": None,
            "kw_stats":   _kw_stats,
        }

    fallback_year = sidebar_year or kw_year or "?"
    return {
        "answer":     None,          # streamed separately
        "selected":   selected,
        "references": [
            {
                "CELEX":   s.get("doc_id", "?"),
                "Article": f"Article {s.get('title', s.get('node_id', '?'))}",
                "Summary": s.get("summary", "")[:120],
                "URL":     s.get("doc_url", ""),
                "Year":    str(s.get("year", fallback_year))
                           if s.get("year") else "uploaded",
            }
            for s in selected
        ],
        "keywords":          all_keywords,
        "citation_keywords": citation_keywords,
        "docs_found":        _docs_total,
        "confidence":        None,   # computed after streaming
        "kw_stats":          _kw_stats,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  SIDEBAR
# ═══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.title("⚖️ LexTreeRAG")
    st.caption("Vectorless Legal RAG via Article Trees · Live EUR-Lex")
    st.divider()

    # API key
    api_key = st.text_input(
        "Anthropic API Key",
        value=os.getenv("ANTHROPIC_API_KEY", ""),
        type="password",
        placeholder="sk-ant-…",
        help="Set in .env to avoid entering it every time.",
    )
    if api_key and not st.session_state.api_key_ok:
        with st.spinner("Validating key…"):
            c = _get_client(api_key)
        if c:
            st.session_state.client     = c
            st.session_state.api_key_ok = True
            st.success("API key valid ✓", icon="✅")
        else:
            st.error("Invalid API key")
    elif st.session_state.api_key_ok:
        st.success("API key valid ✓", icon="✅")

    st.divider()

    # ── Optional Gemini key ───────────────────────────────────────────────────
    st.markdown("**🔮 Gemini (optional — hybrid mode)**")
    gemini_key = st.text_input(
        "Google Gemini API Key",
        value=os.getenv("GEMINI_API_KEY", ""),
        type="password",
        placeholder="AIza…  (free tier at aistudio.google.com)",
        help="Optional. When set, a second answer using Gemini + Google Search "
             "appears alongside the EUR-Lex answer.",
    )
    if gemini_key and not st.session_state.gemini_key_ok:
        with st.spinner("Validating Gemini key…"):
            ok, model_or_err = validate_gemini_key(gemini_key)
        if ok:
            st.session_state.gemini_client = get_gemini_client(gemini_key)
            st.session_state.gemini_key_ok = True
            st.session_state.gemini_model  = model_or_err
            st.success(f"Hybrid mode ON ✓ · `{model_or_err}`", icon="🔮")
        else:
            st.error(f"Gemini key error: {model_or_err}")
    elif st.session_state.gemini_key_ok:
        st.success(f"Hybrid mode ON ✓ · `{st.session_state.gemini_model}`", icon="🔮")
    elif not gemini_key:
        st.caption("_Classic mode — EUR-Lex only_")

    st.divider()

    # ── Optional year filter ──────────────────────────────────────────────────
    st.markdown("**📅 Year filter**")
    year_enabled = st.checkbox(
        "Specify target year",
        value=False,
        help="Leave unchecked to auto-detect the year from your question. "
             "A year-agnostic search is always included to catch laws registered "
             "in a different year than the one mentioned.",
    )
    sidebar_year: int | None = None
    delta = 0
    if year_enabled:
        sidebar_year = int(st.number_input(
            "Target Year",
            min_value=1950, max_value=2026,
            value=datetime.now().year, step=1,
            help="Primary year for the EUR-Lex SPARQL search.",
        ))
        delta = st.slider(
            "± Years range",
            min_value=0, max_value=5, value=0,
            help="Also search N years before and after the target year.",
        )
        if delta:
            st.caption(f"Searching {sidebar_year-delta} – {sidebar_year+delta}")
    else:
        st.caption("Year auto-detected from question · always includes a year-agnostic pass")

    max_laws = st.slider(
        "📄 Max laws to retrieve",
        min_value=1, max_value=15, value=5,
        help="Total documents fetched across all years in the range.",
    )

    st.divider()

    # Sector selection
    st.markdown("**📂 CELEX Sectors to search**")
    selected_sectors = []
    for code, label in SECTORS.items():
        # Default to Legislation (3) + Consolidated acts (0) for best coverage
        default = code in ("3", "0")
        if st.checkbox(label, value=default, key=f"sector_{code}"):
            selected_sectors.append(code)

    if not selected_sectors:
        st.warning("Select at least one sector.")
        selected_sectors = ["3"]   # safety fallback

    st.divider()

    # ── Output mode ───────────────────────────────────────────────────────────
    st.markdown("**🗂️ Output format**")
    structured_mode = st.toggle(
        "Structured JSON mode",
        value=st.session_state.structured_mode,
        help=(
            "**Off** — streaming prose answer with a References section (default).\n\n"
            "**On** — non-streaming structured output: Summary, Obligations, Rights, "
            "Definitions, Exceptions, Procedure, and cited References with quotes. "
            "Useful for programmatic use or detailed legal analysis."
        ),
    )
    st.session_state.structured_mode = structured_mode
    if structured_mode:
        st.caption("Structured mode: answer rendered as categorised sections.")

    # ── Answer length limit ───────────────────────────────────────────────────
    st.markdown("**📏 Answer length**")
    limit_answer_length = st.checkbox(
        "Limit answer to N paragraphs",
        value=False,
        help=(
            "Restrict the final answer to a set number of paragraphs. "
            "Useful when you need a brief summary rather than a full legal analysis.\n\n"
            "In **structured mode** this limits the number of sentences in the summary "
            "and items per list section."
        ),
    )
    max_paragraphs: int | None = None
    if limit_answer_length:
        max_paragraphs = st.slider(
            "Max paragraphs",
            min_value=1, max_value=15, value=3,
            help=(
                "1 = single-paragraph direct answer · "
                "3 = short summary · "
                "10+ = detailed but still bounded response"
            ),
        )
        st.caption(
            f"Answer capped at **{max_paragraphs}** paragraph(s) "
            + ("(sentences/items in structured mode)." if structured_mode else ".")
        )

    st.divider()

    # ── Document upload ───────────────────────────────────────────────────────
    st.markdown("**📎 Upload documents (optional)**")
    source_mode = st.radio(
        "Answer source",
        options=["EUR-Lex only", "EUR-Lex + uploads", "Uploads only"],
        index=0,
        help=(
            "**EUR-Lex only** — live EUR-Lex search, no uploaded files used.\n\n"
            "**EUR-Lex + uploads** — merges EUR-Lex results with your uploaded files.\n\n"
            "**Uploads only** — answers come exclusively from your uploaded files; "
            "no network call to EUR-Lex is made."
        ),
    )

    if source_mode != "EUR-Lex only":
        uploaded_files = st.file_uploader(
            "Add PDF or text files",
            type=["pdf", "txt", "md"],
            accept_multiple_files=True,
            help="Each file is parsed into a reasoning tree "
                 "(same vectorless format as EUR-Lex documents).",
        )
        if uploaded_files and st.session_state.client:
            already_loaded = {t["title"] for t in st.session_state.upload_trees}
            new_files      = [f for f in uploaded_files if f.name not in already_loaded]
            if new_files:
                with st.spinner(f"Ingesting {len(new_files)} file(s)…"):
                    for uf in new_files:
                        tree = ingest_uploaded_file(
                            uf.name, uf.read(),
                            st.session_state.client, CACHE_DIR,
                        )
                        if tree:
                            st.session_state.upload_trees.append(tree)
                            n = len(tree["nodes"])
                            st.success(f"✓ {uf.name}  ({n} sections)", icon="📄")
                        else:
                            st.warning(f"Could not parse '{uf.name}'")

        if st.session_state.upload_trees:
            names = [t["title"] for t in st.session_state.upload_trees]
            st.caption(f"{len(names)} file(s) loaded: {', '.join(names)}")
            if st.button("🗑️ Clear uploads", use_container_width=True, key="clear_uploads"):
                for t in st.session_state.upload_trees:
                    delete_upload_tree(CACHE_DIR, t["title"])
                st.session_state.upload_trees = []
                st.rerun()
        elif source_mode == "Uploads only":
            st.warning("Upload at least one file to use this mode.")
    else:
        # Keep upload_trees in memory but don't use them
        if st.session_state.upload_trees:
            st.caption(
                f"ℹ️ {len(st.session_state.upload_trees)} file(s) loaded but not used in this mode."
            )

    st.divider()
    if st.button("🗑️ Clear conversation", use_container_width=True):
        st.session_state.history    = []
        st.session_state.references = []
        st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN TABS
# ═══════════════════════════════════════════════════════════════════════════════
tab_ask, tab_refs, tab_cache = st.tabs(
    ["💬 Ask EUR-Lex", "📋 References", "🗄️ Cache Manager"]
)

# ── Tab 1: Chat ────────────────────────────────────────────────────────────────
with tab_ask:
    st.markdown("### Ask a legal question about EU law")
    st.caption(
        "Include a year in your question **or** set it in the sidebar. "
        "The answer is grounded in live EUR-Lex documents."
    )

    # Render conversation history
    for msg in st.session_state.history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg["role"] == "assistant" and msg.get("meta"):
                m = msg["meta"]
                cite_kws = m.get("citation_keywords", [])
                topic_kws = [k for k in m.get("keywords", []) if k not in cite_kws]
                parts = [f"Docs found: **{m.get('docs_found', 0)}**"]
                if cite_kws:
                    parts.append(f"Citations: `{'`, `'.join(cite_kws)}`")
                if topic_kws:
                    parts.append(f"Keywords: `{'`, `'.join(topic_kws)}`")
                q_type = st.session_state.last_query_type.get("type", "")
                if q_type:
                    parts.append(f"Type: `{q_type}`")
                st.caption(" · ".join(parts))

    # ── Keyword stats panel (persistent — updates after each query) ──────────
    if st.session_state.kw_stats and not st.session_state.get("focus_rerun_q"):
        import pandas as _pd
        _stats = st.session_state.kw_stats
        with st.expander(
            f"📊 Keyword Analysis  ·  {len(_stats)} keywords tracked",
            expanded=True,
        ):
            # Plain HTML table — avoids numpy/pandas incompatibility on Python 3.9
            _kw_cols = ["Keyword", "Articles", "Docs", "Coverage %"]
            _kw_html = "<table style='width:100%;font-size:0.85em;border-collapse:collapse'>"
            _kw_html += "<tr>" + "".join(
                f"<th style='text-align:left;padding:4px 8px;border-bottom:1px solid #ddd'>{c}</th>"
                for c in _kw_cols
            ) + "</tr>"
            for _r in _stats:
                _kw_html += "<tr>" + "".join(
                    f"<td style='padding:3px 8px;border-bottom:1px solid #eee'>{_r.get(c,'')}</td>"
                    for c in _kw_cols
                ) + "</tr>"
            _kw_html += "</table>"
            st.markdown(_kw_html, unsafe_allow_html=True)
            st.caption(
                f"Articles = number of article nodes across all "
                f"**{st.session_state.kw_docs_found}** retrieved document(s) "
                "that contain the keyword in their text, title, or summary."
            )
            st.divider()
            st.markdown("**🎯 Focus the model on specific keywords**")
            st.caption(
                "Pick one or more keywords below and click **Re-run with focus** — "
                "the navigator re-scores the same cached documents giving extra weight "
                "to articles that mention your selected keywords."
            )
            _kw_options = [r["Keyword"] for r in _stats]
            _focus_sel = st.multiselect(
                "Keywords to emphasise:",
                options=_kw_options,
                default=[],
                key="focus_kw_select",
                placeholder="Select keywords…",
            )
            _btn_col, _info_col = st.columns([2, 5])
            _run_disabled = not _focus_sel or not st.session_state.last_question
            if _btn_col.button(
                "🎯 Re-run with focus",
                disabled=_run_disabled,
                use_container_width=True,
                help="Re-navigates cached documents with selected keywords prioritised.",
            ):
                st.session_state.focus_rerun_q   = st.session_state.last_question
                st.session_state.focus_rerun_kws = _focus_sel
                st.rerun()
            if _focus_sel:
                _info_col.info(
                    f"Will re-run: *{st.session_state.last_question[:70]}…*  "
                    f"→ focus: {', '.join(f'`{k}`' for k in _focus_sel)}"
                )

    # ── Persistent Gemini panel (shown when a previous answer exists) ─────────
    if st.session_state.gemini_key_ok and st.session_state.gemini_last_q:
        st.divider()
        hdr, btn_col = st.columns([5, 1])
        hdr.markdown(
            f"**🔮 Gemini — Google Search** "
            f"<small style='color:grey'>· `{st.session_state.gemini_model}` · "
            f"last query: _{st.session_state.gemini_last_q[:60]}…_</small>",
            unsafe_allow_html=True,
        )
        if btn_col.button("🔄 Refresh", key="gemini_refresh_btn", use_container_width=True):
            st.session_state.gemini_refresh = True
            st.rerun()

        if st.session_state.gemini_refresh:
            st.session_state.gemini_refresh = False
            g_placeholder = st.empty()
            g_placeholder.info("🔍 Re-searching with Google…")
            refreshed_text = ""
            for chunk in gemini_answer_stream(
                st.session_state.gemini_last_q,
                st.session_state.gemini_client,
                model=st.session_state.gemini_model,
            ):
                refreshed_text += chunk
                g_placeholder.markdown(refreshed_text + "▌")
            g_placeholder.markdown(refreshed_text)
            st.session_state.gemini_last_ans = refreshed_text
        else:
            st.markdown(st.session_state.gemini_last_ans)

        st.divider()

    # Per-question Gemini toggle (only shown when key is set)
    use_gemini = False
    if st.session_state.gemini_key_ok:
        use_gemini = st.checkbox(
            f"🔮 Include Gemini answer (`{st.session_state.gemini_model}`)",
            value=True,
            help="Uncheck to skip the Gemini / Google Search panel for this question "
                 "and save your free-tier quota.",
        )

    # Chat input
    user_q = st.chat_input(
        "e.g. What are the mandatory targets for EV charging infrastructure in 2023?"
    )

    if user_q:
        if not st.session_state.client:
            st.error("Please enter a valid Anthropic API key in the sidebar.")
            st.stop()

        # Show user message
        st.session_state.history.append({"role": "user", "content": user_q})
        with st.chat_message("user"):
            st.markdown(user_q)

        # Run pipeline
        with st.chat_message("assistant"):
            status_box = st.empty()

            # Classify the question before searching
            with st.spinner("Classifying question…"):
                q_class = classify_question(user_q, st.session_state.client)
            st.session_state.last_query_type = q_class
            sub_questions = (
                q_class.get("sub_questions", [])
                if q_class["type"] == "complex"
                else []
            )

            status_box.info("🔍 Extracting keywords and searching EUR-Lex…")

            result = run_query(
                question      = user_q,
                sidebar_year  = sidebar_year,
                delta         = int(delta),
                max_laws      = int(max_laws),
                client        = st.session_state.client,
                sectors       = tuple(selected_sectors),
                upload_trees  = st.session_state.upload_trees,
                source_mode   = source_mode,
                prev_selected = st.session_state.prev_selected_nodes,
                sub_questions = sub_questions,
            )

            if result["answer"] is not None:
                # Error / no-result path
                status_box.empty()
                st.warning(result["answer"])
                st.session_state.history.append(
                    {"role": "assistant", "content": result["answer"],
                     "meta": result}
                )
                # Persist keyword stats so the panel appears after rerun.
                # The panel is rendered BEFORE this block executes, so we must
                # call st.rerun() to get a second pass where the panel is visible.
                if result.get("kw_stats"):
                    st.session_state.kw_stats      = result["kw_stats"]
                    st.session_state.kw_docs_found = result.get("docs_found", 0)
                    st.session_state.last_question = user_q
                    st.rerun()   # forces the stats panel to render
            else:
                hybrid = (
                    st.session_state.gemini_key_ok
                    and st.session_state.gemini_client is not None
                    and use_gemini
                )

                if hybrid:
                    col_claude, col_gemini = st.columns(2, gap="medium")
                else:
                    col_claude = st.container()

                # ── Claude answer (EUR-Lex CELLAR) ────────────────────────
                with col_claude:
                    st.markdown(
                        "**⚖️ Claude — EUR-Lex CELLAR**"
                        if hybrid else ""
                    )
                    full_answer = ""
                    claude_placeholder = st.empty()

                    if st.session_state.structured_mode:
                        # ── Structured JSON mode (non-streaming) ──────────
                        status_box.info("🗂️ Generating structured answer…")
                        with st.spinner("Analysing articles…"):
                            structured_data, full_answer = answer_structured(
                                user_q, result["selected"], st.session_state.client,
                                max_paragraphs=max_paragraphs,
                            )
                        status_box.empty()
                        _render_structured_answer(structured_data)
                    else:
                        # ── Prose streaming mode (Attempt 0) ─────────────
                        status_box.info("✍️ Generating answer (streaming)…")

                        for chunk in answer_stream(
                            user_q, result["selected"], st.session_state.client,
                            max_paragraphs=max_paragraphs,
                        ):
                            full_answer += chunk
                            claude_placeholder.markdown(full_answer + "▌")

                        status_box.empty()
                        claude_placeholder.markdown(full_answer)

                    # ── Confidence scoring (post-stream) ──────────────────
                    with st.spinner("Scoring answer confidence…"):
                        confidence = compute_confidence(
                            user_q, full_answer, result["selected"],
                            st.session_state.client,
                        )

                    # ── Confidence-gated retry loop (prose mode only) ─────
                    _CONF_GATE   = 50
                    _uploads_only = source_mode == "Uploads only"
                    best_answer  = full_answer
                    best_conf    = confidence
                    best_result  = result

                    _low_conf = (best_conf["score"] < _CONF_GATE
                                 or best_conf.get("coverage", 100) < 40)
                    if (not st.session_state.structured_mode
                            and not _uploads_only
                            and _low_conf):

                        _retry_box = st.empty()

                        # Attempt 1 — obligation-expanded keywords
                        _retry_box.info("🔄 Refining search (attempt 2 of 3)…")
                        _retry1_kws = obligation_expansion_keywords(
                            user_q, result.get("keywords", [])
                        )
                        _r1 = run_query(
                            question      = user_q,
                            sidebar_year  = sidebar_year,
                            delta         = int(delta),
                            max_laws      = int(max_laws),
                            client        = st.session_state.client,
                            sectors       = tuple(selected_sectors),
                            upload_trees  = st.session_state.upload_trees,
                            source_mode   = source_mode,
                            prev_selected = st.session_state.prev_selected_nodes,
                            sub_questions = sub_questions,
                            focus_keywords= _retry1_kws,
                        )
                        if _r1.get("selected"):
                            _ans1  = answer_prose(user_q, _r1["selected"],
                                                  st.session_state.client, max_paragraphs)
                            _conf1 = compute_confidence(user_q, _ans1, _r1["selected"],
                                                        st.session_state.client)
                            if _conf1["score"] > best_conf["score"]:
                                best_answer = _ans1
                                best_conf   = _conf1
                                best_result = _r1

                        if best_conf["score"] < _CONF_GATE:
                            # Attempt 2 — broader search (drop year filter, expand sectors)
                            _retry_box.info("🔄 Broadening search (attempt 3 of 3)…")
                            _r2 = run_query(
                                question      = user_q,
                                sidebar_year  = None,
                                delta         = 0,
                                max_laws      = min(int(max_laws) + 3, 15),
                                client        = st.session_state.client,
                                sectors       = ("3", "0", "4"),
                                upload_trees  = st.session_state.upload_trees,
                                source_mode   = source_mode,
                                prev_selected = st.session_state.prev_selected_nodes,
                                sub_questions = sub_questions,
                            )
                            if _r2.get("selected"):
                                _ans2  = answer_prose(user_q, _r2["selected"],
                                                      st.session_state.client, max_paragraphs)
                                _conf2 = compute_confidence(user_q, _ans2, _r2["selected"],
                                                            st.session_state.client)
                                if _conf2["score"] > best_conf["score"]:
                                    best_answer = _ans2
                                    best_conf   = _conf2
                                    best_result = _r2

                        _retry_box.empty()

                        # Replace displayed answer if a retry found something better
                        if best_answer != full_answer:
                            claude_placeholder.markdown(best_answer)
                        full_answer = best_answer
                        confidence  = best_conf
                        result      = best_result

                    # ── Confidence metrics ────────────────────────────────
                    level = confidence["level"]
                    color = {"high": "🟢", "medium": "🟡", "low": "🔴"}[level]
                    score = confidence["score"]

                    conf_col1, conf_col2, conf_col3 = st.columns(3)
                    conf_col1.metric(f"{color} Confidence", f"{score}%")
                    conf_col2.metric(
                        "Coverage", f"{confidence['coverage']}%",
                        help="How well the retrieved articles cover your question",
                    )
                    conf_col3.metric(
                        "Grounding", f"{confidence['grounding']}%",
                        help="How well the answer claims are traceable to article text",
                    )
                    st.caption(f"_{confidence['reasoning']}_")

                    n_refs = len(result["references"])
                    if n_refs:
                        st.caption(
                            f"📎 **{n_refs}** article(s) from "
                            f"**{result['docs_found']}** document(s). "
                            "See **References** tab."
                        )

                    # Keyword discovery UI when still low-confidence after all retries
                    _still_low = (confidence["score"] < _CONF_GATE
                                  or confidence.get("coverage", 100) < 40)
                    if (not st.session_state.structured_mode
                            and not _uploads_only
                            and _still_low):
                        _render_keyword_discovery(
                            user_q, confidence, result.get("kw_stats", [])
                        )

                # ── Gemini answer (Google Search grounding) ───────────────
                gemini_text = ""
                if hybrid:
                    with col_gemini:
                        st.markdown("**🔮 Gemini — Google Search**")
                        gemini_placeholder = st.empty()
                        gemini_placeholder.info("Searching the web…")

                        for chunk in gemini_answer_stream(
                            user_q,
                            st.session_state.gemini_client,
                            model=st.session_state.gemini_model,
                        ):
                            gemini_text += chunk
                            gemini_placeholder.markdown(gemini_text + "▌")

                        gemini_placeholder.markdown(gemini_text)

                    # Persist for refresh
                    st.session_state.gemini_last_q   = user_q
                    st.session_state.gemini_last_ans = gemini_text

                # Save selected nodes for conversation memory
                if result.get("selected"):
                    st.session_state.prev_selected_nodes = result["selected"]

                # Save to history
                history_content = full_answer
                if hybrid and gemini_text:
                    history_content = (
                        f"**⚖️ Claude (EUR-Lex):**\n\n{full_answer}"
                        f"\n\n---\n\n**🔮 Gemini (Web):**\n\n{gemini_text}"
                    )

                st.session_state.history.append(
                    {"role": "assistant", "content": history_content,
                     "references": result["references"], "meta": result}
                )
                st.session_state.references  = result["references"]
                st.session_state.kw_stats    = result.get("kw_stats", [])
                st.session_state.kw_docs_found = result.get("docs_found", 0)
                st.session_state.last_question = user_q
                st.rerun()   # show keyword stats panel immediately

    # ── Focus re-run handler ──────────────────────────────────────────────────
    # Fires when the user clicked "Re-run with focus" in the keyword stats panel.
    # Runs immediately on the next script execution (button sets session state,
    # st.rerun() restarts the script, this block executes before any new user_q).
    elif st.session_state.get("focus_rerun_q"):
        _focus_q   = st.session_state.focus_rerun_q
        _focus_kws = st.session_state.get("focus_rerun_kws", [])
        st.session_state.focus_rerun_q   = None
        st.session_state.focus_rerun_kws = []

        if not st.session_state.client:
            st.error("A valid API key is required for focused re-runs.")
        else:
            _label = (
                f"🎯 Re-run — focus on: "
                + ", ".join(f"**{k}**" for k in _focus_kws)
            )
            st.session_state.history.append({"role": "user", "content": _label})
            with st.chat_message("user"):
                st.markdown(_label)

            with st.chat_message("assistant"):
                _status_f = st.empty()
                _status_f.info("🎯 Re-navigating with keyword focus…")

                _fr = run_query(
                    question       = _focus_q,
                    sidebar_year   = sidebar_year,
                    delta          = int(delta),
                    max_laws       = int(max_laws),
                    client         = st.session_state.client,
                    sectors        = tuple(selected_sectors),
                    upload_trees   = st.session_state.upload_trees,
                    source_mode    = source_mode,
                    prev_selected  = st.session_state.prev_selected_nodes,
                    sub_questions  = [],
                    focus_keywords = _focus_kws,
                )

                if _fr["answer"] is not None:
                    _status_f.empty()
                    st.warning(_fr["answer"])
                    _fa_text = _fr["answer"]
                    if _fr.get("kw_stats"):
                        st.session_state.kw_stats      = _fr["kw_stats"]
                        st.session_state.kw_docs_found = _fr.get("docs_found", 0)
                else:
                    _fa_text  = ""
                    _status_f.info("✍️ Generating focused answer…")
                    _fa_ph = st.empty()
                    for _chunk in answer_stream(
                        _focus_q, _fr["selected"], st.session_state.client,
                        max_paragraphs=max_paragraphs,
                    ):
                        _fa_text += _chunk
                        _fa_ph.markdown(_fa_text + "▌")
                    _status_f.empty()
                    _fa_ph.markdown(_fa_text)

                    if _fr.get("selected"):
                        st.session_state.prev_selected_nodes = _fr["selected"]
                    st.session_state.references    = _fr["references"]
                    st.session_state.kw_stats      = _fr.get("kw_stats", [])
                    st.session_state.kw_docs_found = _fr.get("docs_found", 0)
                    # Keep last_question unchanged — still the original question

            st.session_state.history.append({
                "role":       "assistant",
                "content":    _fa_text,
                "references": _fr.get("references", []),
                "meta":       _fr,
            })


# ── Tab 2: References ──────────────────────────────────────────────────────────
with tab_refs:
    refs = st.session_state.references

    if not refs:
        st.info("Ask a question in the Chat tab — references will appear here.")
    else:
        st.markdown(f"### 📋 References  ({len(refs)} articles used)")

        # Full reference cards
        for r in refs:
            url_part = (
                f'<a href="{r["URL"]}" target="_blank">🔗 Open on EUR-Lex</a>'
                if r.get("URL") else ""
            )
            st.markdown(
                f"""<div class="ref-box">
                  <span class="celex-tag">{r['CELEX']}</span>
                  &nbsp;<span class="year-tag">{r['Year']}</span>
                  &nbsp;&nbsp;<strong>{r['Article']}</strong><br>
                  <small>{r['Summary']}</small><br>
                  <small>{url_part}</small>
                </div>""",
                unsafe_allow_html=True,
            )

        st.divider()

        # Download references as JSON
        st.download_button(
            "⬇️ Download references (JSON)",
            data=json.dumps(refs, indent=2),
            file_name=f"eurolex_refs_{datetime.now():%Y%m%d_%H%M%S}.json",
            mime="application/json",
            use_container_width=True,
        )

        # Follow-up question shortcut
        st.markdown("#### Follow-up")
        st.caption(
            "Ask a follow-up in the **Chat** tab. "
            "Your previous answer and references are remembered during this session."
        )


# ── Tab 3: Cache Manager ───────────────────────────────────────────────────────
with tab_cache:
    st.markdown("### 🗄️ Cached Reasoning Trees")
    st.caption(
        "Each row is a EUR-Lex document whose Article tree is stored locally. "
        "Select rows and delete to free space or force a fresh re-fetch."
    )

    col_refresh, col_delete, _ = st.columns([1, 1, 4])

    if col_refresh.button("🔄 Refresh", use_container_width=True):
        st.rerun()

    trees = list_cached_trees()

    if not trees:
        st.info(
            "No cached trees yet. Ask a question in the Chat tab "
            "to start building the cache."
        )
    else:
        st.caption(f"**{len(trees)} document(s)** cached "
                   f"in `{os.path.abspath(CACHE_DIR)}`")

        # Render a plain HTML table — avoids numpy/pandas version incompatibilities
        # that crash st.dataframe / st.table on Python 3.9 + numpy 1.21.
        _cols = ["CELEX", "Title", "Year", "Articles", "Cached"]
        _html = "<table style='width:100%;font-size:0.85em;border-collapse:collapse'>"
        _html += "<tr>" + "".join(
            f"<th style='text-align:left;padding:4px 8px;border-bottom:1px solid #ddd'>{c}</th>"
            for c in _cols
        ) + "</tr>"
        for row in trees:
            _html += "<tr>" + "".join(
                f"<td style='padding:3px 8px;border-bottom:1px solid #eee'>{str(row.get(c,''))[:80]}</td>"
                for c in _cols
            ) + "</tr>"
        _html += "</table>"
        st.markdown(_html, unsafe_allow_html=True)
        _celex_labels = [
            f"{row.get('CELEX', '?')} — {row.get('Title', '')[:50]}"
            for row in trees
        ]
        _selected_labels = st.multiselect(
            "Select trees to delete:",
            options=_celex_labels,
            default=[],
            key="cache_select",
        )
        selected_files = [
            trees[i]["_file"]
            for i, lbl in enumerate(_celex_labels)
            if lbl in _selected_labels
        ]

        n_selected = len(selected_files)
        col_delete.button(
            f"🗑️ Delete ({n_selected})",
            disabled=(n_selected == 0),
            use_container_width=True,
            key="delete_btn",
        )

        if st.session_state.get("delete_btn") and selected_files:
            removed = delete_trees(selected_files)
            st.success(f"Deleted {removed} tree(s).")
            time.sleep(0.8)
            st.rerun()

        st.divider()

        # Summary stats
        total_articles = sum(t["Articles"] for t in trees)
        years_covered  = sorted(set(t["Year"] for t in trees))
        c1, c2, c3 = st.columns(3)
        c1.metric("Documents cached", len(trees))
        c2.metric("Total articles indexed", total_articles)
        c3.metric("Years covered", f"{years_covered[0]}–{years_covered[-1]}" if years_covered else "—")

    # ── Uploaded files archive ─────────────────────────────────────────────────
    st.divider()
    st.markdown("### 📎 Uploaded Files Archive")
    st.caption(
        "Uploaded documents are saved as reasoning trees in "
        f"`{os.path.abspath(os.path.join(CACHE_DIR, 'uploads'))}/` "
        "and auto-loaded on every session start."
    )

    upload_archive = load_all_upload_trees(CACHE_DIR)
    if not upload_archive:
        st.info("No uploaded files archived yet. Upload PDF or text files in the sidebar.")
    else:
        for ut in upload_archive:
            col_info, col_del = st.columns([6, 1])
            n_sections = len(ut.get("nodes", []))
            indexed    = ut.get("indexed_at", "")[:10]
            col_info.markdown(
                f"**{ut['title']}** &nbsp; "
                f"<small style='color:grey'>{n_sections} sections · indexed {indexed}</small>",
                unsafe_allow_html=True,
            )
            if col_del.button("🗑️", key=f"del_upload_{ut['title']}", help=f"Delete {ut['title']}"):
                delete_upload_tree(CACHE_DIR, ut["title"])
                # Also remove from session state
                st.session_state.upload_trees = [
                    t for t in st.session_state.upload_trees
                    if t["title"] != ut["title"]
                ]
                st.rerun()

    # ── PDF Archive ────────────────────────────────────────────────────────────
    st.divider()
    st.markdown("### 📦 PDF Archive")
    st.caption(
        "Raw PDF files downloaded from EUR-Lex CELLAR are stored here for reuse. "
        "Place PDFs manually in the inbox to auto-index them on next app start."
    )

    from pipeline.pdf_archive import PDF_INBOX_DIR as _PDF_INBOX
    st.info(f"📂 **Inbox:** `{os.path.abspath(_PDF_INBOX)}`  "
            f"— place PDFs here and restart the app to auto-index them.")

    _archived = list_archived()
    if not _archived:
        st.info("No PDFs archived yet. They will be saved automatically as you query EUR-Lex.")
    else:
        st.caption(f"**{len(_archived)} PDF(s)** in archive")
        _a_cols = ["CELEX", "Year", "Size KB", "Path"]
        _a_html = "<table style='width:100%;font-size:0.82em;border-collapse:collapse'>"
        _a_html += "<tr>" + "".join(
            f"<th style='text-align:left;padding:4px 8px;border-bottom:1px solid #ddd'>{c}</th>"
            for c in _a_cols
        ) + "</tr>"
        for _ar in _archived:
            _a_html += "<tr>" + "".join(
                f"<td style='padding:3px 8px;border-bottom:1px solid #eee'>{str(_ar.get(k,''))}</td>"
                for k in ("celex", "year", "size_kb", "path")
            ) + "</tr>"
        _a_html += "</table>"
        st.markdown(_a_html, unsafe_allow_html=True)
