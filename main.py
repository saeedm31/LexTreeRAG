"""
EuroLex RAG — Vectorless Reasoning-Tree Pipeline
=================================================

Usage:
    python main.py
    python main.py --question "What are GDPR obligations for data controllers in 2018?"
    python main.py --question "..." --cache-dir ./data/cache --max-docs 6
"""

import os
import sys
import json
import argparse

from dotenv import load_dotenv
from rich.console import Console
from rich.panel   import Panel
from rich.table   import Table
from rich         import box
import anthropic

from pipeline.rag_engine import run_pipeline

load_dotenv()
console = Console()


# ── CLI args ──────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Query EUR-Lex using a vectorless LLM reasoning-tree pipeline."
    )
    p.add_argument(
        "--question", "-q",
        type=str,
        default=None,
        help="Your legal question (include a year, e.g. '… in 2023')",
    )
    p.add_argument(
        "--cache-dir",
        type=str,
        default=os.getenv("CACHE_DIR", "./data/cache"),
        help="Directory for local JSON tree cache  (default: ./data/cache)",
    )
    p.add_argument(
        "--max-laws",
        type=int,
        default=int(os.getenv("MAX_LAWS_PER_YEAR", "5")),
        help="Maximum number of EUR-Lex laws to retrieve per year (default: 5)",
    )
    p.add_argument(
        "--save-report",
        action="store_true",
        help="Save the answer + references to a JSON report file",
    )
    return p.parse_args()


# ── Display helpers ───────────────────────────────────────────────────────────

def print_banner() -> None:
    console.print(
        Panel.fit(
            "[bold blue]EuroLex Reasoning-Tree RAG[/bold blue]\n"
            "[dim]Vectorless · LLM-navigated · Live EUR-Lex search[/dim]",
            border_style="blue",
        )
    )


def print_references(references: list[dict]) -> None:
    if not references:
        return
    table = Table(
        title="References",
        box=box.SIMPLE_HEAVY,
        show_lines=True,
        title_style="bold magenta",
    )
    table.add_column("CELEX",   style="cyan",  no_wrap=True)
    table.add_column("Article", style="green")
    table.add_column("Year",    style="yellow", justify="center")
    table.add_column("Summary", style="white")
    table.add_column("URL",     style="blue",  no_wrap=False)

    for ref in references:
        table.add_row(
            ref.get("doc_id",  "—"),
            ref.get("article", "—"),
            str(ref.get("year", "—")),
            ref.get("summary", "")[:80],
            ref.get("url",     "—"),
        )
    console.print(table)


def save_report(result: dict, question: str, out_dir: str = "./data") -> str:
    import re
    from datetime import datetime
    os.makedirs(out_dir, exist_ok=True)
    slug = re.sub(r"[^\w]+", "_", question[:40]).strip("_")
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"report_{slug}_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"question": question, **result}, f, ensure_ascii=False, indent=2)
    return path


# ── Interactive REPL ──────────────────────────────────────────────────────────

def repl(cache_dir: str, max_docs: int, client: anthropic.Anthropic) -> None:
    console.print(
        "\n[bold]Enter your question below.[/bold] "
        "[dim]Include a year (e.g. 'in 2023'). Type [bold]exit[/bold] to quit.[/dim]\n"
    )
    while True:
        try:
            question = console.input("[bold green]❯ Question:[/bold green] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Bye.[/dim]")
            break

        if question.lower() in ("exit", "quit", "q"):
            console.print("[dim]Bye.[/dim]")
            break
        if not question:
            continue

        _ask(question, cache_dir, max_docs, client, save=False)


def _ask(
    question: str,
    cache_dir: str,
    max_docs: int,
    client: anthropic.Anthropic,
    save: bool = False,
) -> None:
    result = run_pipeline(
        question  = question,
        cache_dir = cache_dir,
        client    = client,
        max_docs  = max_docs,
    )

    # Print reference table
    console.print()
    print_references(result["references"])

    # Stats footer
    console.print(
        f"\n[dim]Docs found: {result['docs_found']}  "
        f"| Articles used: {result['nodes_used']}  "
        f"| Year: {result['year']}  "
        f"| Keywords: {result['keywords']}[/dim]"
    )

    if save:
        path = save_report(result, question)
        console.print(f"[green]Report saved → {path}[/green]")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        console.print(
            "[bold red]Error:[/bold red] ANTHROPIC_API_KEY not set. "
            "Copy .env.example → .env and add your key."
        )
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    os.makedirs(args.cache_dir, exist_ok=True)

    print_banner()

    if args.question:
        _ask(args.question, args.cache_dir, args.max_laws, client, save=args.save_report)
    else:
        repl(args.cache_dir, args.max_laws, client)


if __name__ == "__main__":
    main()
