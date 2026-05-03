# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run web UI
streamlit run app.py           # http://localhost:8501

# Run CLI (interactive REPL)
python main.py

# Run CLI (single question)
python main.py -q "What are GDPR obligations for data controllers in 2018?"

# Run all tests
pytest tests/

# Run a single test file
pytest tests/test_clause_ranker.py

# Run a single test
pytest tests/test_tree_builder.py::test_normalize_pdf_text
```

**Environment:** Copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`. Python 3.9+ required.

## Architecture

LexTreeRAG answers legal questions by navigating structured Article Trees — no vector database, no embeddings. Claude Haiku handles fast/cheap tasks; Claude Opus 4.6 generates the final answer with adaptive thinking.

**Pipeline (7 steps):**

1. **Keyword Extraction** (`pipeline/keyword_extractor.py`) — Haiku extracts keywords, year, domain, `query_variants`, and `exact_signals` (cited regulation/article numbers).

2. **EUR-Lex SPARQL Search** (`pipeline/eurolex_client.py`) — Queries the CELLAR SPARQL endpoint. Runs a 4-pass strategy per year scope (Pass 0: AND compound on sector-3 only; Pass 1–3: OR with progressively noisy terms). Results are client-side re-ranked by keyword hit count. Never uses EUR-Lex web search (blocked by AWS WAF).

3. **PDF Archive + Tree Builder** (`pipeline/pdf_archive.py`, `pipeline/tree_builder.py`) — PDFs are cached to `data/pdfs/{year}/{celex}.pdf` on first download. Trees are built once and saved to `data/cache/{year}/{celex}.json`. Articles >5,000 chars are sub-chunked into navigable parts (`art_5_1`, `art_5_2`, …). The inbox `data/pdfs/inbox/` is scanned at app startup and auto-indexed.

4. **Tree Navigation** (`pipeline/tree_navigator.py`) — Haiku receives only the slim tree (article IDs + one-sentence summaries, ~3,000 tokens). It returns article IDs with relevance scores 0–10. Articles below score 4 are filtered. Selecting a parent node auto-includes all its sub-nodes. Accepts `context_nodes` for conversation memory.

5. **Answer Generation** (`pipeline/rag_engine.py` for CLI; inline in `app.py` for web) — Opus 4.6 with `thinking: {"type": "adaptive"}`. Two modes: streaming prose (default) or structured JSON (Summary / Obligations / Rights / Definitions / Exceptions / Procedure / References).

6. **Confidence Scoring** (`pipeline/answer_verifier.py`) — Haiku scores `coverage` (0–100) and `grounding` (0–100); composite = `0.4×coverage + 0.6×grounding`. Returns `missing_aspects` and `suggested_keywords` when coverage is low.

7. **Retry Loop** (web UI only) — If `score < 50` OR `coverage < 40`, up to 2 silent retries run: Attempt 1 uses obligation-expanded keywords; Attempt 2 drops year/sector filters. Best answer wins. After 3 failed attempts, the Keyword Discovery UI appears.

**Web UI only extras:** `pipeline/query_classifier.py` pre-classifies questions (handles complex → sub-question decomposition); `pipeline/doc_ingester.py` handles uploaded PDF/TXT/MD files; `pipeline/gemini_search.py` provides an optional second answer panel via Google Search grounding.

## Key Design Constraints

- **`st.dataframe` / `st.data_editor` crash** on Python 3.9 due to a numpy/pandas incompatibility. All tabular data in `app.py` is rendered via `st.markdown(..., unsafe_allow_html=True)`.
- The **CLI pipeline** (`rag_engine.py`) is simpler than the web pipeline (`app.py`) — it has no query classification, no confidence retry loop, no upload handling, and no Gemini layer.
- The **JSON tree cache and PDF archive are independent**: delete `data/cache/` to force tree rebuilds (e.g. after `tree_builder.py` changes) while keeping PDFs so no re-download occurs.
- EUR-Lex **sector 3** = original legislation (preferred); **sector 0** = consolidated snapshots (often lack PDF/HTML content in CELLAR). Pass 0 restricts to sector 3; sector 0 results get a −0.5 ranking penalty.
- Tests do **not call the Anthropic API** — they mock navigator output with realistic article texts and test the Python-side pipeline components (`clause_ranker`, `keyword_extractor`, `tree_builder`, `tree_navigator`).
