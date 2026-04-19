# ⚖️ LexTreeRAG

> **Vectorless Legal RAG via Article Trees**

[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue?logo=python)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/UI-Streamlit-FF4B4B?logo=streamlit)](https://streamlit.io/)
[![Claude Opus 4.6](https://img.shields.io/badge/LLM-Claude%20Opus%204.6-blueviolet)](https://www.anthropic.com/)
[![EUR-Lex](https://img.shields.io/badge/Data-EUR--Lex%20CELLAR-003399)](https://eur-lex.europa.eu/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

LexTreeRAG answers legal questions about EU law by reasoning over live EUR-Lex documents — **without any vector database or embeddings**. It downloads regulations as PDFs, parses them into structured Article Trees, and uses Claude to navigate the tree and generate grounded answers. A confidence-gated retry loop and keyword discovery UI handle hard cross-domain queries automatically.

---

## Architecture

```
User Question
      │
      ▼
┌──────────────────────────────────────────┐
│  1. Keyword Extraction      (Haiku)      │
│  2. SPARQL Search  →  EUR-Lex CELLAR     │
│     Compound AND · synonym expansion     │
│  3. PDF Archive  (local, reused forever) │
│  4. Article Tree Builder    (Haiku)      │
│     normalize → parse → sub-chunk       │
└────────────────────┬─────────────────────┘
                     │
          ┌──────────▼───────────┐
          │   5. Tree Navigator  │  ← no vectors
          │   slim tree (Haiku)  │    LLM picks articles
          │   scores 0–10        │    by relevance
          └──────────┬───────────┘
                     │
          ┌──────────▼───────────────────────┐
          │  6. Answer Generation            │
          │     Claude Opus 4.6 (streaming)  │
          └──────────┬───────────────────────┘
                     │
          ┌──────────▼───────────────────────┐
          │  7. Confidence Gate   (Haiku)    │
          │     score < 50 OR coverage < 40  │
          │     → silent retry ×2            │
          │     → Keyword Discovery UI       │
          └──────────────────────────────────┘
```

**Why no vectors?**  The slim tree mimics a table of contents. Claude navigates it like a lawyer scanning a regulation index — article-level precision, zero embedding overhead.

---

## Features

- **Vectorless** — no embedding model, no vector DB, no cosine similarity
- **Live EUR-Lex search** via SPARQL (CELLAR endpoint — free, no auth required)
- **Local PDF + JSON cache** — documents downloaded once, reused forever across sessions
- **PDF inbox** — drop any PDF into `data/pdfs/inbox/` and it's auto-indexed on next startup
- **Compound AND search** with EU-domain synonym expansion (e.g. *motorcycle* → *motor vehicle, l-category, quadricycle*)
- **Sub-chunking** — long articles split into navigable sub-nodes; no content truncation
- **3-attempt confidence-gated retry loop** — obligation expansion → broad search → best answer shown
- **Keyword Discovery UI** — when all retries fail, shows missing aspects, hit stats, and suggested search terms
- **Structured JSON output** mode — answer rendered as Obligations / Rights / Definitions / Exceptions / Procedure
- **Hybrid Gemini layer** — optional second answer panel powered by Google Search grounding
- **Upload your own PDFs/TXT** and query them alongside EUR-Lex documents

---

## Quick Start

```bash
git clone https://github.com/saeedm31/LexTreeRAG.git
cd LexTreeRAG

pip install -r requirements.txt

cp .env.example .env        # then add your ANTHROPIC_API_KEY

streamlit run app.py        # opens http://localhost:8501
```

> **Python 3.9+** required.

---

## Configuration

Copy `.env.example` to `.env` and fill in the values:

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ | Get one at [console.anthropic.com](https://console.anthropic.com/) |
| `GEMINI_API_KEY` | Optional | Enables hybrid Gemini + Google Search panel — free tier at [aistudio.google.com](https://aistudio.google.com/) |
| `CACHE_DIR` | Optional | Directory for JSON article trees (default `./data/cache`) |
| `PDF_ARCHIVE_DIR` | Optional | Directory for raw PDF storage (default `./data/pdfs`) |
| `MAX_LAWS_PER_YEAR` | Optional | Max documents per search pass (default `5`) |

---

## Web UI

```bash
streamlit run app.py
```

1. **Enter your API key** in the sidebar — validated on entry
2. **Ask a question** in the chat box (year auto-detected, or set it in the sidebar)
3. The pipeline searches EUR-Lex, builds article trees, navigates them, and streams the answer
4. **Confidence metrics** appear below the answer (Coverage %, Grounding %, overall score)
5. If confidence is low, **automatic retries** run silently and the best answer is shown
6. The **📊 Keyword Analysis** expander lets you focus the model on specific terms and re-run
7. If all retries fail, the **Keyword Discovery UI** shows what was missed and lets you add search terms

**Sidebar controls:**

| Control | Description |
|---|---|
| Year filter | Auto-detect from question, or pin a specific year ± range |
| Max laws | Documents to retrieve per query (1–15) |
| CELEX sectors | Filter by document type (Legislation, Case-law, Treaties…) |
| Output format | Streaming prose (default) or structured JSON sections |
| Answer source | EUR-Lex only · EUR-Lex + uploads · Uploads only |
| File uploader | PDF, TXT, MD — parsed once, saved to local archive |

**Tabs:**

- **💬 Ask EUR-Lex** — chat interface, confidence scores, keyword analysis
- **📋 References** — reference cards with EUR-Lex links + JSON download
- **🗄️ Cache Manager** — browse/delete cached trees, view PDF archive, inbox path

---

## CLI

```bash
# Interactive REPL
python main.py

# Single question
python main.py -q "What are GDPR obligations for data controllers in 2018?"

# Custom options
python main.py -q "..." --max-laws 10 --cache-dir ./data/cache --save-report
```

| Flag | Default | Description |
|---|---|---|
| `-q` / `--question` | interactive | Your legal question |
| `--cache-dir` | `./data/cache` | JSON tree cache location |
| `--max-laws` | `5` | Max EUR-Lex laws to retrieve |
| `--save-report` | off | Save answer + references to a JSON file |

---

## How It Works

1. **Keyword extraction + search** — Claude Haiku extracts precise search terms and year; SPARQL queries EUR-Lex CELLAR titles using a compound AND strategy with EU-domain synonym expansion. Sector-3 (original legislation) is prioritised over consolidated snapshots.

2. **Local archive** — raw PDFs are saved to `data/pdfs/{year}/{celex}.pdf` on first download. Subsequent queries load from disk — no network call.

3. **Article Tree Builder** — pypdf extraction artifacts (`Ar ticle 5` → `Article 5`) are normalised, then the document is split into Article nodes. Articles over 5 000 chars are sub-chunked into navigable parts. Claude Haiku writes a 1-sentence summary per node.

4. **Tree Navigation** — Claude Haiku receives only the *slim tree* (article IDs + summaries, no full text — ~3 000 tokens for a 60-article regulation). It selects the articles most relevant to the question, scored 0–10. Only nodes scoring ≥ 4 are passed forward.

5. **Answer + confidence gate** — Claude Opus 4.6 streams a grounded answer. A Haiku pass scores Coverage and Grounding (0–100). If `score < 50` or `coverage < 40`, two silent retries run automatically (obligation-expanded keywords → broad search with no year filter). The best answer wins.

---

## Project Structure

```
LexTreeRAG/
├── app.py                      # Streamlit web UI
├── main.py                     # CLI entry point
├── requirements.txt
├── .env.example
├── pipeline/
│   ├── keyword_extractor.py    # Haiku keyword + year extraction
│   ├── eurolex_client.py       # SPARQL search + CELLAR fetch
│   ├── pdf_archive.py          # Local PDF archive + inbox scanner
│   ├── tree_builder.py         # Article tree parser + sub-chunker
│   ├── tree_navigator.py       # LLM-based article selection
│   ├── answer_verifier.py      # Confidence scoring + missing aspects
│   ├── clause_ranker.py        # Obligation/list signal scoring
│   ├── doc_ingester.py         # Uploaded file → tree
│   ├── query_classifier.py     # Question type + sub-question decomposition
│   └── gemini_search.py        # Optional Gemini hybrid layer
├── data/
│   ├── cache/                  # JSON article trees (gitignored)
│   └── pdfs/
│       └── inbox/              # Drop PDFs here for auto-indexing
└── tests/
```

---

## Contributing

Pull requests are welcome. Please open an issue first to discuss any significant changes.

---

## License

MIT © 2025 Shojjat Panah
