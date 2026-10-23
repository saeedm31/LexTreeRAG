# ⚖️ LexTreeRAG

**Ask a question about EU law. Get an answer that cites the exact articles, pulled live from EUR-Lex.**

[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue?logo=python)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/UI-Streamlit-FF4B4B?logo=streamlit)](https://streamlit.io/)
[![Claude](https://img.shields.io/badge/LLM-Claude-blueviolet)](https://www.anthropic.com/)
[![EUR-Lex](https://img.shields.io/badge/Data-EUR--Lex%20CELLAR-003399)](https://eur-lex.europa.eu/)
[![Tests](https://github.com/saeedm31/LexTreeRAG/actions/workflows/tests.yml/badge.svg)](https://github.com/saeedm31/LexTreeRAG/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

📖 **[Documentation](https://saeedm31.github.io/LexTreeRAG/)** · 🚀 **[Quick start](#-quick-start)** · 🧠 **[How it works](#-how-it-works)**

![LexTreeRAG chat page](docs/screenshots/chat-page.png)

---

## 💡 Why it exists

EU law is huge, split across thousands of regulations, and written for lawyers.
Finding *which article* answers your question can take hours.

LexTreeRAG does that search for you:

- **Finds the right laws** — searches the official EUR-Lex database live, no manual browsing
- **Reads them like a lawyer** — navigates each regulation by its articles, the way you'd use a table of contents
- **Shows its sources** — every answer links back to the exact articles it used
- **Checks itself** — scores its own answer and retries with a wider search when confidence is low

> **Example:** *"What are the obligations of data controllers under GDPR?"*
> → finds Regulation (EU) 2016/679, picks the relevant articles, and returns a grounded answer with article references.

### Why no vector database?

Most RAG systems cut documents into chunks and search them with embeddings.
Laws already have a structure — articles, paragraphs, a clear index — so LexTreeRAG uses it.
The model reads a short "table of contents" of each regulation and picks the articles it needs.
That gives article-level precision with no embedding model and no vector DB to run.

---

## ✨ Features

- **Vectorless retrieval** — no embeddings, no vector DB
- **Live EUR-Lex search** via SPARQL on the CELLAR endpoint (free, no auth)
- **Smart search terms** — EU-domain synonyms (e.g. *motorcycle* → *motor vehicle, L-category*)
- **Confidence gate** — coverage and grounding scores, with up to 2 automatic retries
- **Keyword Discovery** — when retries fail, shows what was missed and suggests new terms
- **Structured answers** — optional Obligations / Rights / Definitions / Exceptions / Procedure view
- **Bring your own documents** — upload PDF, TXT or MD and query them alongside EUR-Lex
- **Local cache** — each law is downloaded and parsed once, then reused
- **Optional Gemini panel** — a second answer grounded in Google Search, for comparison
- **Shows what each answer costs** — tokens and euros per answer, split by pipeline step, with a monthly budget
- **Choose the answer model** — Opus, Sonnet or Haiku, with the price per question shown before you ask
- **Demo mode, no key needed** — replays real recorded answers with their real token counts

---

## 🧠 How it works

```
User Question
      │
      ▼
┌──────────────────────────────────────────┐
│  1. Keyword Extraction      (Haiku)      │
│  2. SPARQL Search  →  EUR-Lex CELLAR     │
│     Compound AND · synonym expansion     │
│  3. PDF Archive  (local, reused)         │
│  4. Article Tree Builder    (Haiku)      │
│     normalize → parse → sub-chunk        │
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
          │     Claude Opus (streaming)      │
          └──────────┬───────────────────────┘
                     │
          ┌──────────▼───────────────────────┐
          │  7. Confidence Gate   (Haiku)    │
          │     score < 50 OR coverage < 40  │
          │     → silent retry ×2            │
          │     → Keyword Discovery UI       │
          └──────────────────────────────────┘
```

1. **Search** — Haiku extracts search terms and the year; a SPARQL query finds matching laws on EUR-Lex. Original legislation is preferred over consolidated versions.
2. **Archive** — each PDF is saved locally on first download, so later queries need no network call.
3. **Article tree** — the PDF is cleaned and split into article nodes. Long articles are split into sub-nodes. Haiku writes a one-line summary for each.
4. **Navigate** — Haiku sees only the article IDs and summaries (~3,000 tokens for a 60-article law) and scores each one 0–10. Articles scoring 4+ move on.
5. **Answer and check** — Claude Opus streams the answer from the selected articles. Haiku then scores coverage and grounding. Low scores trigger a wider search, and the best answer wins.

Full details are in the [documentation](https://saeedm31.github.io/LexTreeRAG/) and [TECHNICAL_DOCS.md](TECHNICAL_DOCS.md).

---

## 💶 What each answer costs

One question makes several model calls: Haiku sorts the question, picks search words, indexes new laws,
chooses articles and checks the answer; the chosen model (Claude Opus 4.6 by default) writes the answer.
LexTreeRAG counts every call and shows the bill:

- **Under each answer:** the number of model calls, tokens in and out, and the price in euros, plus
  **Where the money went**: the cost of each step and its share. Token counts come from the provider's reply,
  so they are exact (thinking tokens included); euros are estimated from list prices.
- **Indexing a law is a one-time cost.** Each law is summarised once and cached; when a later question reuses
  it, the answer shows what the cache saved.
- **💶 Usage tab:** spend this session, today, this month and since the last reset; spend by step, by model
  and per day; the most expensive questions; savings from the cache; a call-by-call log with CSV download.
- **Monthly budget:** a warning at 80%; at 100% AI answers stop until next month, a higher budget or a reset.
  Budget, USD→EUR rate and prices are editable. Everything is kept in your browser only; your key is never stored.
- **Answer model picker:** Opus 4.6 / 5.5, Sonnet 4.6 / 5.5, Haiku 4.5 / 5.5, with a rough price per question
  before you ask and your real average after.
- **Command line too:** `python main.py` prints the same cost line after each answer.

### Demo mode (no key)

**▶️ Demo** replays answers that were recorded with a real key: the same answer, references and confidence,
and the real token counts of that run, so the cost tracker shows real numbers. Nothing is charged.
To add one, ask in **⚡ Live** mode and use **💾 Save this answer as a demo example**
(see [`demo/examples/`](demo/examples/)).

---

## 🚀 Quick start

```bash
git clone https://github.com/saeedm31/LexTreeRAG.git
cd LexTreeRAG
pip install -r requirements.txt

cp .env.example .env        # add your ANTHROPIC_API_KEY
streamlit run app.py        # opens http://localhost:8501
```

Requires **Python 3.9+**.

### Configuration

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ | From [console.anthropic.com](https://console.anthropic.com/) |
| `GEMINI_API_KEY` | Optional | Turns on the Gemini comparison panel ([free tier](https://aistudio.google.com/)) |
| `CACHE_DIR` | Optional | Where article trees are stored (default `./data/cache`) |
| `PDF_ARCHIVE_DIR` | Optional | Where PDFs are stored (default `./data/pdfs`) |
| `MAX_LAWS_PER_YEAR` | Optional | Laws fetched per search (default `5`) |

### Command line

```bash
python main.py                                   # interactive
python main.py -q "What are GDPR obligations for data controllers?"
python main.py -q "..." --max-laws 10 --save-report
```

---

## 🖥️ The app

| | |
|---|---|
| **💬 Ask EUR-Lex** | Chat, confidence scores, keyword analysis |
| **📋 References** | Source cards with EUR-Lex links, JSON export |
| **💶 Usage** | Spend per session, day, month and step; budget; prices; CSV export |
| **🗄️ Cache Manager** | Browse cached laws and the PDF archive |

<details>
<summary>More screenshots</summary>

![References page](docs/screenshots/references-page.png)
![Cache manager](docs/screenshots/cache-page.png)

</details>

---

## 📁 Project structure

```
LexTreeRAG/
├── app.py                      # Streamlit web UI
├── main.py                     # CLI entry point
├── pipeline/
│   ├── keyword_extractor.py    # Search terms + year
│   ├── eurolex_client.py       # SPARQL search + CELLAR fetch
│   ├── pdf_archive.py          # Local PDF archive + inbox
│   ├── tree_builder.py         # Article tree parser
│   ├── tree_navigator.py       # LLM article selection
│   ├── answer_verifier.py      # Confidence scoring
│   ├── clause_ranker.py        # Obligation signal scoring
│   ├── doc_ingester.py         # Uploaded files → tree
│   ├── query_classifier.py     # Question type + sub-questions
│   ├── gemini_search.py        # Optional Gemini layer
│   ├── costs.py                # Token counts → euros, model prices
│   ├── metering.py             # Counts every model call, labelled by step
│   ├── usage.py                # Usage ledger kept in the browser, budget
│   └── demo.py                 # Record and replay demo answers
├── demo/examples/              # Recorded answers for Demo mode
├── data/                       # Cache + PDF archive (gitignored)
└── tests/                      # 200+ unit tests, no API key needed
```

## ✅ Tests

```bash
pip install pytest
pytest -q
```

The tests run offline (no API calls) on every push with GitHub Actions. They cover article parsing, keyword and obligation-signal extraction, clause ranking, tree navigation, end-to-end retrieval scenarios on sample regulation text, and the token counting and cost tracking.

---

## ⚠️ Disclaimer

LexTreeRAG is a research and demo tool. Its answers are not legal advice — always check the cited articles.

## 🤝 Contributing

Pull requests are welcome. Please open an issue first for larger changes.

Built by [Saeed Hojjatpanah](https://github.com/saeedm31).
