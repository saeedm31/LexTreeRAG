# LexTreeRAG — Technical Documentation
> Vectorless Legal RAG via Article Trees

## Overview

**LexTreeRAG** is a vectorless Retrieval-Augmented Generation system
for querying EU legal documents published on [EUR-Lex](https://eur-lex.europa.eu).
It requires no vector database or pre-built embeddings. Instead, it uses Claude LLMs
to navigate structured article trees in real time.

Users can query live EUR-Lex documents, upload their own PDF/text files as additional
sources, or restrict answers to uploaded files only — all through the same pipeline.
Downloaded PDFs and built article trees are persisted locally so subsequent queries
never re-download or re-process the same document.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.9 |
| Web UI | Streamlit |
| LLM (fast tasks) | Claude Haiku 4.5 (`claude-haiku-4-5`) |
| LLM (final answer) | Claude Opus 4.6 (`claude-opus-4-6`) with adaptive thinking |
| LLM (optional layer) | Google Gemini (any available tier) + Google Search grounding |
| Legal data source | EUR-Lex CELLAR SPARQL endpoint (free, no auth) |
| PDF extraction | pypdf |
| HTML parsing | BeautifulSoup4 + lxml |
| HTML → Markdown | html2text |
| HTTP | requests |
| CLI formatting | rich |
| Config | python-dotenv |

---

## Project Structure

```
lextreerag/
├── main.py                     # CLI entry point (REPL + single-question mode)
├── app.py                      # Streamlit web UI
├── requirements.txt
├── .env.example
├── pipeline/
│   ├── rag_engine.py           # Full pipeline orchestrator (CLI version)
│   ├── keyword_extractor.py    # Step 1 — LLM keyword + year extraction
│   ├── eurolex_client.py       # Step 2 — SPARQL search + HTML/PDF fetch + archive integration
│   ├── tree_builder.py         # Step 3 — Article tree builder + EUR-Lex JSON cache
│   ├── tree_navigator.py       # Step 4 — LLM tree navigation with sub-node support
│   ├── doc_ingester.py         # Upload handler — PDF/TXT → tree + disk cache
│   ├── pdf_archive.py          # Local PDF archive — persist/load raw PDFs + inbox scanner
│   ├── query_classifier.py     # Question classifier + complex query decomposition
│   ├── answer_verifier.py      # Post-generation confidence scorer (coverage + grounding)
│   ├── clause_ranker.py        # Python-side obligation/list signal scoring
│   └── gemini_search.py        # Optional — Gemini hybrid answer layer
└── data/
    ├── cache/
    │   ├── {year}/
    │   │   └── {CELEX}.json    # EUR-Lex article trees (per document)
    │   └── uploads/
    │       └── {filename}.json # Uploaded file trees (persistent archive)
    └── pdfs/
        ├── {year}/
        │   └── {CELEX}.pdf     # Raw PDFs downloaded from CELLAR (persistent archive)
        └── inbox/              # Drop PDFs here — auto-indexed on next app start
```

---

## Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│                          User Interface                              │
│          main.py (CLI / REPL)   │   app.py (Streamlit Web UI)       │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │
                     ┌────────────▼────────────────┐
                     │  Pre-processing (web UI)    │
                     │  query_classifier (Haiku)   │
                     │  → type + sub-questions     │
                     └────────────┬────────────────┘
                                  │
                     ┌────────────▼────────────┐
                     │   Source Mode (web UI)  │
                     │  EUR-Lex only           │
                     │  EUR-Lex + uploads      │
                     │  Uploads only           │
                     └──────────┬──────────────┘
              ┌─────────────────┼──────────────────┐
              │                 │                  │
              ▼                 ▼                  ▼
   ┌─────────────────┐  ┌─────────────┐  ┌──────────────────────┐
   │  EUR-Lex path   │  │ Upload path │  │  Navigate & Answer   │
   │                 │  │             │  │                      │
   │ 1. Keywords     │  │ doc_ingester│  │ tree_navigator       │
   │    (Haiku)      │  │  PDF/TXT→   │  │ (Haiku, scores 0-10 │
   │ 2. SPARQL search│  │  tree+cache │  │  filters score < 4, │
   │    year-specific│  │             │  │  uses memory nodes) │
   │  + year-agnostic│  │ data/cache/ │  │                      │
   │ 3. PDF archive  │  │  uploads/   │  │ + citation links     │
   │    check first; │  │  *.json     │  │   (follow_citation   │
   │    download+save│  └─────────────┘  │    _links, re-nav)  │
   │    if missing   │                   └──────────┬───────────┘
   │ 4. tree_builder │                              │
   │    → JSON cache │                             ▼
   └─────────────────┘             ┌──────────────────────────┐
                                   │  Context Assembly        │
                                   │  + Claude Opus 4.6       │
                                   │  (adaptive thinking)     │
                                   └──────────┬───────────────┘
                                              │
                                             ▼
                                   ┌──────────────────────┐
                                   │  answer_verifier     │
                                   │  Coverage + Grounding│
                                   │  missing_aspects     │
                                   │  suggested_keywords  │
                                   └──────────┬───────────┘
                                              │
                              ┌───────────────┴──────────────┐
                              │  score < 50 OR coverage < 40 │
                              │  → silent retry (up to 3x)   │
                              │  Attempt 1: obligation expand │
                              │  Attempt 2: broad search     │
                              └───────────────┬──────────────┘
                                              │ still low after 3 attempts
                                             ▼
                                   ┌──────────────────────┐
                                   │  Keyword Discovery   │
                                   │  UI: missing aspects │
                                   │  keyword hit stats   │
                                   │  suggested terms     │
                                   │  re-run button       │
                                   └──────────────────────┘
```

---

## Pipeline — Step by Step

### Step 1 · Keyword Extraction (`keyword_extractor.py`)

**What it does:** Sends the raw user question to Claude Haiku and receives back:
- `keywords` — 4-6 short title-friendly terms (1-2 words each) to query EUR-Lex
- `year` — integer year detected in the question text (optional, used as search hint)
- `domain` — short label used for logging
- `query_variants` — synonym/obligation-expanded keyword variants for multi-pass search
- `exact_signals` — regulation numbers or article numbers explicitly cited in the question

**Why Haiku?** Simple classification task — cheap and fast, no reasoning depth needed.

**Fallback:** If the LLM returns malformed JSON, a regex extracts the year and the
question words become the keyword list.

> Skipped in **Uploads only** mode (keyword extraction still runs for tree navigation quality).

---

### Step 1b · Query Classification (`query_classifier.py`)  *(web UI only)*

**What it does:** Before searching, Claude Haiku classifies the question into one of six types and — for complex questions — decomposes it into focused sub-questions.

| Type | Example | Effect |
|---|---|---|
| `definitional` | "What is a 'data controller'?" | Standard single retrieval |
| `compliance` | "Do we need to register under X?" | Standard retrieval |
| `comparison` | "How does Reg A differ from Reg B?" | Standard retrieval |
| `timeline` | "What changed in 2022?" | Standard retrieval |
| `complex` | Multi-part question | Decomposed into 2-4 sub-questions; navigator runs once per sub-question, results merged |
| `general` | Catch-all | Standard retrieval |

**For complex questions:** `navigate_tree` is called once per sub-question against the same tree set, selected nodes are merged and de-duplicated (capped at 12), then passed as a unified context to Claude Opus.

---

### Step 2 · EUR-Lex Search (`eurolex_client.py`)

**What it does:** Queries the EU Publications Office's **CELLAR SPARQL endpoint**
(`publications.europa.eu/webapi/rdf/sparql`) using the CDM ontology.

> **Note:** EUR-Lex's web search interface (`search.html`) is blocked by AWS WAF (returns a JavaScript challenge). All search uses SPARQL only, which is a stable machine-to-machine API.

**SPARQL title-only search** — SPARQL searches document titles (not full text). This is fast and reliable but requires the right keywords to appear in regulation titles.

**Keyword expansion** (`_expand_keywords`): Multi-word phrases are split + EU-domain synonyms are injected so lay terms map to official EU regulation titles. Plural forms are normalised before synonym lookup (`motorcycles` → check `motorcycle`):

| User keyword | EU synonyms added |
|---|---|
| `motorcycle` / `motorcycles` | `three-wheel`, `quadricycle`, `moped`, `motor vehicle`, `l-category` |
| `car` | `passenger car`, `light-duty vehicle` |
| `truck` | `heavy-duty vehicle`, `heavy goods vehicle` |
| `drone` | `unmanned aircraft`, `UAS`, `RPAS` |
| `ai` | `artificial intelligence`, `automated decision` |

The `motor vehicle` synonym ensures that broad vehicle-safety regulations such as Regulation (EU) 2019/2144 (which covers cybersecurity via UN R155) are found even when their titles say "motor vehicles" rather than "motorcycle".

**Noisy terms** (`compliance`, `regulations`, `emissions`, `requirements`, `market`, `security`, `approval`, etc.) are stripped from the specific-keyword passes because they appear in thousands of unrelated document titles.

**4-pass keyword search strategy** (within each year search):

| Pass | Logic | Keywords used | Runs when |
|---|---|---|---|
| 0 | AND compound | Vehicle-domain terms AND topic terms | When both groups exist; **sector 3 only** |
| 1 | OR | Specific (low-noise) terms only | Always |
| 2 | OR | All keywords including noisy legal boilerplate | Only if Pass 1 < max results |
| 3 | OR, no sector filter | Specific terms, sector filter dropped | Last resort |

**Pass 0 — compound AND search** finds regulations that match *both* a vehicle-domain term *and* a topic term in their title. This prevents the OR-search problem where generic terms like "type-approval" return many unrelated regulations before the relevant one. Pass 0 restricts to **sector 3** (original legislation) to avoid consolidated snapshots (sector 0) which often have no PDF/HTML in CELLAR.

**Sector re-ranking bonus:** Results from sector 3 (original legislation) receive a +0.5 score bonus over sector 0 (consolidated) to ensure they rank above snapshots that share the same CELEX base number.

**Client-side score re-ranking:** After all passes, results are scored by the number of specific keywords present in the document title, then sorted descending. Documents matching more specific keywords rank first.

**CELEX quality filters applied to every SPARQL query:**
- Year ≥ 2010: pre-2010 documents rarely have HTML in CELLAR
- Merger/competition decisions (type `M`) excluded
- Consolidated duplicates: when multiple snapshots of the same regulation exist, only the newest is kept

**Year resolution priority:** sidebar year → LLM-detected year from question → years from explicit act citations (e.g. "Regulation 2018/858").

**Citation extraction** (`extract_cited_regulations`): Regex parses both citation styles (`YEAR/NUMBER` and `NUMBER/YEAR`) and resolves them directly to CELEX IDs via `owl:sameAs` SPARQL lookups.

**Parallel document fetching** (`fetch_documents_parallel`): Uses `ThreadPoolExecutor` (max 3 workers) to fetch HTML/PDF concurrently.

**Document fetching per doc (archive-first):**
1. **Archive check** (`pdf_archive.is_archived`): If a raw PDF is already in `data/pdfs/{year}/{celex}.pdf`, load it directly (no network call), extract text with pypdf.
2. **Network fetch**: If not archived, query SPARQL for the correct expression slot (`_get_expression_slot`), try PDF first then HTML.
3. **Archive save**: On a successful PDF download, save raw bytes to the archive via `pdf_archive.save_pdf_bytes` before returning extracted text.
4. Converts HTML → Markdown via BeautifulSoup4 + html2text; HTML capped at **600 KB**.

**Iterative citation retrieval** (`follow_citation_links`): After navigation, auto-fetches up to 3 linked laws referenced within selected articles.

**Rate limiting:** `REQUEST_DELAY = 0.3s` per HTTP fetch; `SEARCH_DELAY = 0.5s` per search query.

> Entire step skipped in **Uploads only** mode.

---

### Step 2b · PDF Archive (`pdf_archive.py`)

**What it does:** Provides a persistent local cache for raw PDF bytes so documents are
never re-downloaded once seen.

**Archive layout:**
```
data/pdfs/
  {year}/
    {celex}.pdf     ← one file per EUR-Lex document
  inbox/            ← user-placed PDFs, auto-scanned on startup
```

**Key functions:**

| Function | Purpose |
|---|---|
| `archive_path(celex)` | Returns `data/pdfs/{year}/{celex}.pdf`, creates directory |
| `is_archived(celex)` | Fast existence check (no I/O beyond stat) |
| `load_pdf_bytes(celex)` | Read raw bytes from archive; returns `None` if absent |
| `save_pdf_bytes(celex, data)` | Write raw bytes to archive |
| `list_archived()` | Walk archive tree, return `[{celex, year, path, size_kb}]` |
| `scan_inbox(cache_dir, client)` | Process all PDFs in the inbox (see below) |

**Inbox scanner (`scan_inbox`):**  
At app startup, every `.pdf` file in `data/pdfs/inbox/` is processed:
- **CELEX detected** (e.g. `32019R2144.pdf`, `EU_32019R2144_clean.pdf`): extract text with pypdf, run `normalize_pdf_text`, build article tree with `build_tree`, save JSON to cache, move PDF to `data/pdfs/{year}/{celex}.pdf`. Status: `"indexed"`.
- **CELEX already cached**: skip without moving. Status: `"skipped"`.
- **No CELEX in filename**: delegate to `doc_ingester.ingest_uploaded_file` (treated as a generic upload). Status: `"upload"`.

If any PDFs are indexed, a success banner is shown in the sidebar.

---

### Step 3a · EUR-Lex Tree Builder (`tree_builder.py`)

**What it does:** Converts a Markdown document into a structured **Article Tree** and
persists it as a JSON file in `data/cache/{year}/{celex}.json`.

**PDF text normalisation (`normalize_pdf_text`):**  
pypdf often produces inter-character spaces (`Ar ticle 5`, `A r t i c l e  5`) and
hyphenated line breaks (`regu-\nlation`). Before parsing, the text is normalised:
- `A\s*r\s*t\s*i\s*c\s*l\s*e` → `Article` (handles all spacing variants)
- `A\s*N\s*N\s*E\s*X` → `ANNEX`
- `(\w)-\n(\w)` → join word (remove hyphenated line break)
- Double-spaces in article header lines collapsed

**Parsing:**  
Article headers are matched with an improved regex that tolerates Markdown heading prefixes
and captures inline article titles:
```
^(?:##*\s*)?(?:ARTICLE|Article)\s+(\d+\w*)(?:\s+(?![-–])(.+))?\s*$
```

**Sub-chunking long articles:**  
Articles longer than **5 000 chars** are split into sub-nodes (~3 000 chars each):
- **Parent node**: `is_parent=True`, `sub_count=N`, text = first paragraph preview only
- **Sub-nodes**: `id = art_5_1`, `number = 5.1`, `parent_id = art_5`
- If only one chunk results after splitting, a single flat node is returned (no split)

This prevents the slim tree from omitting content from long articles when the navigator selects only the parent.

**Paragraph fallback chunker (`_paragraph_fallback_chunks`):**  
When no `Article N` headers are found (e.g. preamble-heavy documents), instead of
returning a single truncated `doc_body` node, the document is chunked into:
- A `preamble` node (recitals, up to `HAS ADOPTED THIS REGULATION` boundary)
- Numbered `chunk_N` nodes at ~3 000-char paragraph boundaries

This ensures the navigator always has multiple navigable sections even for unstructured documents.

**Summarisation:** Claude Haiku generates a 1-sentence (≤25-word) summary per node,
in batches of 12.

**Cache hit:** If `data/cache/{year}/{celex}.json` already exists it is loaded directly —
no re-fetch, no LLM calls.

**Cache format:**
```json
{
  "doc_id": "32016R0679",
  "title":  "General Data Protection Regulation",
  "year":   2016,
  "date":   "2016-04-27",
  "url":    "https://eur-lex.europa.eu/...",
  "indexed_at": "2024-01-01T12:00:00+00:00",
  "nodes": [
    {
      "id": "art_5", "number": "5", "title": "Principles",
      "summary": "...", "text": "...",
      "is_parent": true, "sub_count": 3,
      "obligation_score": 3, "list_score": 2
    },
    {
      "id": "art_5_1", "number": "5.1", "title": "Principles (part 1/3)",
      "summary": "...", "text": "...",
      "parent_id": "art_5",
      "obligation_score": 2, "list_score": 1
    }
  ]
}
```

---

### Step 3b · Document Ingester (`doc_ingester.py`)  *(web UI only)*

**What it does:** Handles user-uploaded PDF, TXT, and MD files — converts them into the
same reasoning tree format as Step 3a, and saves them to `data/cache/uploads/{filename}.json`.

**Splitting strategy:**
1. Try `Article N` heading split (same regex as `tree_builder.py`)
2. If no article structure found, fall back to **paragraph-aware chunking** (~2 000 chars
   per chunk), so the navigator still has meaningful sections to reason over

**Persistence:**
- Trees are saved to `data/cache/uploads/{safe_filename}.json`
- **Auto-loaded on every session start** — uploaded files survive browser refreshes and
  server restarts without re-uploading
- Re-uploading the same filename returns the cached tree instantly (no re-processing)

---

### Step 4 · Tree Navigator (`tree_navigator.py`)

**What it does:** Claude Haiku receives only the **slim tree** — article IDs, numbers,
and 1-sentence summaries — for all documents. It returns the IDs and relevance scores
of articles most likely to answer the question.

**Why no vectors:** The slim tree mimics a table of contents. The LLM reasons about
relevance like a lawyer scanning a regulation index — no cosine similarity needed.

**Sub-node display in slim tree:**  
Parent nodes (from sub-chunked articles) are shown with a `(N parts)` label, and their
sub-nodes are rendered indented beneath them. Sub-nodes display their part number (e.g.
`Article 5.1`, `Article 5.2`) so the navigator can select specific parts of long articles.
Top-level rendering skips sub-nodes to avoid duplication — they only appear indented.

**Parent auto-expansion:**  
When the navigator selects a parent node (`is_parent=True`), all its sub-nodes are
automatically included in the enriched result. This ensures the full text of a long
split article is passed to the answer LLM, not just the preview in the parent node.

**Relevance scoring:** The navigator returns a `score` (0–10) per selected article.
Articles scoring below 4 are filtered out before passing to the answer step.

**Conversation memory:** Accepts an optional `context_nodes` list — the selected
articles from the previous question. These appear under a `PREVIOUS CONTEXT` header
in the slim tree prompt so the navigator can reuse them for follow-up questions
without re-fetching.

**Output:** Up to 8 article nodes enriched with full metadata (doc_id, title, year,
URL, full text, relevance_score) passed to the answer step.

---

### Step 5 · Context Assembly

Selected nodes are formatted as a numbered context block:

```
[1] GDPR (2016) — Article 5
CELEX: 32016R0679  |  URL: https://...

1. Personal data shall be...

---

[2] myreport.pdf — Section 3
...
```

EUR-Lex articles and uploaded document sections appear in the same context block —
the model treats them identically.

---

### Step 6 · Answer Generation

**Model:** Claude Opus 4.6 with `thinking: {"type": "adaptive"}`.

**Two output modes** (toggled from the sidebar):

#### Prose mode (default)
Streaming token-by-token. System prompt enforces:
- Answer only from provided articles/sections
- Cite specific Article number and source name for every claim
- Distinguish obligations, rights, definitions, and exceptions
- End with a `## References` section

#### Structured JSON mode
Non-streaming. System prompt instructs Claude to return a single JSON object rendered
as categorised Streamlit components:

| Section | Content |
|---|---|
| **Summary** | 2-3 sentence plain-English overview |
| **Obligations** | Legal requirements ("shall", "must") |
| **Rights** | Entitlements granted to parties |
| **Definitions** | Term → definition from article text |
| **Exceptions** | Derogations and carve-outs |
| **Procedure** | Ordered steps (if applicable) |
| **References** | CELEX · Article · verbatim quote · relevance explanation |
| **Caveats** | Gaps or limitations in retrieved articles |

Only non-empty sections are rendered. Useful for programmatic consumption or detailed legal analysis.

---

### Step 7 · Confidence Scoring (`answer_verifier.py`)  *(web UI only)*

**What it does:** After the answer is generated, a Claude Haiku call scores the answer
on two dimensions and extracts search guidance for low-quality results:

| Field | Type | Purpose |
|---|---|---|
| `coverage` | 0–100 | How well the retrieved articles collectively address all aspects of the question |
| `grounding` | 0–100 | How well the answer's claims are traceable to the provided article texts |
| `score` | 0–100 | `0.4 × coverage + 0.6 × grounding` |
| `level` | str | `"high"` / `"medium"` / `"low"` |
| `reasoning` | str | 1-2 sentence plain-English explanation |
| `missing_aspects` | list[str] | Aspects of the question not covered (empty if coverage ≥ 70) |
| `suggested_keywords` | list[str] | Precise EU legal terms to search for the missing content |

**Confidence level thresholds:**

| Level | Score |
|---|---|
| 🟢 High | ≥ 75 |
| 🟡 Medium | 50–74 |
| 🔴 Low | < 50 |

Displayed as three metrics below the answer with a plain-English explanation.

> **Coverage gate:** The retry loop triggers on `score < 50` **OR** `coverage < 40`,
> preventing cases where high grounding (an answer correctly saying "not in retrieved
> articles") inflates the composite score despite near-zero coverage.

---

### Step 8 · Confidence-Gated Retry Loop  *(web UI, prose mode only)*

**What it does:** After the initial answer is scored, if confidence is low the pipeline
automatically runs up to 2 silent retries without interrupting the user.

**Trigger condition:** `score < 50` OR `coverage < 40`

| Attempt | Strategy | Changes from previous |
|---|---|---|
| 0 | Normal search | Streamed live to user |
| 1 | Obligation expansion | `obligation_expansion_keywords` expands keywords with legal obligation terms; re-search + re-navigate (non-streaming) |
| 2 | Broad search | Year filter dropped, sectors expanded to `("3", "0", "4")`, max laws +3 (non-streaming) |

The best answer found across all attempts (by score) replaces the displayed answer.
If the best answer comes from a retry, the answer area is updated silently.
Attempts 1 and 2 do not stream — the user sees a `"🔄 Searching further…"` spinner.

---

### Step 9 · Keyword Discovery UI  *(web UI, prose mode only)*

**When it appears:** After all 3 attempts, if `score < 50` OR `coverage < 40` still holds.

**Sections:**

1. **Missing aspects** — list of specific aspects of the question not found in any retrieved article (from `answer_verifier.missing_aspects`)

2. **Keyword hit stats table** — for each keyword used, shows how many article nodes and how many documents contain it:

   | Keyword | Articles | Docs | Coverage % |
   |---|---|---|---|
   | cybersecurity | 3 | 2 | 40% |
   | type-approval | 12 | 4 | 100% |

3. **Suggested terms** — multiselect populated from `answer_verifier.suggested_keywords`

4. **Free-text input** — user can type additional comma-separated keywords

5. **Re-run button** — combines selected + typed keywords, injects them as `focus_keywords` into the next search via `st.session_state.focus_rerun_kws`, and triggers `st.rerun()`

The best-effort answer is always shown below the discovery section.

---

### Optional · Gemini Hybrid Layer (`gemini_search.py`)

When `GEMINI_API_KEY` is set, the web UI shows a second answer panel powered by
**Gemini + Google Search grounding** alongside the primary Claude answer.

**Model selection:** Detected dynamically at key-validation time (preference order:
`gemini-2.0-flash-lite` → `gemini-2.0-flash` → `gemini-1.5-flash-8b` → …).

**Rate limit handling:** Auto-waits and retries once if a 429 is returned and the
suggested wait is ≤ 90 seconds.

---

## Interfaces

### CLI (`main.py`)

```bash
# Interactive REPL
python main.py

# Single question
python main.py --question "What are GDPR obligations for data controllers in 2018?"

# With options
python main.py -q "..." --cache-dir ./data/cache --max-laws 10 --save-report
```

| Flag | Default | Description |
|---|---|---|
| `--question` / `-q` | interactive | Legal question |
| `--cache-dir` | `./data/cache` | JSON tree cache directory |
| `--max-laws` | 5 | Max EUR-Lex laws fetched |
| `--save-report` | false | Save answer + refs to JSON |

### Web UI (`app.py`)

```bash
conda activate rag_app
streamlit run app.py
# Opens LexTreeRAG at http://localhost:8501
```

**Sidebar controls:**

| Control | Description |
|---|---|
| Anthropic API Key | Required — validated on entry |
| Gemini API Key | Optional — enables hybrid mode |
| Year filter (checkbox) | Off = auto-detect year from question. On = specify target year + ±delta range |
| Max laws slider | Total documents fetched (1–15) |
| CELEX sector checkboxes | Filter by document type (Legislation, Case-law, Treaties…) |
| Output format toggle | Off = streaming prose. On = structured JSON sections |
| Answer source radio | `EUR-Lex only` / `EUR-Lex + uploads` / `Uploads only` |
| File uploader | PDF, TXT, MD — parsed once, saved to disk archive |

**Tabs:**
- **Ask EUR-Lex** — chat interface; shows answer + confidence score (Coverage %, Grounding %, overall %); keyword analysis expander + focus re-run; keyword discovery UI on low confidence
- **References** — reference cards with links + JSON download
- **Cache Manager** — EUR-Lex tree browser + Uploaded files archive (per-file delete) + PDF archive viewer (inbox path, archived PDFs list)

**Startup behaviour:**
1. Load all upload trees from `data/cache/uploads/` into session state
2. Scan `data/pdfs/inbox/` for unprocessed PDFs → auto-index detected documents

> **UI note (Python 3.9):** `st.dataframe` / `st.data_editor` crash due to a numpy 1.21 ↔ pandas 1.3 incompatibility. All tabular data is rendered as plain HTML via `st.markdown(..., unsafe_allow_html=True)` to work around this.

---

## Source Modes (Web UI)

| Mode | EUR-Lex search | Uploaded files | Use case |
|---|---|---|---|
| **EUR-Lex only** | Yes | Ignored | Default — live legal research |
| **EUR-Lex + uploads** | Yes | Merged | Supplement EU law with your own docs |
| **Uploads only** | No (no network call) | Only source | Answer from private/internal documents |

---

## Configuration (`.env`)

```bash
# Required
ANTHROPIC_API_KEY=sk-ant-...

# Optional — enables Gemini hybrid mode (free tier at aistudio.google.com)
GEMINI_API_KEY=

# Optional overrides
CACHE_DIR=./data/cache            # JSON tree cache
PDF_ARCHIVE_DIR=./data/pdfs       # Raw PDF storage
MAX_LAWS_PER_YEAR=5
```

---

## Key Design Decisions

### Why "Vectorless"?

Traditional RAG systems embed text into vectors and retrieve by cosine similarity.
This project avoids that entirely:
- No embedding model to run or pay for
- No vector database to set up or maintain
- LLM navigation produces **article-level precision** — the model understands legal
  structure, not just semantic proximity
- Works on live EUR-Lex data without pre-indexing the corpus
- Same approach works unchanged for uploaded user documents

### Two-Model Strategy

| Task | Model | Reason |
|---|---|---|
| Keyword extraction | Haiku | Simple classification, cheap |
| Question classification | Haiku | Simple routing decision, cheap |
| Article summarisation | Haiku | Repetitive batch task, cheap |
| Tree navigation (with scoring) | Haiku | Low-complexity selection + scoring |
| Confidence scoring | Haiku | Post-hoc evaluation, cheap |
| Final answer (prose or structured) | Opus 4.6 + adaptive thinking | Requires legal reasoning depth |

### Search Strategy

The pipeline runs **two year-scope passes** per query:
1. **Year-specific** — using any known year (sidebar, question, citations)
2. **Year-agnostic** — no year filter, catches laws from unexpected registration years

Within each pass, a **4-tier keyword strategy** maximises precision:
- Pass 0: AND compound on sector 3 only — finds cross-domain regulations (e.g. cybersecurity for motor vehicles) while avoiding consolidated snapshots with no PDF content
- Pass 1: specific terms only (low noise)
- Pass 2: all keywords including boilerplate — only if Pass 1 found fewer than max results
- Pass 3: same as Pass 2, sector filter dropped — last resort

This means a question like *"What does GDPR say about consent in 2023?"* will find GDPR (registered 2016) even though the year filter for 2023 would miss it. A query like *"cybersecurity regulations for motorcycles"* finds Regulation 2019/2144 (General Safety Regulation, motor vehicle cybersecurity via UN R155) rather than unrelated telecom-market or aviation-security documents.

### Caching Strategy

| Cache location | Content | Invalidated by |
|---|---|---|
| `data/cache/{year}/{celex}.json` | EUR-Lex article trees | Manual delete (Cache Manager) |
| `data/cache/uploads/{file}.json` | Uploaded file trees | Manual delete (Cache Manager or sidebar) |
| `data/pdfs/{year}/{celex}.pdf` | Raw PDF bytes from CELLAR | Manual delete |
| `data/pdfs/inbox/` | Unprocessed PDFs awaiting indexing | Moved to archive after indexing |

The JSON tree cache and the PDF archive are independent — clearing the JSON cache forces article trees to be rebuilt (useful after tree_builder improvements) while the PDFs remain so no re-download is needed.

### Confidence-Gated Retrieval

A single retrieval attempt often fails for niche cross-domain questions (e.g. cybersecurity + motorcycle). The 3-attempt loop improves coverage without burdening the user:
- **Attempt 0** streams immediately so the user sees a response without waiting for retries
- **Attempt 1** (obligation expansion) broadens to legal obligation vocabulary — often finds enforcement articles missed by title-only search
- **Attempt 2** (broad search) drops year and sector constraints — catches laws registered under unexpected years or sectors

The composite confidence score uses `0.4 × coverage + 0.6 × grounding`. To prevent a degenerate case where grounding is high (answer correctly says "nothing found") while coverage is 5%, the retry gate also fires when `coverage < 40` regardless of composite score.

---

## Data Flow Summary

```
User question
    │
    ▼ query_classifier (Haiku) — type + sub-questions if complex
    │
    ├─[Uploads only]──────────────────────────────────────────────────┐
    │                                                                 │
    ▼ keyword_extractor (Haiku)                                       │
keywords, year, exact_signals, query_variants                         │
    │                                                                 │
    ▼ CELLAR SPARQL — Pass 0 (AND, sector 3) + Pass 1-3 (OR)         │
docs = [{celex, title, date, url, uuid}, ...]                         │
    │                                                                 │
    ▼ fetch_documents_parallel (ThreadPoolExecutor, 3 workers)        │
    │   Step 1: pdf_archive.is_archived → load if cached             │
    │   Step 2: download PDF/HTML + save to archive                   │
(doc, markdown) pairs                                                 │
    │                                                                 │
    ▼ tree_builder (normalize → parse → sub-chunk → Haiku summaries) │
    │   → data/cache/{year}/{celex}.json                              │
EUR-Lex trees                                                         │
    │                                                                 │
    ├─[EUR-Lex + uploads]──────────────────────────────────┐          │
    │                                          merge        │          │
    │                        data/cache/uploads/*.json ────┘          │
    │                        (auto-loaded from disk)  ◄───────────────┘
    ▼
all_trees (EUR-Lex + uploads)
    │
    ▼ navigate_tree (Haiku, scores 0-10, filters <4, uses memory)
    │   parent selected → sub-nodes auto-included
    │   → for complex questions: one navigate call per sub-question → merge
selected_nodes = [{doc_id, node_id, relevance_score, text, ...}, ...]
    │
    ▼ follow_citation_links → fetch up to 3 linked docs → re-navigate
selected_nodes (expanded)
    │
    ▼ Context assembly
context = "[1] GDPR (2016) — Article 5 \n\n ... \n\n [2] ..."
    │
    ├─[Prose mode]────────────────────────────────────────────────────┐
    │  Claude Opus 4.6 (adaptive thinking, streaming, Attempt 0)      │
    │  → markdown answer                                              │
    │                                                                 │
    ├─[Structured mode]───────────────────────────────────────────────┤
    │  Claude Opus 4.6 (adaptive thinking, non-streaming)             │
    │  → JSON → Summary / Obligations / Rights / etc.                 │
    │                                                                 │
    ▼ (prose mode)                                                    │
answer_verifier (Haiku) ◄─────────────────────────────────────────────┘
  → coverage %, grounding %, score, missing_aspects, suggested_keywords
    │
    ├─[score ≥ 50 AND coverage ≥ 40]──────────────────────────────────┐
    │  Accept answer, show metrics                                     │
    │                                                                 ▼
    ├─[low confidence → Attempt 1] obligation_expand keywords         │
    │  run_query → navigate → answer_prose (non-streaming) → verify  │
    │                                                                  │
    ├─[still low → Attempt 2] drop year + expand sectors              │
    │  run_query → navigate → answer_prose (non-streaming) → verify  │
    │                                                                  │
    ▼ best answer shown                                               │
    │                                                                 │
    ├─[best still low confidence]                                      │
    │  keyword discovery UI: missing aspects, hit stats, suggested    │
    │  terms, re-run button                                            │
    │                                                                 │
    ▼ 📊 Keyword Analysis expander (all paths)                        │
  keyword hit stats table, focus re-run control ◄─────────────────────┘
```

---

## External Dependencies

| Service | URL | Auth |
|---|---|---|
| CELLAR SPARQL | `publications.europa.eu/webapi/rdf/sparql` | None (public) |
| CELLAR content | `publications.europa.eu/resource/cellar/...` | None (public) |
| EUR-Lex portal | `eur-lex.europa.eu` | None (public) |
| Anthropic API | `api.anthropic.com` | `ANTHROPIC_API_KEY` |
| Google Generative AI | `generativelanguage.googleapis.com` | `GEMINI_API_KEY` (optional) |
