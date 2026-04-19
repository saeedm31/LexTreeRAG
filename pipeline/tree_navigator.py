"""
Vectorless navigation of the reasoning tree.

The LLM receives only the *slim tree* (article IDs + titles + summaries —
NO full text) for every document retrieved.  It returns the IDs of the
articles most likely to answer the user's question.  Only those articles'
full texts are then passed to the final answer step.

This mimics the PageIndex "human-like" search:
  1. Skim the table-of-contents / summaries  →  pick relevant articles
  2. Read only those articles in full         →  formulate the answer
"""

import json
import re
from typing import Optional
import anthropic


# ── Slim tree builder ─────────────────────────────────────────────────────────

def make_slim_tree(trees: list[dict]) -> str:
    """
    Produce a compact, token-efficient representation of all documents
    and their article summaries — suitable for fitting inside a single
    navigation prompt.

    Format:
      [DOC 1] 32016R0679 — GDPR (2016)
        art_1 | Article 1 "Subject-matter and objectives"
              | Establishes the regulation's scope ...
        art_2 | Article 2 "Material scope"
              | Defines what data processing this regulation applies to ...
      [DOC 2] ...
    """
    lines = []
    for doc in trees:
        lines.append(
            f"\n[DOC] {doc['doc_id']} — {doc['title']} ({doc['year']})"
        )
        for node in doc.get("nodes", []):
            # Sub-nodes are displayed indented under their parent — skip rendering
            # them as top-level entries; they'll appear after the parent.
            if node.get("parent_id"):
                continue

            # Build signal badges
            badges = ""
            if node.get("obligation_score", 0) >= 2:
                badges += "[OBL]"
            if node.get("list_score", 0) >= 2:
                badges += "[LIST]"
            badge_str = f" {badges}" if badges else ""

            if node.get("is_parent"):
                sub_count = node.get("sub_count", 0)
                lines.append(
                    f"  {node['id']} | Article {node['number']} \"{node['title']}\""
                    f" ({sub_count} parts){badge_str}"
                )
                lines.append(f"         | {node.get('summary', '—')}")
                # Render sub-nodes indented
                for sub in doc.get("nodes", []):
                    if sub.get("parent_id") != node["id"]:
                        continue
                    sub_num = sub.get("number", "")
                    lines.append(
                        f"    {sub['id']} | Article {sub_num}"
                    )
                    lines.append(f"           | {sub.get('summary', '—')}")
            else:
                lines.append(
                    f"  {node['id']} | Article {node['number']} \"{node['title']}\"{badge_str}"
                )
                lines.append(
                    f"         | {node.get('summary', '—')}"
                )
    return "\n".join(lines)


def _make_context_section(context_nodes: list[dict]) -> str:
    """
    Format previously-selected nodes as a PREVIOUS CONTEXT section
    so the navigator can reuse them for follow-up questions.
    """
    if not context_nodes:
        return ""
    lines = ["\n--- PREVIOUS CONTEXT (from previous question) ---"]
    for n in context_nodes[:8]:   # cap to avoid bloating the prompt
        lines.append(
            f"  [{n.get('doc_id','?')}] Article {n.get('title','?')}: "
            f"{n.get('summary','')}"
        )
    lines.append("--- END PREVIOUS CONTEXT ---\n")
    return "\n".join(lines)


# ── Navigation prompt ─────────────────────────────────────────────────────────

_NAV_SYSTEM = """\
You are a legal research assistant with expertise in EU law.
You will be shown a structured index of EUR-Lex documents and their articles.
Your job is to identify which specific articles are most relevant to a user question.

SIGNAL BADGES in the index:
  [OBL]  = article text contains obligation keywords (shall/must/required to). Score +2.
  [LIST] = article text contains enumerated lists (a),(b),(c). Score +2.
Articles marked [OBL] or [LIST] are more likely to directly answer the question.

SCORING RULES (apply strictly):
1. EXACT PROVISIONS FIRST — Prefer articles marked [OBL] or [LIST] and whose summary
   contains "shall", "must", "required to", "shall provide", "obligation", "prohibited".
   Score articles with [OBL]+[LIST] at least 1 point higher than equivalent articles without.
2. EXACT MATCH BOOST — If the question mentions a regulation number (e.g. 2019/631)
   or a specific article number (e.g. Article 73), boost articles from that exact
   regulation or with that article number to score 9-10.
3. QUERY EXPANSION AWARENESS — Questions about requirements, documentation, or conditions
   often use legal phrasing like "documentation required", "evidence to substantiate",
   "holder shall have available", "proof of functionality". Match articles that use
   this obligation-based vocabulary even if the question uses lay terms.
4. PENALISE CONTEXT DRIFT — Articles about "Commission may adopt", "delegated acts",
   "implementing acts", or general objectives/subject-matter should score ≤ 4 UNLESS
   they directly contain the answer.
5. SCOPE QUESTION EXCEPTION (overrides Rule 4) — If the question asks "what is in scope",
   "what categories", "what types", "which vehicles", "what falls under", "what does this
   cover", "what entities are covered", or "what applies to" — then Scope articles
   (Article 2) and Definitions articles (Article 3) ARE the primary answer.
   Score them 7-9 if their summary lists categories, types, or covered entities.
   Article 1 (Subject-matter) is still too general — prefer Article 2/3 for scope questions.
6. SPECIFIC BEATS GENERAL — If both a specific provision and a general framework
   article match, select the specific provision and drop the framework article.
7. NEVER SELECT JUST BECAUSE IT IS TANGENTIALLY RELATED — every selected article
   must directly contribute to answering the question.
8. NO SPECULATIVE SELECTION — Do NOT select an article because it "might" contain
   the answer or is from the right regulation but its summary doesn't confirm relevance.
   If the summary does not indicate the article addresses the question → score 0-3.
"""

def navigate_tree(
    question: str,
    trees: list[dict],
    client: anthropic.Anthropic,
    max_nodes: int = 8,
    context_nodes: Optional[list] = None,
    exact_signals: Optional[list] = None,
    focus_keywords: Optional[list] = None,
) -> list[dict]:
    """
    Ask Claude to navigate the slim tree and return a list of
    (doc_id, node_id, score) objects pointing to the most relevant articles.

    Args:
        context_nodes: Optional list of previously selected nodes (conversation
                       memory). When provided they are included in the prompt
                       under a PREVIOUS CONTEXT header so the navigator can
                       reuse or avoid repeating them for follow-up questions.

    Returns:
        [
          {"doc_id": "32016R0679", "node_id": "art_5",
           "title": "Article 5", "doc_title": "GDPR",
           "relevance_score": 9},
          ...
        ]
        Only nodes with relevance_score >= 4 are included.
    """
    slim = make_slim_tree(trees)
    context_section = _make_context_section(context_nodes or [])

    # Build exact-match hint line for the prompt
    exact_hint = ""
    if exact_signals:
        exact_hint = (
            f"\nEXACT MATCH PRIORITY: The question explicitly references "
            f"{', '.join(exact_signals)}. "
            f"Articles from those exact sources must score 9-10 if they address the question.\n"
        )

    focus_hint = ""
    if focus_keywords:
        focus_hint = (
            f"\nKEYWORD FOCUS (user-selected): The user specifically wants emphasis on: "
            f"{', '.join(focus_keywords)}. "
            f"Boost any article that prominently features these terms by +2 points. "
            f"Rank articles that don't mention them at all at least 1 point lower than similar ones that do.\n"
        )

    prompt = f"""Below is the index of {len(trees)} EUR-Lex document(s) with per-article summaries.

--- INDEX ---
{slim}
--- END INDEX ---
{context_section}{exact_hint}{focus_hint}
User question: {question}

Task: Select up to {max_nodes} article nodes that MOST DIRECTLY answer this question.

Scoring guidance (0-10):
- 9-10 : Article directly contains the specific legal provision, list of requirements,
         obligation, or scope definition that answers the question. Uses "shall", "must",
         "required", or lists specific categories/types covered.
         Or is from the exact regulation number / article number mentioned in the question.
- 7-8  : Article is clearly relevant and addresses the question's topic specifically.
         For scope/category questions: Article 2 (Scope) or Article 3 (Definitions)
         that list covered vehicle types / categories / entities score 8-9.
- 5-6  : Article is related but only partially answers the question.
- 4    : Article might be useful context but doesn't directly answer.
- 0-3  : Skip — too general, subject-matter article (Article 1), or about
         delegated/implementing acts not directly relevant.
         EXCEPTION: Article 1 scores 0-3 for scope/category questions
         (prefer Article 2 which defines the actual scope).

Return ONLY a JSON array with keys "doc_id", "node_id", "score":
[
  {{"doc_id": "32016R0679", "node_id": "art_5", "score": 9}},
  {{"doc_id": "32016R0679", "node_id": "art_13", "score": 7}}
]
If no article is relevant, return [].
"""

    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        system=_NAV_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    # Determine whether selections include score field
    _has_scores = True
    try:
        selections = json.loads(raw)
        if not isinstance(selections, list):
            raise ValueError
        # If items exist but have no "score" key, fall back to assigning score=7
        if selections and "score" not in selections[0]:
            _has_scores = False
    except (json.JSONDecodeError, ValueError):
        # Fallback: return the first node of the first doc with score=7
        if trees and trees[0].get("nodes"):
            t = trees[0]
            n = t["nodes"][0]
            return [
                {
                    "doc_id":          t["doc_id"],
                    "node_id":         n["id"],
                    "title":           n.get("title", ""),
                    "summary":         n.get("summary", ""),
                    "doc_title":       t.get("title", t["doc_id"]),
                    "year":            t.get("year", ""),
                    "doc_url":         t.get("url", ""),
                    "text":            n.get("text", ""),
                    "relevance_score": 7,
                }
            ]
        return []

    # Enrich with title, doc_title, and relevance_score for the caller
    tree_index = {t["doc_id"]: t for t in trees}
    enriched = []
    seen_node_ids: set = set()  # deduplicate when parent + sub-nodes both selected

    def _enrich_node(doc_id: str, node: dict, score: int, tree: dict) -> dict:
        return {
            "doc_id":          doc_id,
            "node_id":         node["id"],
            "title":           node.get("title", ""),
            "summary":         node.get("summary", ""),
            "doc_title":       tree.get("title", doc_id),
            "year":            tree.get("year", ""),
            "doc_url":         tree.get("url", ""),
            "text":            node.get("text", ""),
            "relevance_score": score,
        }

    for sel in selections:
        doc_id  = sel.get("doc_id", "")
        node_id = sel.get("node_id", "")
        score   = int(sel.get("score", 7)) if _has_scores else 7

        # Filter out low-relevance selections
        if score < 4:
            continue

        tree = tree_index.get(doc_id, {})
        node = next((n for n in tree.get("nodes", []) if n["id"] == node_id), None)
        if not node:
            continue

        key = f"{doc_id}:{node_id}"
        if key in seen_node_ids:
            continue
        seen_node_ids.add(key)

        enriched.append(_enrich_node(doc_id, node, score, tree))

        # When a parent node is selected, automatically include all its sub-nodes
        if node.get("is_parent"):
            for sub in tree.get("nodes", []):
                if sub.get("parent_id") != node_id:
                    continue
                sub_key = f"{doc_id}:{sub['id']}"
                if sub_key in seen_node_ids:
                    continue
                seen_node_ids.add(sub_key)
                enriched.append(_enrich_node(doc_id, sub, score, tree))

    return enriched
