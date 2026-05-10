# Pipeline Architecture

## Module Overview

| Module | File | Responsibility |
|---|---|---|
| Keyword Extractor | `pipeline/keyword_extractor.py` | Extract search keywords and year from question |
| EUR-Lex Client | `pipeline/eurolex_client.py` | SPARQL search, CELLAR fetch, HTML→Markdown, parallel fetch |
| Tree Builder | `pipeline/tree_builder.py` | Parse Markdown → Article nodes → Haiku summaries → JSON cache |
| Tree Navigator | `pipeline/tree_navigator.py` | Slim tree (summaries only) → Haiku picks relevant articles |
| Clause Ranker | `pipeline/clause_ranker.py` | Python reranking: obligation/list scoring, generic penalty |
| Answer Verifier | `pipeline/answer_verifier.py` | Confidence scoring — coverage + grounding |
| Query Classifier | `pipeline/query_classifier.py` | Classify question type, decompose complex questions |
| Doc Ingester | `pipeline/doc_ingester.py` | Ingest uploaded PDF/TXT/MD into reasoning tree format |
| PDF Archive | `pipeline/pdf_archive.py` | Local PDF storage, inbox scanner |
| Gemini Search | `pipeline/gemini_search.py` | Optional Google Search grounded second opinion |
| RAG Engine | `pipeline/rag_engine.py` | CLI-only simplified pipeline orchestrator |

---

## LLM Usage

| Step | Model | max_tokens | Purpose |
|---|---|---|---|
| Question classification | `claude-haiku-4-5` | 300 | Classify type, decompose complex questions |
| Keyword extraction | `claude-haiku-4-5` | 256 | Keywords, year, query variants |
| Article summarisation | `claude-haiku-4-5` | 1024 | 1-sentence summary per article (batch of 12) |
| Tree navigation | `claude-haiku-4-5` | 512 | Pick relevant articles from slim tree |
| Answer — prose | `claude-opus-4-6` | 4096 | Streaming answer with adaptive thinking |
| Answer — structured | `claude-opus-4-6` | 4096 | JSON answer with adaptive thinking |
| Confidence scoring | `claude-haiku-4-5` | 400 | Score coverage and grounding |

---

## Reasoning Tree Schema

Path: `data/cache/{year}/{celex}.json`

```json
{
  "doc_id":     "32019R2144",
  "title":      "Regulation (EU) 2019/2144 ...",
  "year":       2019,
  "date":       "2019-11-27",
  "url":        "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32019R2144",
  "indexed_at": "2024-01-15T10:30:00+00:00",
  "nodes": [
    {
      "id":               "art_1",
      "number":           "1",
      "title":            "Subject-matter and objectives",
      "summary":          "Establishes the regulation scope covering vehicle type-approval.",
      "text":             "1. This Regulation lays down ...",
      "obligation_score": 2,
      "list_score":       0
    }
  ]
}
```

### Node fields

| Field | Type | Source | Description |
|---|---|---|---|
| `id` | `str` | Parser | Internal ID e.g. `art_5`, `art_5_1`, `annex_i` |
| `number` | `str` | Parser | Article number e.g. `"5"`, `"5.1"`, `"Annex I"` |
| `title` | `str` | Parser | Article title text |
| `summary` | `str` | Claude Haiku | 1-sentence summary (max 25 words) |
| `text` | `str` | Parser | Full article body (max 6,000 chars) |
| `obligation_score` | `int` | `clause_ranker.obligation_score()` | Count of obligation keywords |
| `list_score` | `int` | `clause_ranker.list_score()` | Count of enumerated list items |
| `parent_id` | `str` | Sub-chunker | ID of parent node (sub-nodes only) |
| `is_parent` | `bool` | Sub-chunker | True for parent nodes of long articles |
| `sub_count` | `int` | Sub-chunker | Number of sub-chunks (parent nodes only) |

---

## Reranking Formula

```python
final_score = (
    base_relevance_score           # from Claude Haiku navigator (0–10)
    + min(obligation_score, 6)     # obligation keyword count, capped at 6
    + min(list_score * 2, 6)       # enumerated list count × 2, capped at 6
    + penalty                      # −4.0 if generic section (scope/definitions)
)
```

**Obligation keywords** (multi-word score 3×, single-word score 2×):
`shall`, `must`, `required to`, `shall provide`, `shall be required`, `is required`, `shall have available`, `shall ensure`, `shall submit`, `shall notify`

**Generic section titles** (trigger −4 penalty unless real obligation present):
`scope`, `definitions`, `general provisions`, `subject-matter`, `objectives`, `purpose`, `aim`, `applicability`

---

## File Structure

```
LexTreeRAG/
├── app.py                    # Streamlit UI + run_query() + answer functions
├── main.py                   # CLI entry point
├── mkdocs.yml                # This docs site config
├── .env.example
├── requirements.txt
├── pipeline/
│   ├── answer_verifier.py
│   ├── clause_ranker.py
│   ├── doc_ingester.py
│   ├── eurolex_client.py
│   ├── gemini_search.py
│   ├── keyword_extractor.py
│   ├── pdf_archive.py
│   ├── query_classifier.py
│   ├── rag_engine.py
│   ├── tree_builder.py
│   └── tree_navigator.py
├── docs/                     # This documentation
│   ├── index.md
│   ├── chat.md
│   ├── references.md
│   ├── cache-manager.md
│   └── pipeline.md
├── data/
│   ├── cache/
│   │   ├── {year}/{celex}.json
│   │   └── uploads/{file}.json
│   └── pdfs/
│       ├── inbox/
│       └── {year}/{celex}.pdf
└── .github/
    └── workflows/
        └── docs.yml          # Auto-deploy on push
```
