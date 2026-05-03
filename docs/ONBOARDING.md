# 🎓 LexTreeRAG Developer Onboarding Guide

Welcome to LexTreeRAG! This guide helps new developers understand the codebase and get productive quickly.

---

## 📚 Project Overview

**LexTreeRAG** is a **vectorless legal RAG system** for EU law. It answers questions about regulations published on [EUR-Lex](https://eur-lex.europa.eu) by:

1. **Downloading** regulations live from EUR-Lex (no pre-indexed database)
2. **Parsing** them into hierarchical **Article Trees** (no embeddings or vector database)
3. **Using Claude Haiku** to navigate the tree and select relevant articles
4. **Using Claude Opus 4.6** to generate a grounded answer from selected articles

**Key innovation:** Vectorless navigation mimics how a lawyer reasons through an index — article-level precision without embedding overhead.

**Tech Stack:**
- **Language:** Python 3.9+
- **Web UI:** Streamlit
- **LLM (fast tasks):** Claude Haiku 4.5 (keyword extraction, tree navigation, confidence scoring)
- **LLM (final answer):** Claude Opus 4.6 with adaptive thinking
- **Data source:** EUR-Lex CELLAR SPARQL endpoint (free, no auth)
- **Optional:** Google Gemini for hybrid mode with Google Search grounding

---

## 🏛️ Architecture Layers

The codebase is organized into 8 architectural layers, each with a specific responsibility:

### 1. **Entry Points** (`app.py`, `main.py`)
User-facing interfaces to the system:
- **`app.py` (1,863 lines):** Streamlit web UI — features include PDF uploads, confidence scoring, Gemini second opinion, and keyword analysis expander
- **`main.py` (191 lines):** CLI REPL interface — lightweight terminal access with rich-formatted output
- **Key function:** `app.py:run_query()` — orchestrates the web pipeline

**Where to start:** These files tell you what users can do with the system.

### 2. **Pipeline Orchestration** (`pipeline/rag_engine.py`)
CLI-specific orchestrator that chains all steps:
- **`rag_engine.py` (227 lines):** Implements the full pipeline as a single `answer_question()` call used by `main.py`
- Sequences: keyword extraction → SPARQL search → tree building → LLM navigation → answer generation

**When you'd edit this:** To add new pipeline steps or change the CLI flow.

### 3. **Retrieval** (Keyword extraction, SPARQL search, document fetching)
Finds and fetches EU legal documents:

- **`keyword_extractor.py` (210 lines):** Claude Haiku extracts keywords, year, domain, and explicit regulation/article signals from the user question
  - `extract_keywords_and_year()` — Main entry point
  - `_extract_exact_signals()` — Finds "2016/679", "Article 5" patterns
  - `generate_query_variants()` — Creates multi-pass search variants
  - `obligation_expansion_keywords()` — Compliance-focused keyword boost

- **`eurolex_client.py` (905 lines):** Queries EUR-Lex CELLAR SPARQL endpoint and fetches documents
  - `search_eurlex()` — SPARQL queries with 4-pass strategy (AND compound → OR specific → OR noisy → OR no-sector)
  - `fetch_documents_parallel()` — ThreadPoolExecutor fetches multiple documents concurrently
  - `follow_citation_links()` — Expands retrieval by fetching cited regulations
  - `extract_cited_regulations()` — Regex extracts regulation citations from text

- **`pdf_archive.py` (212 lines):** Local PDF archive and inbox scanning
  - `scan_inbox()` — Auto-indexes PDFs in `data/pdfs/inbox/` at startup
  - `save_pdf_bytes()`, `load_pdf_bytes()` — Persistent PDF storage

**Complexity:** `eurolex_client.py` is the most complex — it manages SPARQL queries, parallel fetching, HTML/PDF conversion, and rate limiting.

**Key insight:** All search is **title-only SPARQL** — no full-text search at CELLAR. EU regulation titles are precise enough that this works well.

### 4. **Tree Processing** (Article tree building and navigation)
Core innovation — parsing documents into navigable trees:

- **`tree_builder.py` (486 lines):** Parses Markdown into hierarchical Article Trees
  - `build_tree()` — Main entry point; calls Claude Haiku to summarize each node
  - `normalize_pdf_text()` — Fixes PDF extraction artefacts (inter-character spaces, hyphenation)
  - `_paragraph_fallback_chunks()` — Splits unstructured documents into ~3,000 char chunks
  - Articles >5,000 chars are sub-chunked into sub-nodes (`art_5_1`, `art_5_2`, …)

- **`tree_navigator.py` (312 lines):** **Vectorless** LLM-based article selection
  - `navigate_tree()` — Renders a "slim tree" (article IDs + summaries, ~3,000 tokens) and asks Claude Haiku to select relevant articles
  - Only selected full texts are passed to the answer LLM

- **`doc_ingester.py` (223 lines):** Handles user-uploaded PDFs/TXT
  - `ingest_uploaded_file()` — Converts uploads into the same tree format
  - Trees are cached in `data/cache/uploads/` and auto-loaded on session start

**Key pattern:** The "slim tree" is the innovation. It lets Claude reason about relevance like a lawyer scanning a regulation index — no cosine similarity needed.

### 5. **Quality Control** (Reranking, confidence scoring, question routing)
Improves answer quality before and after generation:

- **`answer_verifier.py` (104 lines):** Post-generation confidence scoring
  - `score_answer()` — Haiku scores COVERAGE (how well articles address the question) and GROUNDING (how well claims are backed by text)
  - Triggers retry loop if score < 50 or coverage < 40

- **`clause_ranker.py` (266 lines):** Pre-generation node reranking
  - `rerank_nodes()` — Combines obligation score, list score, answer signal detection
  - `obligation_score()` — Counts "shall", "must", "required" → obligation-heavy articles rank higher
  - `list_score()` — Scores enumerated lists (a), (b), (c) → structural richness
  - `has_answer_signal()` — Filters out meta/delegation articles before they reach the answer context

- **`query_classifier.py` (54 lines):** Question type routing (web UI only)
  - `classify_query()` — Routes questions as definitional, compliance, comparison, timeline, complex, or general
  - For complex questions, decomposes into 2-4 sub-questions; navigator runs once per sub-question

**Key concept:** Obligation scoring is crucial — EU regulations often list requirements in specific structural patterns. Boosting those patterns improves answer quality.

### 6. **External Integrations** (Gemini, package init)
Integration with external AI services:

- **`gemini_search.py` (218 lines):** Optional Google Gemini + Google Search grounding
  - `get_gemini_answer()` — Sends question + relevant articles to Gemini for a second opinion
  - Enabled only if `GEMINI_API_KEY` is set in `.env`

- **`pipeline/__init__.py` (1 line):** Package initializer (empty)

### 7. **Tests** (`tests/` directory)
Pytest test suite with 2,100+ lines covering all modules:

- **`test_clause_ranker.py` (428 lines):** Unit tests for obligation/list scoring, reranking, answer signal detection
- **`test_keyword_extractor.py` (99 lines):** Keyword extraction and year detection
- **`test_pipeline_scenarios.py` (807 lines):** Integration tests with 6 real EU regulation scenarios
- **`test_retrieval_quality.py` (462 lines):** Cross-module retrieval quality tests
- **`test_tree_builder.py` (167 lines):** Article tree parsing, sub-chunking, normalization
- **`test_tree_navigator.py` (143 lines):** Vectorless navigation tests

**Note:** Tests **do not call the Anthropic API** — they mock LLM outputs with realistic article texts and verify the Python-side pipeline components.

### 8. **Docs and Config**
Documentation and configuration:

- **`README.md`:** Public overview, quick start, feature list, CLI/UI reference
- **`TECHNICAL_DOCS.md`:** 764-line deep dive into architecture, pipeline steps, data formats, design decisions
- **`CLAUDE.md`:** Claude Code developer guidance (commands, architecture summary, constraints)
- **`requirements.txt`:** Python dependencies (anthropic, streamlit, google-genai, requests, beautifulsoup4, html2text, python-dotenv, rich)
- **`.env.example`:** Template for environment variables (ANTHROPIC_API_KEY, GEMINI_API_KEY, cache directories)

---

## 🚀 Guided Tour for New Developers

Follow this path to understand the codebase step by step:

### Step 1: Project Overview → `README.md`
Read the README to understand the vectorless concept and see the architecture diagram.

### Step 2: Entry Points → `app.py` + `main.py`
- Skim `app.py` to see the Streamlit UI structure (sidebar controls, tabs, session state)
- Skim `main.py` to see how CLI args map to pipeline options
- **Key question:** Where does the user question come from?

### Step 3: Retrieval Pipeline → `keyword_extractor.py` + `eurolex_client.py`
- Read `keyword_extractor.py:extract_keywords_and_year()` to see how questions become search signals
- Read `eurolex_client.py:search_eurlex()` to understand the 4-pass SPARQL strategy
- Trace through `fetch_documents_parallel()` to see concurrent fetching
- **Key question:** How does the system find the right regulations?

### Step 4: Article Tree Construction → `tree_builder.py` + `pdf_archive.py`
- Read `tree_builder.py:build_tree()` to see how Markdown becomes a tree
- Understand sub-chunking logic (articles >5,000 chars → multiple nodes)
- See how `pdf_archive.py:scan_inbox()` auto-indexes dropped PDFs
- **Key question:** What does a cached tree JSON file look like?

### Step 5: LLM Navigation and Answering → `tree_navigator.py` + `rag_engine.py`
- Read `tree_navigator.py:navigate_tree()` — this is the core innovation
- See how the "slim tree" (summaries only) is presented to Claude Haiku
- Trace `rag_engine.py:answer_question()` to see the full CLI pipeline
- **Key question:** Why is vectorless navigation better than embeddings?

### Step 6: Quality Control → `answer_verifier.py` + `clause_ranker.py`
- Read `clause_ranker.py:obligation_score()` to see how obligation detection works
- Understand `answer_verifier.py:score_answer()` confidence metrics
- See the retry loop trigger (score < 50 OR coverage < 40)
- **Key question:** How does the system improve low-confidence answers?

### Step 7: Web-Only Extras → `query_classifier.py` + `doc_ingester.py` + `gemini_search.py`
- `query_classifier.py:classify_query()` — routing by question type
- `doc_ingester.py:ingest_uploaded_file()` — user PDF uploads
- `gemini_search.py:get_gemini_answer()` — Google Search grounding layer
- **Key question:** What can the web UI do that the CLI cannot?

---

## 🔍 Key Concepts and Patterns

### Vectorless Navigation
Instead of embedding text and using cosine similarity, LexTreeRAG presents Claude with a "slim tree":
```
Article 5 — Principles (relevance score: 8/10)
Article 6 — Lawful basis (relevance score: 7/10)
Article 7 — Consent (relevance score: 6/10)
```
Claude Haiku is given only this tree + the user question, selects articles >4/10, and returns their full text. This is **article-level precision** without embedding overhead.

### Two-Model Strategy
- **Claude Haiku:** Fast, cheap tasks (keyword extraction, tree summarization, navigation, confidence scoring)
- **Claude Opus 4.6:** Final answer generation with adaptive thinking — requires legal reasoning depth

### SPARQL Title-Only Search
The system queries EUR-Lex's CELLAR SPARQL endpoint by **document title only** — not full-text search. This works because:
- EU regulation titles are precise and include key terminology
- Title-only queries are fast and don't require PDF indexing
- Keyword expansion (synonyms, pluralization) handles lay terminology

### Caching Strategy
- **JSON trees:** `data/cache/{year}/{celex}.json` — article trees (deleted → rebuilt)
- **PDF archive:** `data/pdfs/{year}/{celex}.pdf` — raw PDFs (deleted → re-downloaded)
- **Upload trees:** `data/cache/uploads/{filename}.json` — user-uploaded files (persistent across sessions)

The JSON cache and PDF archive are **independent** — you can clear trees (forcing rebuild) while keeping PDFs to avoid re-downloads.

### Confidence-Gated Retry Loop (Web UI only)
If an answer scores low (coverage < 40 or score < 50), two silent retries run automatically:
1. **Attempt 1:** Obligation-expanded keywords (e.g., "data protection" → "data protection shall", "must process")
2. **Attempt 2:** Broad search with no year filter and expanded sectors

The best answer (by score) replaces the displayed answer. If all attempts fail, the Keyword Discovery UI shows missing aspects and suggests new search terms.

### Obligation Scoring
EU regulations express requirements using specific keywords: "shall", "must", "required to", "shall ensure", etc. Articles with high obligation-keyword density are more likely to contain concrete requirements than definitions or procedural text.

---

## 🛠️ Common Development Tasks

### Running Tests
```bash
pytest tests/                          # Run all tests
pytest tests/test_clause_ranker.py     # Single test file
pytest tests/test_tree_builder.py::test_normalize_pdf_text  # Single test
```

### Starting the Web UI
```bash
streamlit run app.py  # Opens http://localhost:8501
```

### Running the CLI
```bash
python main.py                                                       # Interactive REPL
python main.py -q "What are GDPR obligations for data controllers?" # Single question
```

### Adding a New Pipeline Step
1. Create a new file in `pipeline/` with your function
2. Add it to the imports in `rag_engine.py` (CLI) and `app.py` (web)
3. Insert the step into the pipeline chain
4. Add unit tests in `tests/`

### Debugging SPARQL Queries
1. Check `pipeline/eurolex_client.py:_build_sparql_query()` for the current query template
2. The 4-pass strategy is implemented in `search_eurlex()` — trace through the passes to see which one found your document
3. Look at `_expand_keywords()` to see synonym expansion for domain terms

---

## ⚠️ Complexity Hotspots

These areas require careful understanding before modifying:

### 🔴 **`eurolex_client.py` (High Complexity)**
- **Why:** Manages SPARQL queries, parallel HTTP fetching, HTML/PDF conversion, rate limiting, citation following
- **Key challenge:** The 4-pass keyword strategy has many edge cases (noisy terms, sector filtering, year resolution)
- **Testing:** Make sure any SPARQL changes still pass `test_retrieval_quality.py` scenarios

### 🔴 **`tree_builder.py` (High Complexity)**
- **Why:** Article parsing regex is fragile; sub-chunking logic must preserve all content
- **Key challenge:** pypdf extraction produces many artefacts (inter-character spaces, hyphenation)
- **Testing:** Verify on real EUR-Lex PDFs with `test_tree_builder.py`

### 🟡 **`clause_ranker.py` (Moderate Complexity)**
- **Why:** Obligation scoring heuristics must balance precision (avoiding false positives) and recall
- **Key challenge:** Different EU regulations have different obligation keyword patterns
- **Testing:** Scenario tests in `test_pipeline_scenarios.py` cover 6 real regulations

### 🟡 **`answer_verifier.py` (Moderate Complexity)**
- **Why:** Coverage and grounding scores must correlate with actual answer quality
- **Key challenge:** Defining what "coverage" and "grounding" mean in practice is subjective
- **Testing:** Empirical — compare scores to human annotations on real questions

---

## 📖 Further Reading

1. **TECHNICAL_DOCS.md** — In-depth pipeline documentation, data formats, design decisions, external dependencies
2. **CLAUDE.md** — Claude Code developer guidance: commands, architecture, constraints
3. **README.md** — Features, quick start, UI/CLI reference, configuration

---

## 💡 Developer Tips

### Incremental Testing
The test suite does **not** call the Anthropic API — it mocks LLM outputs. This lets you test the Python-side pipeline logic without API costs or latency. When you modify a pipeline module, run its tests first:
```bash
pytest tests/test_<module>.py -v
```

### Local Development Workflow
1. Copy `.env.example` to `.env` and fill in your `ANTHROPIC_API_KEY`
2. Run `streamlit run app.py` for interactive development
3. Save test output to a JSON file and iterate on the Python logic

### Understanding the Slim Tree
The slim tree is the key innovation. To see one, run:
```bash
python main.py -q "What is GDPR?" 2>&1 | grep -A 30 "slim tree" 
```
This shows exactly what Claude Haiku sees when selecting articles.

### Debugging SPARQL Results
If a question doesn't find the right regulation:
1. Check what keywords were extracted: look for the `keywords` key in the intermediate output
2. Check the SPARQL query: it's built in `_build_sparql_query()`
3. Try manually querying CELLAR: `publications.europa.eu/webapi/rdf/sparql` (POST with `query` parameter)

---

## 🤝 Contributing

Before making changes:
1. Read the relevant section of TECHNICAL_DOCS.md
2. Run existing tests to understand the expected behavior
3. Add tests for your changes
4. Ensure all tests pass: `pytest tests/ -v`
5. Run `streamlit run app.py` to test interactively

---

**Welcome aboard!** Start with Step 1 of the Guided Tour and don't hesitate to ask questions as you explore the codebase. The architecture is designed to be modular — each layer is responsible for one concern, and edges flow cleanly downward.

Happy coding! 🚀
