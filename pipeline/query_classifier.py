"""
Classify incoming question to route retrieval strategy.
Decomposes complex multi-part questions into focused sub-questions.
"""
import json, re
import anthropic

_TYPES = ("definitional", "compliance", "comparison", "timeline", "complex", "general")

def classify_question(question: str, client: anthropic.Anthropic) -> dict:
    """
    Returns:
        {
            "type": one of _TYPES,
            "sub_questions": [],  # non-empty only for "complex"
            "reasoning": "short explanation"
        }
    """
    prompt = f"""You are an EU legal research assistant. Classify this legal question.

Question: {question}

Classify as exactly one of:
- "definitional": asks for the meaning/definition of a legal concept or term
- "compliance": asks whether something is required, permitted, or how to comply
- "comparison": asks to compare two regulations, articles, or concepts
- "timeline": asks about changes over time, historical evolution, or what changed in a year
- "complex": has 2+ clearly distinct sub-questions to research separately
- "general": any other legal question

For "complex" ONLY, decompose into 2-4 focused sub-questions (each independently researchable).
For all other types, return sub_questions as an empty list [].

Return ONLY a JSON object:
{{"type": "...", "sub_questions": [], "reasoning": "1 sentence"}}"""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        result = json.loads(raw)
        if result.get("type") not in _TYPES:
            result["type"] = "general"
        result.setdefault("sub_questions", [])
        result.setdefault("reasoning", "")
        return result
    except Exception:
        return {"type": "general", "sub_questions": [], "reasoning": "fallback"}
