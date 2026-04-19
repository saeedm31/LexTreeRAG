"""
Search EUR-Lex via the CELLAR SPARQL endpoint, then fetch the XHTML content
of each document from publications.europa.eu (no WAF — works without a browser).

No PDFs needed — everything is fetched live.

Tested predicates (April 2026):
  cdm:resource_legal_id_celex        → CELEX number string
  cdm:date_creation_legacy           → date string "YYYY-MM-DD"
  cdm:expression_belongs_to_work     → reverse relation expression→work
  cdm:expression_title               → document title string
  cdm:expression_uses_language       → language URI
  HTML content URL pattern:
      https://publications.europa.eu/resource/cellar/{UUID}.0001.03/DOC_1
"""

from __future__ import annotations
import io
import re
import time
import warnings
import requests
import html2text
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from concurrent.futures import ThreadPoolExecutor
from typing import Optional


# ── Regulation citation extractor ─────────────────────────────────────────────

# Handles both numbering conventions:
#   New (post-2015): YEAR/NUMBER  e.g. 2018/858,  2014/65/EU
#   Old (pre-2015):  NUMBER/YEAR  e.g. 715/2007,  No 1907/2006

# New-style: 4-digit year first
_CITATION_NEW = re.compile(
    r"""
    (?:\((?:EU|EC|Euratom|EEA)\)\s*(?:No\.?\s*)?|No\.?\s+)?
    \b((?:19|20)\d{2})           # year (19xx or 20xx)
    /
    (\d{1,4})                    # number
    (?:/[A-Z]+)?                 # optional /EU suffix
    \b
    """,
    re.VERBOSE,
)

# Old-style: 1-4 digit number first, then 4-digit year
_CITATION_OLD = re.compile(
    r"""
    (?:\((?:EU|EC|Euratom|EEA)\)\s*(?:No\.?\s*)?|No\.?\s+)?
    \b(\d{1,4})                  # number (short)
    /
    ((?:19|20)\d{2})             # year
    \b
    """,
    re.VERBOSE,
)


def extract_cited_regulations(text: str) -> list[dict]:
    """
    Find all EU act citation patterns in free text and return structured info.

    Handles both numbering styles:
      New (post-2015)  YEAR/NUMBER : "(EU) 2018/858"  → keyword "2018/858"
      Old (pre-2015)   NUMBER/YEAR : "(EC) No 715/2007" → keyword "715/2007"

    The returned "keyword" strings appear verbatim in EUR-Lex title fields
    and can be fed directly into the SPARQL CONTAINS filter.
    """
    found = []
    seen: set[str] = set()

    # New-style matches
    for m in _CITATION_NEW.finditer(text):
        year, number = int(m.group(1)), m.group(2)
        key = f"{year}/{number}"
        if key not in seen:
            seen.add(key)
            found.append({"year": year, "number": number, "keyword": key})

    # Old-style matches (NUMBER/YEAR)
    for m in _CITATION_OLD.finditer(text):
        number, year = m.group(1), int(m.group(2))
        key = f"{number}/{year}"
        if key not in seen:
            seen.add(key)
            found.append({"year": year, "number": number, "keyword": key})

    return found


def fetch_by_citation(citation: dict, sectors: tuple[str, ...] = ("3",)) -> Optional[dict]:
    """
    Given a parsed citation dict {"year": 2018, "number": "858", "keyword": "2018/858"},
    resolve the document in CELLAR by trying candidate CELEX IDs via owl:sameAs.

    Tries: Regulation (R), Directive (L), Decision (D) for each requested sector.
    Returns a search-result dict {celex, title, date, url, cellar_uuid} or None.
    """
    year   = citation["year"]
    number = citation["number"].zfill(4)   # 858 → 0858

    doc_types = ["R", "L", "D", "DC", "PC"]
    candidates = [
        f"{s}{year}{dt}{number}"
        for s in sectors
        for dt in doc_types
    ]

    for celex in candidates:
        q = f"""
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT ?work ?title ?date WHERE {{
  ?work owl:sameAs <http://publications.europa.eu/resource/celex/{celex}> .
  OPTIONAL {{
    ?expr cdm:expression_belongs_to_work ?work ;
          cdm:expression_title ?title ;
          cdm:expression_uses_language <{_ENGLISH_LANG}> .
  }}
  OPTIONAL {{ ?work cdm:date_creation_legacy ?date . }}
}}
LIMIT 1
"""
        try:
            bindings = _sparql(q)
            if bindings:
                b        = bindings[0]
                title    = b.get("title", {}).get("value", celex)
                date     = b.get("date",  {}).get("value", "")
                work_uri = b.get("work",  {}).get("value", "")
                uuid     = _cellar_uuid(work_uri)
                return {
                    "celex":       celex,
                    "title":       title,
                    "date":        date,
                    "url":         EURLEX_DOC_URL.format(celex=celex),
                    "cellar_uuid": uuid,
                }
        except Exception as exc:
            print(f"[eurolex] citation lookup error ({celex}): {exc}")
            continue
    return None

try:
    import pypdf
    _PYPDF_OK = True
except ImportError:
    _PYPDF_OK = False

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# ── endpoints ────────────────────────────────────────────────────────────────
SPARQL_ENDPOINT  = "https://publications.europa.eu/webapi/rdf/sparql"
CELLAR_HTML_TMPL = (
    "https://publications.europa.eu/resource/cellar/{uuid}.0001.03/DOC_1"
)
EURLEX_DOC_URL   = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{celex}"

REQUEST_DELAY       = 0.3    # seconds between HTTP fetches (be polite but faster)
SEARCH_DELAY        = 0.5    # seconds between search queries (be polite to EUR-Lex search)

_HEADERS = {
    "User-Agent": "EuroLexRAG-Research/1.0 (academic)",
    "Accept": "application/xhtml+xml,text/html,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

_ENGLISH_LANG = (
    "http://publications.europa.eu/resource/authority/language/ENG"
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _cellar_uuid(work_uri: str) -> str:
    """Extract the UUID from a CELLAR work URI."""
    # e.g. http://publications.europa.eu/resource/cellar/6337734c-58e4-...
    m = re.search(
        r"cellar/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]+)",
        work_uri,
    )
    return m.group(1) if m else ""


def _sparql(query: str) -> list[dict]:
    """Run a SPARQL query against CELLAR and return bindings."""
    resp = requests.get(
        SPARQL_ENDPOINT,
        params={"query": query, "format": "application/sparql-results+json"},
        headers={"Accept": "application/sparql-results+json", "User-Agent": "curl/7.84.0"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("results", {}).get("bindings", [])


# ── EUR-Lex REST full-text search ─────────────────────────────────────────────

def _search_eurlex_rest(
    keywords: list[str],
    max_results: int,
    year: Optional[int] = None,
    sectors: tuple[str, ...] = ("3",),
) -> list[dict]:
    """
    Full-text search via EUR-Lex web search interface.

    Uses EUR-Lex's own search index which searches across document titles,
    summaries, keywords, and metadata — much richer than SPARQL title-only
    search. EUR-Lex internally uses a sophisticated search engine.

    Returns list of {celex, title, date, url, cellar_uuid} or empty list on error.
    Falls back gracefully so SPARQL can take over.
    """
    query_text = " ".join(str(k) for k in keywords[:10])

    params = {
        "scope":      "EURLEX",
        "text":       query_text,
        "lang":       "en",
        "type":       "quick",       # searches titles, summaries, keywords
        "DTS_SUBDOM": "EU_LAW",
    }

    try:
        time.sleep(SEARCH_DELAY)
        resp = requests.get(
            "https://eur-lex.europa.eu/search.html",
            params=params,
            headers=_HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
    except Exception as exc:
        print(f"[eurolex] REST search failed: {exc}")
        return []

    # ── Extract CELEX IDs from search result links ────────────────────────────
    soup = BeautifulSoup(resp.text, "lxml")
    seen: set[str] = set()
    raw_docs: list[dict] = []

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        m = re.search(r'[?&;]uri=CELEX[:%3A]([A-Z0-9]+(?:-\d+)?)', href)
        if not m:
            continue

        celex = m.group(1)

        # Sector filter — only keep requested sectors
        # CELEX format: sector(1) + year(4) + type(1) + number(4) + ...
        # e.g. 32019R2144 = sector 3, year 2019, type R (regulation), number 2144
        # e.g. 02016R0679 = consolidated (sector 0)
        if sectors and not any(celex.startswith(s) for s in sectors):
            continue

        # Year filter — CELEX contains the year for sector-3 docs
        if year and str(year) not in celex:
            continue

        if celex in seen:
            continue
        seen.add(celex)

        raw_docs.append({
            "celex": celex,
            "title": a.get_text(" ", strip=True)[:300] or celex,
        })

        if len(raw_docs) >= max_results * 3:
            break

    if not raw_docs:
        return []

    # ── Batch SPARQL for UUID + proper title + date ───────────────────────────
    # We have CELEX IDs and rough titles from HTML, now get authoritative metadata
    top = raw_docs[:max_results]
    values_clause = " ".join(f'"{d["celex"]}"' for d in top)

    sparql_q = f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT DISTINCT ?celex_v ?work ?title ?date WHERE {{
  VALUES ?celex_v {{ {values_clause} }}
  ?work cdm:resource_legal_id_celex ?celex_v .
  OPTIONAL {{
    ?expr cdm:expression_belongs_to_work ?work ;
          cdm:expression_title ?title ;
          cdm:expression_uses_language <{_ENGLISH_LANG}> .
  }}
  OPTIONAL {{ ?work cdm:date_creation_legacy ?date . }}
}}
"""
    meta: dict[str, dict] = {}
    try:
        for b in _sparql(sparql_q):
            c = b.get("celex_v", {}).get("value", "")
            if c and c not in meta:
                meta[c] = {
                    "title": b.get("title", {}).get("value", ""),
                    "date":  b.get("date",  {}).get("value", ""),
                    "uuid":  _cellar_uuid(b.get("work", {}).get("value", "")),
                }
    except Exception as exc:
        print(f"[eurolex] REST meta lookup error: {exc}")

    results = []
    for d in top:
        m2 = meta.get(d["celex"], {})
        results.append({
            "celex":       d["celex"],
            "title":       m2.get("title") or d["title"],
            "date":        m2.get("date", ""),
            "url":         EURLEX_DOC_URL.format(celex=d["celex"]),
            "cellar_uuid": m2.get("uuid", ""),
        })

    return results


# ── SPARQL search ─────────────────────────────────────────────────────────────

def _expand_keywords(keywords: list[str]) -> list[str]:
    """
    Expand a keyword list so that multi-word phrases also contribute their
    individual significant words. This maximises SPARQL title-match coverage.

    e.g. ["electric vehicle charging", "alternative fuels"]
      → ["electric vehicle charging", "electric", "vehicle", "charging",
         "alternative fuels", "alternative", "fuels"]

    Also injects EU-specific synonyms for common vehicle/legal concepts so that
    SPARQL title-only search finds the right regulations even when keywords use
    lay terms rather than official EU nomenclature.
    """
    # Short stop-words that add no value as standalone SPARQL terms
    _STOP = {
        "a", "an", "the", "of", "in", "for", "to", "on", "at", "by", "or",
        "and", "with", "that", "this", "from", "as", "be", "are", "is",
        "was", "were", "has", "have", "had", "its", "their", "between",
        "under", "over", "into", "about", "regulation", "directive",
        "decision", "obligation", "mandatory", "targets", "member",
        "states", "requirements", "measures",
        "motor",   # too generic on its own (split from "motor vehicle")
    }

    # EU-domain synonym map: lay term → official EU title terms.
    # IMPORTANT: use exact substrings that appear in regulation TITLES on EUR-Lex.
    _SYNONYMS: dict[str, list[str]] = {
        "motorcycle":       ["three-wheel", "quadricycle", "moped",
                             "motor vehicle", "l-category"],
        "car":              ["passenger car", "light-duty vehicle"],
        "truck":            ["heavy-duty vehicle", "heavy goods vehicle"],
        "van":              ["light commercial vehicle"],
        "bus":              ["coach"],
        "ev":               ["electric vehicle", "battery electric"],
        "electric car":     ["electric vehicle", "zero-emission"],
        "gdpr":             ["personal data", "data protection"],
        "chemical":         ["REACH", "substance", "hazardous"],
        "drone":            ["unmanned aircraft", "UAS", "RPAS"],
        "ai":               ["artificial intelligence", "automated decision"],
    }

    seen = set()
    expanded = []
    for kw in keywords:
        kw_l = kw.lower().strip()
        if kw_l and kw_l not in seen:
            seen.add(kw_l)
            expanded.append(kw_l)
        # Synonym lookup: try exact form, then strip trailing 's' (plural → singular)
        singular = kw_l.rstrip("s") if kw_l.endswith("s") and len(kw_l) > 3 else kw_l
        lookup_key = kw_l if kw_l in _SYNONYMS else (singular if singular in _SYNONYMS else None)
        for synonym in (_SYNONYMS.get(lookup_key, []) if lookup_key else []):
            s_l = synonym.lower()
            if s_l not in seen:
                seen.add(s_l)
                expanded.append(s_l)
        # Also add meaningful individual words from multi-word phrases
        if " " in kw_l:
            for word in kw_l.split():
                word = word.strip("(),;")
                if len(word) >= 4 and word not in _STOP and word not in seen:
                    seen.add(word)
                    expanded.append(word)
    return expanded


def _sparql_search(
    keywords: list[str],
    year: Optional[int],
    max_results: int,
    sectors: tuple[str, ...] = ("3",),
    require_sector: bool = True,
) -> list[dict]:
    """
    Run one SPARQL query for the given keyword list, year and sectors.
    When year is None the year filter is omitted (searches across all years).

    cdm:date_creation_legacy is OPTIONAL — many CELLAR nodes omit it, so
    making it required would silently drop valid documents.

    require_sector=False: drop the CELEX sector filter entirely, giving the
    widest possible coverage at the cost of some noise.
    """
    kw_filters = " || ".join(
        f'CONTAINS(LCASE(STR(?title)), "{kw}")'
        for kw in keywords
    )
    sector_clause = ""
    if require_sector and sectors:
        sector_filter = " || ".join(
            f'STRSTARTS(STR(?celex), "{s}")'
            for s in sectors
        )
        sector_clause = f"  FILTER({sector_filter})"

    year_clause = ""
    if year is not None:
        year_clause = f'  FILTER(STRSTARTS(STR(?date), "{year}"))'

    # Exclude pre-2010 documents: older EUR-Lex docs rarely have HTML in CELLAR.
    # Also exclude merger/competition decisions (type M) and parliamentary questions (9).
    min_year_clause = (
        '  FILTER(REGEX(STR(?celex), "(201[0-9]|202[0-9]|203[0-9])"))'
        '\n  FILTER(!REGEX(STR(?celex), "^[39][0-9]{4}M"))'
    )

    query = f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>

SELECT DISTINCT ?work ?celex ?title ?date WHERE {{
  ?work cdm:resource_legal_id_celex ?celex .
  OPTIONAL {{ ?work cdm:date_creation_legacy ?date . }}
  ?expr cdm:expression_belongs_to_work ?work ;
        cdm:expression_title ?title ;
        cdm:expression_uses_language <{_ENGLISH_LANG}> .
{year_clause}
{sector_clause}
{min_year_clause}
  FILTER({kw_filters})
}}
ORDER BY DESC(?date)
LIMIT {max_results}
"""
    try:
        return _sparql(query)
    except Exception as exc:
        print(f"[eurolex] SPARQL error: {exc}")
        return []


def _sparql_search_compound(
    domain_kws: list[str],
    topic_kws: list[str],
    year: Optional[int],
    max_results: int,
    sectors: tuple[str, ...] = ("3",),
) -> list[dict]:
    """
    AND-between-groups SPARQL search.
    Title must match at least one domain_kw AND at least one topic_kw.
    Used as Pass 0 to find vehicle-specific topic regulations.
    """
    domain_filter = " || ".join(
        f'CONTAINS(LCASE(STR(?title)), "{kw}")' for kw in domain_kws
    )
    topic_filter = " || ".join(
        f'CONTAINS(LCASE(STR(?title)), "{kw}")' for kw in topic_kws
    )
    sector_clause = ""
    if sectors:
        sector_filter = " || ".join(f'STRSTARTS(STR(?celex), "{s}")' for s in sectors)
        sector_clause = f"  FILTER({sector_filter})"
    year_clause = f'  FILTER(STRSTARTS(STR(?date), "{year}"))' if year else ""
    min_year_clause = (
        '  FILTER(REGEX(STR(?celex), "(201[0-9]|202[0-9]|203[0-9])"))'
        '\n  FILTER(!REGEX(STR(?celex), "^[39][0-9]{4}M"))'
    )
    query = f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT DISTINCT ?work ?celex ?title ?date WHERE {{
  ?work cdm:resource_legal_id_celex ?celex .
  OPTIONAL {{ ?work cdm:date_creation_legacy ?date . }}
  ?expr cdm:expression_belongs_to_work ?work ;
        cdm:expression_title ?title ;
        cdm:expression_uses_language <{_ENGLISH_LANG}> .
{year_clause}
{sector_clause}
{min_year_clause}
  FILTER(({domain_filter}) && ({topic_filter}))
}}
ORDER BY DESC(?date)
LIMIT {max_results}
"""
    try:
        return _sparql(query)
    except Exception as exc:
        print(f"[eurolex] Compound SPARQL error: {exc}")
        return []


def search_eurlex(
    keywords: list[str],
    year: Optional[int],
    max_results: int = 5,
    sectors: tuple[str, ...] = ("3",),
) -> list[dict]:
    """
    Search CELLAR for English-language legal acts whose title contains at least
    one of *keywords*, restricted to *sectors*.

    year: if provided, restricts results to that calendar year; if None,
          searches across all years (useful to catch laws registered in a
          different year than the one mentioned in the question).

    Sectors are the first character(s) of the CELEX number:
      "1" Treaties, "2" Int'l agreements, "3" Legislation, "4" Complementary,
      "5" Preparatory acts, "6" Case-law, "7" NTMs, "8" National case-law,
      "9" Parliamentary questions, "0" Consolidated, "C" OJ-C, "E" EFTA

    Multi-word keywords are automatically expanded to individual words so that
    title matches are not missed due to phrase mismatch.

    Returns a list of dicts:
        {celex, title, date, url, cellar_uuid}
    """
    all_keywords = _expand_keywords(keywords)
    _sectors = tuple(sectors) if sectors else ("3",)

    # NOTE: EUR-Lex search.html is blocked by AWS WAF — using SPARQL only.
    # SPARQL searches document titles. Strategy (each pass only runs if previous
    # pass found too few results):
    #   Pass 0 — AND compound: title matches vehicle-domain term AND topic term.
    #            Finds regulations like 2019/2144 that cover motor vehicle cybersecurity.
    #   Pass 1 — OR of specific (low-noise) keywords. Finds L-category regs via
    #            motorcycle synonyms (three-wheel, quadricycle, moped).
    #   Pass 2 — OR of all keywords including noisy ones (fallback).
    #   Pass 3 — Drop sector filter for maximum coverage (last resort).

    # Common legal boilerplate words: appear in too many unrelated titles.
    _NOISY = {
        "compliance", "regulations", "requirements", "shall", "required",
        "measures", "implementation", "obligations", "vehicle", "vehicles",
        "emission", "emissions", "standard", "standards", "rules", "act", "law",
        "market", "security", "approval",   # too generic: match telecom/aviation/other
    }

    # Vehicle/product domain keywords — used to separate domain from topic for AND search
    # Include both singular and plural forms so "motorcycles" is recognised.
    _VEHICLE_TERMS = {
        "motorcycle", "motorcycles", "three-wheel", "quadricycle", "quadricycles",
        "moped", "mopeds", "l-category", "motor vehicle", "motor vehicles",
        "passenger car", "light-duty vehicle", "heavy-duty vehicle",
        "heavy goods vehicle", "light commercial vehicle",
        "unmanned aircraft", "uas", "rpas", "electric vehicle", "battery electric",
        "zero-emission",
    }

    specific_kws = [k for k in all_keywords if k not in _NOISY]
    noisy_kws    = [k for k in all_keywords if k in _NOISY]

    # Split specific_kws into vehicle/domain terms vs regulatory topic terms
    domain_kws = [k for k in specific_kws if k in _VEHICLE_TERMS]
    topic_kws  = [k for k in specific_kws if k not in _VEHICLE_TERMS]

    seen_celex: set[str] = set()
    bindings: list[dict] = []

    # Fetch more candidates than max_results to allow client-side score re-ranking
    fetch_limit = max_results * 3

    # Pass 0: compound AND search — domain term AND topic term in title
    # Run on sector "3" (original legislation) ONLY so that non-consolidated docs
    # with actual PDFs are returned before dated consolidated snapshots (02xxx).
    # e.g. ("motor vehicle" OR "three-wheel") AND ("type-approval" OR "cybersecurity")
    if domain_kws and topic_kws:
        for row in _sparql_search_compound(domain_kws, topic_kws, year, max_results, ("3",)):
            celex = row.get("celex", {}).get("value", "")
            if celex and celex not in seen_celex:
                seen_celex.add(celex)
                bindings.append(row)

    # Pass 1: specific keywords only (OR) — domain terms, low false-positive rate
    if specific_kws:
        for row in _sparql_search(specific_kws, year, fetch_limit, _sectors):
            celex = row.get("celex", {}).get("value", "")
            if celex and celex not in seen_celex:
                seen_celex.add(celex)
                bindings.append(row)

    # Pass 2: only if results still short — try all keywords including noisy ones
    if len(bindings) < max_results:
        combined = specific_kws + noisy_kws
        for row in _sparql_search(combined, year, fetch_limit, _sectors):
            celex = row.get("celex", {}).get("value", "")
            if celex and celex not in seen_celex:
                seen_celex.add(celex)
                bindings.append(row)

    # Pass 3: if still empty, drop sector filter for maximum coverage
    if not bindings:
        kws_for_broad = specific_kws or all_keywords
        for row in _sparql_search(kws_for_broad, year, fetch_limit, _sectors,
                                  require_sector=False):
            celex = row.get("celex", {}).get("value", "")
            if celex and celex not in seen_celex:
                seen_celex.add(celex)
                bindings.append(row)

    # Deduplicate consolidated versions: e.g. 02013R0168-20241127 and
    # 02013R0168-20201114 are two snapshots of the same regulation — keep newest.
    seen_doc: set[str] = set()
    seen_base: set[str] = set()
    docs = []
    for b in bindings:
        celex      = b.get("celex",  {}).get("value", "")
        title      = b.get("title",  {}).get("value", celex)
        date       = b.get("date",   {}).get("value", "")
        work_uri   = b.get("work",   {}).get("value", "")
        uuid       = _cellar_uuid(work_uri)
        if not celex or celex in seen_doc:
            continue
        # Base celex = strip consolidation date suffix (e.g. "-20241127")
        base_celex = re.sub(r"-\d{8}$", "", celex)
        if base_celex in seen_base:
            continue
        seen_doc.add(celex)
        seen_base.add(base_celex)
        docs.append(
            {
                "celex":       celex,
                "title":       title,
                "date":        date,
                "url":         EURLEX_DOC_URL.format(celex=celex),
                "cellar_uuid": uuid,
            }
        )

    # Re-rank by: (1) keyword match count in title — more specific matches first,
    # (2) sector preference — sector-3 (original legislation, has PDFs) beats sector-0
    #    (consolidated versions, no PDFs in CELLAR), (3) date descending as tiebreaker.
    def _kw_score(doc: dict) -> int:
        t = doc["title"].lower()
        base = sum(1 for k in specific_kws if k in t)
        # Sector 3 gets +0.5 so it beats consolidated (0) at the same keyword score
        sector_bonus = 0.5 if doc["celex"].startswith("3") else 0.0
        return base + sector_bonus

    docs.sort(key=lambda d: (_kw_score(d), d.get("date") or ""), reverse=True)
    return docs[:max_results]


# ── HTML fetch & Markdown conversion ─────────────────────────────────────────

def _get_expression_slot(uuid: str) -> str:
    """
    Use SPARQL to find the English expression slot number for this CELLAR work.
    Returns the slot string (e.g. '0006') or '0001' as default fallback.
    The expression URI returned by SPARQL ends with '.NNNN' — that is the slot.
    """
    q = f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT ?expr WHERE {{
  ?expr cdm:expression_belongs_to_work
            <http://publications.europa.eu/resource/cellar/{uuid}> ;
        cdm:expression_uses_language <{_ENGLISH_LANG}> .
}}
LIMIT 1
"""
    try:
        bindings = _sparql(q)
        if bindings:
            expr_uri = bindings[0]["expr"]["value"]
            m = re.search(r"\.(\d{4})$", expr_uri)
            if m:
                return m.group(1)
    except Exception:
        pass
    return "0001"


def _html_to_markdown(html: str) -> str:
    """
    Convert EUR-Lex XHTML to clean Markdown text, preserving Article headings
    so the tree builder can split on them.
    """
    soup = BeautifulSoup(html, "lxml")

    # Drop non-content elements (tables are KEPT — annexes live in them)
    for tag in soup(["script", "style", "nav", "header", "footer",
                     "noscript", "iframe"]):
        tag.decompose()

    # Convert tables to pipe-delimited text BEFORE html2text so that:
    #   (a) annex tables are preserved as readable text
    #   (b) list_score() can detect the | col | col | pattern
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if any(c.strip() for c in cells):   # skip empty rows
                rows.append("| " + " | ".join(cells) + " |")
        if rows:
            table.replace_with(soup.new_string("\n\n" + "\n".join(rows) + "\n\n"))
        else:
            table.decompose()

    # Promote article titles to Markdown headings so the parser can find them
    # EUR-Lex XHTML uses <p class="sti-art"> / <p class="doc-ti"> for article headings
    for p in soup.find_all("p"):
        text = p.get_text(strip=True)
        if re.match(r"^Article\s+\d+", text, re.IGNORECASE):
            p.string = f"\n\n{text}\n"

    h = html2text.HTML2Text()
    h.ignore_links    = True
    h.ignore_images   = True
    h.body_width      = 0
    h.unicode_snob    = True
    h.ignore_tables   = True   # we already converted tables above

    md = h.handle(str(soup))
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return md


def _fetch_pdf_as_text(
    uuid: str,
    slot: str = "0001",
    *,
    save_celex: Optional[str] = None,
) -> Optional[str]:
    """
    Download the PDF manifestation (.{slot}.01/DOC_1) and extract plain text.
    If *save_celex* is provided, raw bytes are saved to the local PDF archive.
    Requires pypdf (pip install pypdf).
    """
    if not _PYPDF_OK:
        return None
    url = f"https://publications.europa.eu/resource/cellar/{uuid}.{slot}.01/DOC_1"
    try:
        resp = requests.get(url,
            headers={**_HEADERS, "Accept": "application/pdf,*/*"},
            timeout=20, allow_redirects=True)
        if resp.status_code != 200:
            return None
        ct = resp.headers.get("Content-Type", "")
        if "pdf" not in ct.lower():
            return None
        # Save to local archive before extracting text
        if save_celex:
            try:
                from pipeline.pdf_archive import save_pdf_bytes
                save_pdf_bytes(save_celex, resp.content)
            except Exception:
                pass
        reader = pypdf.PdfReader(io.BytesIO(resp.content))
        pages  = [page.extract_text() or "" for page in reader.pages]
        text   = "\n\n".join(pages)
        text   = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text if len(text) > 200 else None
    except Exception as exc:
        print(f"[eurolex] PDF fetch error ({uuid[:8]}…): {exc}")
        return None


def _fetch_html_as_markdown(uuid: str, slot: str) -> Optional[str]:
    """Fetch HTML at the given slot and convert to Markdown."""
    _MAX_HTML_BYTES = 600_000
    url = f"https://publications.europa.eu/resource/cellar/{uuid}.{slot}.03/DOC_1"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15, allow_redirects=True)
        if resp.status_code == 200:
            ct = resp.headers.get("Content-Type", "")
            if "html" in ct.lower() or "xml" in ct.lower():
                md = _html_to_markdown(resp.text[:_MAX_HTML_BYTES])
                if len(md) > 200:
                    return md
    except Exception:
        pass
    return None


def fetch_document_markdown(celex: str, cellar_uuid: str = "") -> Optional[str]:
    """
    Fetch and convert a EUR-Lex document to Markdown.

    Strategy per document:
      0. Check local PDF archive — if present, extract text immediately (no network)
      1. Resolve SPARQL expression slot (many consolidated docs use slot ≠ 0001)
      2. Try PDF at discovered slot  (PDF primary — save to archive on download)
      3. Try PDF at default slot 0001 (if slot ≠ 0001)
      4. Try HTML at discovered slot
      5. Try HTML at default slot 0001 (if slot ≠ 0001)

    Returns None if the document cannot be retrieved.
    """
    time.sleep(REQUEST_DELAY)

    # Step 0: fast path — check local PDF archive first (no network call needed)
    if celex and _PYPDF_OK:
        try:
            from pipeline.pdf_archive import load_pdf_bytes
            cached = load_pdf_bytes(celex)
            if cached:
                reader = pypdf.PdfReader(io.BytesIO(cached))
                pages  = [page.extract_text() or "" for page in reader.pages]
                text   = re.sub(r"\n{3,}", "\n\n", "\n\n".join(pages)).strip()
                if len(text) > 200:
                    print(f"[eurolex] archive hit: {celex}")
                    return text
        except Exception:
            pass

    uuid = cellar_uuid

    # If UUID not supplied, resolve it from SPARQL
    if not uuid:
        try:
            q = f"""
PREFIX owl: <http://www.w3.org/2002/07/owl#>
SELECT ?work WHERE {{
  ?work owl:sameAs <http://publications.europa.eu/resource/celex/{celex}> .
}}
LIMIT 1
"""
            bindings = _sparql(q)
            if bindings:
                uuid = _cellar_uuid(bindings[0]["work"]["value"])
        except Exception:
            pass

    if not uuid:
        return None

    # Discover the correct expression slot via SPARQL
    slot = _get_expression_slot(uuid)

    # Build ordered list of slots to try (deduplicated)
    slots = [slot] if slot == "0001" else [slot, "0001"]

    # 1. Try PDF first at each slot — save to archive on success
    for s in slots:
        text = _fetch_pdf_as_text(uuid, s, save_celex=celex if celex else None)
        if text:
            return text

    # 2. Fall back to HTML at each slot
    for s in slots:
        md = _fetch_html_as_markdown(uuid, s)
        if md:
            return md

    return None


# ── Parallel document fetching ────────────────────────────────────────────────

def fetch_documents_parallel(
    docs: list[dict],
    max_workers: int = 3,
) -> list[tuple[dict, Optional[str]]]:
    """
    Fetch markdown for multiple documents concurrently.
    Returns list of (doc, markdown_or_None) pairs in the same order as docs.
    """
    def _fetch(doc):
        return doc, fetch_document_markdown(doc["celex"], doc.get("cellar_uuid", ""))

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        return list(ex.map(_fetch, docs))


# ── Citation link following ───────────────────────────────────────────────────

def follow_citation_links(
    selected_nodes: list[dict],
    already_seen: set[str],
    sectors: tuple[str, ...] = ("3",),
    max_linked: int = 3,
) -> list[dict]:
    """
    Extract EU act citations from selected article texts, resolve them to CELEX IDs,
    and return new documents not already in the tree.
    Used for iterative retrieval — catches linked laws referenced within articles.
    """
    all_text = " ".join(n.get("text", "") for n in selected_nodes)
    citations = extract_cited_regulations(all_text)

    linked_docs = []
    seen = set(already_seen)
    for c in citations:
        if len(linked_docs) >= max_linked:
            break
        doc = fetch_by_citation(c, sectors=sectors)
        if doc and doc["celex"] not in seen:
            seen.add(doc["celex"])
            linked_docs.append(doc)
    return linked_docs
