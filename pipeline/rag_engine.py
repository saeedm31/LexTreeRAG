"""
Full pipeline orchestrator.

Flow for every user question:
  1.  Extract keywords + year  (Claude Haiku)
  2.  Search EUR-Lex SPARQL    (live, no PDFs needed)
  3.  For each document found:
        a. Fetch HTML → convert to Markdown
        b. Build / load reasoning tree (JSON cache)
  4.  Navigate trees           (Claude Haiku reads slim tree, picks articles)
  5.  Assemble context         (full text of chosen articles only)
  6.  Generate streamed answer (Claude Opus 4.6 + adaptive thinking)
  7.  Return answer + structured references
"""

from __future__ import annotations
import os
from typing import Generator

import anthropic
from rich.console import Console

from .keyword_extractor import extract_keywords_and_year
from .eurolex_client    import search_eurlex, fetch_document_markdown
from .tree_builder      import build_tree
from .tree_navigator    import navigate_tree

console = Console()

# ── System prompt for the final answer ───────────────────────────────────────

_ANSWER_SYSTEM = """\
You are an expert EU legal analyst.
You answer questions based ONLY on the EUR-Lex articles provided.
Always:
  • Cite the specific Article number and regulation name for every claim.
  • Distinguish between obligations, rights, definitions, and exceptions.
  • If the provided articles do not fully answer the question, say so clearly.
  • Keep the answer structured: use short paragraphs or bullet points.
  • End with a "References" section listing each article used.
"""


# ── Context builder ───────────────────────────────────────────────────────────

def _build_context(selected_nodes: list[dict]) -> str:
    """Format the retrieved article texts as a numbered context block."""
    parts = []
    for i, node in enumerate(selected_nodes, 1):
        parts.append(
            f"[{i}] {node['doc_title']} ({node['year']}) — "
            f"Article {node.get('title', node['node_id'])}\n"
            f"CELEX: {node['doc_id']}  |  URL: {node['doc_url']}\n\n"
            f"{node['text']}"
        )
    return "\n\n---\n\n".join(parts)


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_pipeline(
    question: str,
    cache_dir: str,
    client: anthropic.Anthropic,
    max_docs: int = 8,
) -> dict:
    """
    Run the full vectorless RAG pipeline and return:
        {
            "answer":      str,           # full answer text
            "references":  list[dict],    # [{doc_id, article, title, url}]
            "keywords":    list[str],
            "year":        int | None,
            "docs_found":  int,
            "nodes_used":  int,
        }
    Streams the answer to the terminal while building it.
    """

    # ── Step 1: extract keywords ──────────────────────────────────────────────
    console.print("\n[bold cyan]▶ Extracting keywords…[/bold cyan]")
    meta = extract_keywords_and_year(question, client)
    keywords = meta["keywords"]
    year     = meta["year"]
    console.print(f"  Keywords : {keywords}")
    console.print(f"  Year     : {year}")

    if not year:
        return {
            "answer": (
                "Please include a specific year in your question "
                "(e.g. '… in 2023')."
            ),
            "references": [],
            "keywords":   keywords,
            "year":       None,
            "docs_found": 0,
            "nodes_used": 0,
        }

    # ── Step 2: search EUR-Lex ────────────────────────────────────────────────
    console.print(f"\n[bold cyan]▶ Searching EUR-Lex for {year}…[/bold cyan]")
    docs = search_eurlex(keywords, year, max_results=max_docs)
    console.print(f"  Found {len(docs)} document(s).")

    if not docs:
        return {
            "answer": (
                f"No EUR-Lex documents found for year {year} "
                f"with keywords: {', '.join(keywords)}."
            ),
            "references": [],
            "keywords":   keywords,
            "year":       year,
            "docs_found": 0,
            "nodes_used": 0,
        }

    # ── Step 3: build reasoning trees ────────────────────────────────────────
    console.print("\n[bold cyan]▶ Building reasoning trees…[/bold cyan]")
    trees = []
    for doc in docs:
        console.print(f"  [{doc['celex']}] {doc['title'][:60]}…", end=" ")
        md = fetch_document_markdown(doc["celex"])
        if not md:
            console.print("[yellow]skip (fetch failed)[/yellow]")
            continue
        tree = build_tree(
            celex     = doc["celex"],
            title     = doc["title"],
            date      = doc["date"],
            url       = doc["url"],
            markdown  = md,
            cache_dir = cache_dir,
            client    = client,
        )
        trees.append(tree)
        node_count = len(tree.get("nodes", []))
        console.print(f"[green]{node_count} articles[/green]")

    if not trees:
        return {
            "answer": "Could not retrieve document content from EUR-Lex.",
            "references": [],
            "keywords":   keywords,
            "year":       year,
            "docs_found": len(docs),
            "nodes_used": 0,
        }

    # ── Step 4: navigate trees ────────────────────────────────────────────────
    console.print("\n[bold cyan]▶ Navigating trees (LLM picks relevant articles)…[/bold cyan]")
    selected = navigate_tree(question, trees, client)
    console.print(f"  Selected {len(selected)} article(s).")
    for s in selected:
        console.print(
            f"    • {s['doc_id']} — Article {s.get('title','?')} "
            f"({s.get('summary','')[:60]}…)"
        )

    if not selected:
        return {
            "answer": (
                "The retrieved documents do not appear to contain "
                "information relevant to your question."
            ),
            "references": [],
            "keywords":   keywords,
            "year":       year,
            "docs_found": len(docs),
            "nodes_used": 0,
        }

    # ── Step 5: build context ─────────────────────────────────────────────────
    context = _build_context(selected)

    user_prompt = f"""Use the EUR-Lex articles below to answer the question.

QUESTION: {question}

ARTICLES:
{context}

Answer in clear, structured language. Cite every article you rely on."""

    # ── Step 6: stream the answer ─────────────────────────────────────────────
    console.print("\n[bold cyan]▶ Generating answer (streaming)…[/bold cyan]\n")
    console.rule("[bold green]Answer[/bold green]")

    answer_parts = []
    with client.messages.stream(
        model     = "claude-opus-4-6",
        max_tokens= 4096,
        thinking  = {"type": "adaptive"},
        system    = _ANSWER_SYSTEM,
        messages  = [{"role": "user", "content": user_prompt}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            answer_parts.append(text)

    print()   # newline after stream
    console.rule()

    full_answer = "".join(answer_parts)

    # ── Step 7: build references ──────────────────────────────────────────────
    references = [
        {
            "doc_id":  s["doc_id"],
            "article": f"Article {s.get('title', s['node_id'])}",
            "summary": s.get("summary", ""),
            "url":     s.get("doc_url", ""),
            "year":    s.get("year", year),
        }
        for s in selected
    ]

    return {
        "answer":     full_answer,
        "references": references,
        "keywords":   keywords,
        "year":       year,
        "docs_found": len(docs),
        "nodes_used": len(selected),
    }
