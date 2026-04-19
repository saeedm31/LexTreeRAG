"""
Post-retrieval scoring, reranking, and clause-level extraction.

Implements all retrieval quality improvements:
  #1  Clause-level granularity  — extract best paragraphs from each article
  #2  Obligation keyword scoring — boost "shall"/"must"/"required to"
  #3  Structural signal boosting — boost enumerated lists (a),(b),(c)
  #6  Anti-generic filter        — penalise scope/definitions articles
  #7  Answer validation gate     — detect whether nodes have answer-bearing content
"""

import re

# ── Constants ─────────────────────────────────────────────────────────────────

OBLIGATION_KEYWORDS: list[str] = [
    "shall",
    "must",
    "required to",
    "shall provide",
    "shall be required",
    "is required",
    "are required",
    "shall have available",
    "shall ensure",
    "shall submit",
    "shall notify",
]

# Titles that suggest a generic framework article
_GENERIC_TITLE_TOKENS: frozenset[str] = frozenset({
    "scope",
    "definitions",
    "definition",
    "general provisions",
    "general provision",
    "subject-matter",
    "subject matter",
    "objectives",
    "objective",
    "purpose",
    "aim",
    "aims",
    "applicability",
    "application",
    "introductory",
})

# Phrases that signal delegated/implementing meta-text — not the answer
_DELEGATION_PHRASES: list[str] = [
    "commission may adopt",
    "commission is empowered",
    "commission shall adopt",
    "delegated acts",
    "implementing acts",
    "may be adopted by means of",
    "acts referred to in article",
]


# ── Scoring functions ─────────────────────────────────────────────────────────

def obligation_score(text: str) -> int:
    """
    Count obligation keyword occurrences in *text*, weighted by specificity.
    Higher weight for multi-word phrases (they are more precise signals).
    """
    lower = text.lower()
    score = 0
    for kw in OBLIGATION_KEYWORDS:
        # Multi-word keywords score double
        weight = 3 if " " in kw else 2
        score += lower.count(kw) * weight
    return score


def list_score(text: str) -> int:
    """
    Detect enumerated legal lists:
      (a), (b), (c)         — alphabetical sub-items
      (i), (ii), (iii)      — roman numeral sub-items
      1. / 2. / 3.          — numbered paragraphs
      | col | col |         — annex/table rows (e.g. Regulation 2019/2144 annexes)
      — item / – item       — dash-list items
    Returns the item count (capped at a reasonable maximum to avoid overflow).
    """
    alpha  = len(re.findall(r"\([a-z]{1,3}\)", text))
    roman  = len(re.findall(r"\((?:i{1,3}|iv|vi{0,3}|ix|x{1,3})\)", text, re.I))
    table  = len(re.findall(r"^\s*\|.+\|", text, re.MULTILINE))
    dash   = len(re.findall(r"^\s*[–—-]\s+\S", text, re.MULTILINE))

    # Numbered items — only count as a list when there are 2+ items in the
    # same chunk OR the match is not the chunk's own paragraph number.
    # A single "1. Some text..." at the start of a chunk is a regulation
    # paragraph number, not a list entry — counting it inflates scores on
    # ordinary paragraph text and de-prioritises the actual (a)(b)(c) lists.
    raw_bullets = re.findall(r"^\s*\d+\.\s", text, re.MULTILINE)
    if len(raw_bullets) == 1 and re.match(r"^\s*\d+\.\s", text.strip()):
        bullet = 0   # lone paragraph-number prefix — not a list
    else:
        bullet = len(raw_bullets)

    return min(alpha + roman + bullet + table + dash, 30)  # cap to avoid overflow


def is_generic_section(title: str, text: str) -> bool:
    """
    Return True when an article is a generic framework section
    (scope / definitions / general provisions) AND has no meaningful
    obligation content.  These are penalised by rerank_nodes().

    Uses substring matching on the lowercased title so hyphenated forms
    like "Subject-matter" and multi-word phrases like "General provisions"
    are correctly detected without relying on tokenisation.
    """
    title_lower = title.lower()
    is_generic_title = any(tok in title_lower for tok in _GENERIC_TITLE_TOKENS)
    if not is_generic_title:
        return False  # title doesn't look generic → not penalised

    lower = text.lower()
    # Non-delegation obligation keywords only — "shall be adopted" in delegation
    # context doesn't make an article non-generic, so exclude delegation phrases
    has_real_obligation = (
        any(kw in lower for kw in OBLIGATION_KEYWORDS)
        and not any(ph in lower for ph in _DELEGATION_PHRASES)
    )
    return not has_real_obligation


# ── Clause-level extraction (Point #1) ───────────────────────────────────────

def extract_best_clauses(text: str, max_clauses: int = 6) -> str:
    """
    Split article text into paragraphs, score each for obligation / list content,
    and return the top-scoring paragraphs (in their original order).

    This implements Point #1: answer-bearing clause targeting rather than
    returning full article text that may start with generic header paragraphs.
    """
    # Split on blank lines OR on a numbered paragraph start (e.g. "2. The ...")
    paras = re.split(r"\n{2,}|\n(?=\d+\.\s)", text.strip())
    paras = [p.strip() for p in paras if len(p.strip()) > 40]

    if not paras or len(paras) <= max_clauses:
        return text  # short enough — return as-is

    # Score each paragraph
    scored: list[tuple[int, int, str]] = []
    for i, p in enumerate(paras):
        score = obligation_score(p) + list_score(p) * 3
        scored.append((score, i, p))

    # Pick top-N, then restore original document order
    top_indices: set[int] = {
        i for _, i, _ in sorted(scored, key=lambda x: -x[0])[:max_clauses]
    }
    return "\n\n".join(p for i, p in enumerate(paras) if i in top_indices)


# ── Node reranker (Points #2, #3, #6) ────────────────────────────────────────

def rerank_nodes(nodes: list[dict]) -> list[dict]:
    """
    Re-score and reorder navigator-selected nodes using:
      - Obligation keyword count    (+up to 6)
      - Enumerated list count       (+up to 6)
      - Generic section penalty     (−4)

    Scoring is always performed on the ORIGINAL text so that obligation headers
    like "The following systems shall be equipped..." are not lost by clause
    extraction.  The extracted (best-clause) text is stored separately as
    'text' for the LLM answer step, while 'obligation_score_raw' and
    'list_score_raw' record what was detected on the original full text.

    Returns nodes sorted by final score (highest first).
    """
    scored: list[tuple[float, dict]] = []
    for node in nodes:
        original_text = node.get("text", "")
        title         = node.get("title", "")

        # Score on ORIGINAL text — not on clause-extracted fragment
        obl_raw = obligation_score(original_text)
        lst_raw = list_score(original_text)

        base    = float(node.get("relevance_score", 5))
        obl     = min(obl_raw, 6)        # cap +6
        lst     = min(lst_raw * 2, 6)    # cap +6
        penalty = -4.0 if is_generic_section(title, original_text) else 0.0

        final = base + obl + lst + penalty

        # Clause-level extraction for LLM (uses more clauses for complex regs)
        best_text = extract_best_clauses(original_text) if len(original_text) > 400 else original_text
        updated   = {
            **node,
            "text":                best_text,
            "obligation_score_raw": obl_raw,   # signals from original text
            "list_score_raw":       lst_raw,
            "final_score":          round(final, 1),
        }

        scored.append((final, updated))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [n for _, n in scored]


# ── Answer validation gate (Points #7, #8, #9) ───────────────────────────────

def has_answer_signal(nodes: list[dict]) -> bool:
    """
    Return True if at least one node contains answer-bearing content.

    Checks (in priority order):
      1. obligation_score_raw / list_score_raw  — scores on the ORIGINAL full text
         (set by rerank_nodes; avoids false negatives caused by clause extraction)
      2. Direct re-check on current node text   — fallback if raw scores absent
      3. Navigator relevance_score >= 7          — trust the LLM's own judgment
         as a last resort when Python signal detection cannot confirm.

    A False result triggers iterative re-retrieval (Point #7).
    If retries are exhausted it triggers the hard failure rule (Point #9).
    """
    for node in nodes:
        # 1. Use pre-computed raw scores when available (most reliable)
        if node.get("obligation_score_raw", 0) > 0:
            return True
        if node.get("list_score_raw", 0) > 0:
            return True

        # 2. Direct check on whatever text is stored
        text = node.get("text", "")
        if obligation_score(text) > 0 or list_score(text) > 0:
            return True

        # 3. Trust LLM navigator score — if it scored ≥ 7 the article is
        #    clearly relevant even if Python signal patterns didn't match
        if node.get("relevance_score", 0) >= 7:
            return True

    return False


def obligation_expansion_keywords(question: str, base_keywords: list[str]) -> list[str]:
    """
    Point #4 / #7 — Query Expansion.

    If the question asks for requirements, documentation, or conditions,
    inject obligation-based search terms that are more likely to hit
    specific legal provisions rather than framework sections.
    """
    triggers = {
        "requir", "document", "proof", "evidence", "condition",
        "need to", "obligat", "mandatory", "certif", "approv",
        "how", "what", "which", "list",
    }
    q_lower = question.lower()
    if not any(t in q_lower for t in triggers):
        return base_keywords  # no expansion needed

    additions = ["shall", "required", "obligation", "documentation"]
    existing  = {k.lower() for k in base_keywords}
    return base_keywords + [a for a in additions if a not in existing]
