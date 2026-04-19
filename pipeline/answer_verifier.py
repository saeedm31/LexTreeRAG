"""
Post-generation answer verification and confidence scoring.

Runs a Claude Haiku pass after the main answer to:
  - Score COVERAGE: how well selected articles cover the question (0-100)
  - Score GROUNDING: how well answer claims are backed by article text (0-100)
  - Compute overall CONFIDENCE = 0.4*coverage + 0.6*grounding
"""
import json, re
import anthropic

def compute_confidence(
    question: str,
    answer: str,
    selected_nodes: list[dict],
    client: anthropic.Anthropic,
) -> dict:
    """
    Returns:
        {
            "score":     int,   # 0-100 overall
            "level":     str,   # "high" / "medium" / "low"
            "coverage":  int,   # 0-100
            "grounding": int,   # 0-100
            "reasoning": str,   # plain-English explanation
        }
    """
    articles_summary = "\n".join(
        f"- [{n.get('doc_id','?')}] Article {n.get('title','?')}: {n.get('summary','')}"
        for n in selected_nodes[:12]
    )
    answer_snippet = answer[:1500]

    prompt = f"""You are a quality-control analyst for an EU legal research system.

User question: "{question}"

Articles retrieved:
{articles_summary}

Answer produced (first 1500 chars):
{answer_snippet}

Score on two dimensions (0-100 each):

COVERAGE — Do the retrieved articles cover all aspects of the question?
 90-100: All aspects fully covered
 70-89:  Most aspects covered, minor gaps
 50-69:  Core addressed but notable gaps
 30-49:  Partial coverage, significant gaps
 0-29:   Little relevant coverage

GROUNDING — Are the answer's claims traceable to the provided article texts?
 90-100: All claims clearly traceable
 70-89:  Most claims grounded, minor extrapolation
 50-69:  Mix of grounded and extrapolated
 30-49:  Many claims not clearly supported
 0-29:   Answer draws heavily on external knowledge

Also identify:
MISSING_ASPECTS — list (max 4) of specific aspects of the question NOT covered by the retrieved articles.
  Return [] if coverage >= 70.
SUGGESTED_KEYWORDS — list (max 5) of precise EU legal search terms that would likely find the missing content.
  Return [] if coverage >= 70.

Return ONLY valid JSON (no markdown):
{{
  "coverage": int,
  "grounding": int,
  "reasoning": "1-2 sentence explanation",
  "missing_aspects": ["aspect 1", ...],
  "suggested_keywords": ["keyword 1", ...]
}}"""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        coverage  = max(0, min(100, int(data.get("coverage", 50))))
        grounding = max(0, min(100, int(data.get("grounding", 50))))
        reasoning = str(data.get("reasoning", ""))
        missing_aspects    = data.get("missing_aspects", []) or []
        suggested_keywords = data.get("suggested_keywords", []) or []
        score = round(0.4 * coverage + 0.6 * grounding)
        level = "high" if score >= 75 else ("medium" if score >= 50 else "low")
        return {
            "score": score, "level": level, "coverage": coverage,
            "grounding": grounding, "reasoning": reasoning,
            "missing_aspects": missing_aspects,
            "suggested_keywords": suggested_keywords,
        }
    except Exception:
        return {
            "score": 50, "level": "medium", "coverage": 50,
            "grounding": 50, "reasoning": "Could not compute confidence score.",
            "missing_aspects": [], "suggested_keywords": [],
        }
