# LexTreeRAG

!!! tip "What is this?"
    LexTreeRAG is a **vectorless legal RAG** system that answers questions about EU law by searching the live EUR-Lex CELLAR database — without needing a vector database or pre-embedded corpus.

## How it works

```
User question
  │
  ├─ 1. Classify question ──────── Claude Haiku
  ├─ 2. Extract keywords + year ── Claude Haiku
  ├─ 3. Search EUR-Lex SPARQL ──── publications.europa.eu
  ├─ 4. Fetch documents ────────── HTML → Markdown  /  PDF → text
  ├─ 5. Build reasoning trees ──── parse Articles → summarise → cache JSON
  ├─ 6. Navigate trees ─────────── Claude Haiku picks relevant articles
  ├─ 7. Python reranking ───────── obligation score + list score
  ├─ 8. Generate answer ────────── Claude Opus 4.6 (streaming)
  └─ 9. Confidence scoring ──────── Claude Haiku → retry if < 50%
```

## Architecture

**Pattern B — Unified Streamlit.** There is no separate frontend and backend.

| Layer | File | Role |
|---|---|---|
| UI + Orchestration | `app.py` | Streamlit interface, calls pipeline modules directly |
| CLI | `main.py` | Terminal entry point, same pipeline |
| Pipeline | `pipeline/*.py` | Keyword extraction, EUR-Lex search, tree building, navigation, ranking, answer generation |
| Cache | `data/cache/` | JSON reasoning trees per document |
| PDF Archive | `data/pdfs/` | Raw PDFs downloaded from CELLAR |

## External dependencies

| Service | Purpose |
|---|---|
| Anthropic API — Claude Haiku 4.5 | Keyword extraction, question classification, article summarisation, tree navigation, confidence scoring |
| Anthropic API — Claude Opus 4.6 | Final answer generation with adaptive thinking |
| EUR-Lex CELLAR SPARQL | Live document discovery (`publications.europa.eu/webapi/rdf/sparql`) |
| EUR-Lex CELLAR content | HTML and PDF document fetch |
| Google Gemini *(optional)* | Hybrid second-opinion answer with Google Search grounding |

## Pages

- [Chat — Ask EUR-Lex](chat.md) — main chat interface
- [References Tab](references.md) — article reference cards
- [Cache Manager](cache-manager.md) — browse and manage cached trees
- [Pipeline Architecture](pipeline.md) — full technical reference
