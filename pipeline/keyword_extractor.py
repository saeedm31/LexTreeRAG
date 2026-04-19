"""
Extract search keywords and the year from a user question using Claude Haiku.
Fast and cheap — Haiku is sufficient for this task.
"""

import json
import re
import anthropic


def _extract_exact_signals(question: str) -> list[str]:
    """
    Extract explicit regulation numbers and article numbers from the question text.
    These are injected as high-priority keywords so the navigator can exact-match them.

    Examples:
      "Article 73 of Regulation 2019/631"  → ["2019/631", "article 73"]
      "under Directive 2014/65/EU"          → ["2014/65"]
      "Article 5 GDPR"                      → ["article 5"]
    """
    signals = []

    # Regulation / Directive numbers  — YEAR/NUMBER or NUMBER/YEAR
    for m in re.finditer(
        r"\b((?:19|20)\d{2}/\d{1,4}|\d{1,4}/(?:19|20)\d{2})(?:/[A-Z]+)?\b",
        question,
    ):
        signals.append(m.group(0).split("/")[0] + "/" + m.group(0).split("/")[1])

    # Explicit article numbers — "Article 73", "Articles 10-12", "Art. 5"
    for m in re.finditer(
        r"\bArt(?:icles?|icle|s?\.)\s*(\d+\w*(?:\s*[-–]\s*\d+\w*)?)\b",
        question,
        re.IGNORECASE,
    ):
        signals.append(f"article {m.group(1).strip()}")

    return signals


def extract_keywords_and_year(question: str, client: anthropic.Anthropic) -> dict:
    """
    Returns:
        {
            "keywords": ["data protection", "personal data", "controller"],
            "year": 2023,
            "domain": "data privacy",
            "exact_signals": ["2019/631", "article 73"]  # regulation/article refs
        }
    """
    prompt = f"""You are a legal research assistant specialising in EU law.

Given the user question below, extract search terms for querying the EUR-Lex title database.

CRITICAL RULES for keywords:
- Each keyword MUST be 1-2 words MAX (e.g. "charging", "alternative fuels", "GDPR", "deployment")
- Keywords must be words that would literally appear in an EU regulation or directive TITLE
- Think: "What words would be in the TITLE of the law this question is about?"
- Do NOT use: full phrases, descriptions, obligations, or question paraphrases
- Good examples: "alternative fuels", "data protection", "vehicle emissions", "food safety"
- Bad examples: "mandatory targets for member states", "obligations under directive", "infrastructure deployment requirements"

Extract:
1. 4-6 SHORT title-friendly keywords (1-2 words each)
2. The year mentioned (integer or null)
3. A short domain label (2-4 words)

Return ONLY a JSON object: {{"keywords": [...], "year": int|null, "domain": str}}

User question: {question}"""

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        year_match = re.search(r"\b(19|20)\d{2}\b", question)
        result = {
            "keywords": [w for w in question.lower().split() if len(w) > 4][:5],
            "year": int(year_match.group()) if year_match else None,
            "domain": "eu law",
        }

    # Ensure year is an int if present
    if result.get("year") and not isinstance(result["year"], int):
        try:
            result["year"] = int(result["year"])
        except (ValueError, TypeError):
            result["year"] = None

    # Attach exact regulation/article signals from regex (not LLM)
    result["exact_signals"] = _extract_exact_signals(question)

    # Point #4: Query expansion — inject obligation-based terms for requirement questions
    result["keywords"] = _expand_obligation_keywords(question, result.get("keywords", []))

    # Generate multi-query variants for broader SPARQL coverage (#4 / #10)
    result["query_variants"] = generate_query_variants(
        question,
        result["keywords"],
        result["exact_signals"],
    )

    return result


# ── Obligation query expansion (Point #4) ────────────────────────────────────

_EXPANSION_TRIGGERS = {
    "requir", "document", "proof", "evidence", "condition",
    "need to", "obligat", "mandatory", "certif", "approv",
    "how", "what must", "which document", "what proof",
    "when must", "must a", "must be", "must provide",
}

_EXPANSION_ADDITIONS = ["shall", "required", "obligation", "documentation"]


def _expand_obligation_keywords(question: str, keywords: list[str]) -> list[str]:
    """
    If the question asks for requirements, documentation, or conditions,
    inject obligation-based search terms.  These expand the SPARQL/title
    search to surface specific provision articles rather than framework sections.
    """
    q_lower = question.lower()
    if not any(t in q_lower for t in _EXPANSION_TRIGGERS):
        return keywords
    existing = {k.lower() for k in keywords}
    additions = [a for a in _EXPANSION_ADDITIONS if a not in existing]
    return keywords + additions[:2]  # add at most 2 extra terms to avoid over-broadening


# ── Multi-query variant generation (#4 / #10) ─────────────────────────────────

# Semantic synonyms: maps lay terms to equivalent legal title terms.
# Used to generate alternative keyword sets that may match different regulations.
_LEGAL_SYNONYMS: dict[str, str] = {
    "requirement":    "obligation",
    "requirements":   "obligations",
    "evidence":       "proof",
    "documentation":  "documents",
    "demonstrate":    "verify",
    "mandatory":      "required",
    "prohibition":    "prohibited",
    "reuse":          "recycling",
    "batteries":      "battery",
    "vehicle":        "motor vehicle",
    "vehicles":       "motor vehicles",
    "passenger car":  "passenger vehicle",
    "safety system":  "type-approval",
    "fine":           "penalty",
    "fines":          "penalties",
}


def generate_query_variants(
    question: str,
    keywords: list[str],
    exact_signals: list[str],
) -> list[list[str]]:
    """
    Generate up to 4 keyword sets for multi-pass SPARQL title search.
    Each variant targets a different angle on the same question so that
    different regulation titles are matched.

    Variant 1: base keywords (always first)
    Variant 2: regulation numbers + first 2 keywords (exact-reference queries)
    Variant 3: synonym-normalised keywords (#10 semantic layer)
    Variant 4: obligation-phrase variant (for requirement-type questions)
    """
    variants: list[list[str]] = [list(keywords)]

    # Variant 2 — explicit regulation/directive number + domain keywords
    reg_nums = [s for s in exact_signals if re.search(r"\d+/\d+", s)]
    if reg_nums:
        v2 = reg_nums[:2] + keywords[:2]
        if v2 != keywords:
            variants.append(v2)

    # Variant 3 — synonym normalisation
    v3 = [_LEGAL_SYNONYMS.get(kw.lower(), kw) for kw in keywords]
    if v3 != keywords:
        variants.append(v3)

    # Variant 4 — obligation-focused (for requirement-type questions)
    q_lower = question.lower()
    if any(t in q_lower for t in ["requir", "document", "evidence", "demonstrat", "prove"]):
        v4 = keywords[:3] + ["compliance", "obligation"]
        # Only add if different from already queued variants
        if v4 not in variants:
            variants.append(v4)

    # Deduplicate preserving order
    seen: set[tuple] = set()
    unique: list[list[str]] = []
    for v in variants:
        key = tuple(sorted(v))
        if key not in seen:
            seen.add(key)
            unique.append(v)

    return unique[:4]