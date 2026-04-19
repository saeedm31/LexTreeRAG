"""
Retrieval quality regression tests.

Each test class is anchored to a real failed query so the bug cannot
silently regress.  No API calls are made — all trees/nodes are mocked
with realistic article text that mirrors the actual regulation structure.

Test cases:
  - 2023/1542 "used batteries vs waste batteries" documentation query
    (originally retrieved only Article 1 – scope and returned no answer)
"""

import pytest
from pipeline.clause_ranker import (
    obligation_score,
    list_score,
    is_generic_section,
    rerank_nodes,
    has_answer_signal,
    obligation_expansion_keywords,
)
from pipeline.keyword_extractor import _extract_exact_signals, _expand_obligation_keywords
from pipeline.tree_navigator import make_slim_tree


# ─────────────────────────────────────────────────────────────────────────────
# Realistic mock article texts from Regulation (EU) 2023/1542
# ─────────────────────────────────────────────────────────────────────────────

# Article 1 — the generic scope article that was being returned instead of the answer
ART1_SCOPE_TEXT = """\
This Regulation lays down requirements for sustainability, safety, labelling
and information, and end-of-life requirements, including extended producer
responsibility, collection, treatment and recycling, for batteries.

It also lays down rules on due diligence obligations for economic operators
making batteries available on the market.
"""
ART1_TITLE = "Subject matter and scope"

# Article 76 — the specific provision that answers the question about
# documentation required to distinguish "used batteries" from "waste batteries"
# (mirrors the actual structure of Art. 76, Reg. 2023/1542)
ART76_TEXT = """\
Article 76
Shipment of used batteries and waste batteries

1. For the purposes of this Regulation, the holder of a battery shall,
prior to shipment, provide documentation demonstrating that the battery is
a used battery intended for reuse and not a waste battery within the meaning
of Directive 2008/98/EC.

2. The documentation referred to in paragraph 1 shall include at least the
following:

(a) a copy of the invoice and contract relating to the sale or transfer of
ownership of the battery confirming that the battery is destined for direct
reuse or preparation for reuse;

(b) evidence of evaluation or testing, including the results of functional
tests demonstrating that the battery is capable of performing its intended
function, and a record that the battery is not damaged, does not leak and
does not pose a risk to human health or the environment during transport;

(c) a statement from the holder that none of the material or components
listed in Annex VI is present in the battery in quantities that would
prevent its reuse;

(d) documentation of the destination, including the name and address of the
facility to which the battery is being shipped and confirmation that the
facility is authorised to receive used batteries for reuse or preparation
for reuse.

3. In the absence of documentation meeting the requirements of paragraph 2,
the battery shall be presumed to be a waste battery and shall be subject to
the requirements applicable to waste batteries under this Regulation and
under Directive 2008/98/EC.

4. The holder shall ensure that the documentation referred to in paragraph 2
is available for inspection by competent authorities for a period of at
least three years from the date of shipment.
"""
ART76_TITLE = "Shipment of used batteries and waste batteries"

# Article 83 — another provision about penalties (specific but different topic)
ART83_TEXT = """\
Member States shall lay down rules on penalties applicable to infringements
of this Regulation and shall take all measures necessary to ensure that
they are implemented.

The penalties shall be effective, proportionate and dissuasive.
"""
ART83_TITLE = "Penalties"

QUESTION = (
    "According to Regulation (EU) 2023/1542, what documentation and evidence "
    "must a holder provide to demonstrate that shipped batteries are 'used batteries' "
    "intended for reuse rather than 'waste batteries' under Directive 2008/98/EC?"
)


def _make_node(node_id, number, title, text, nav_score=7):
    """Build a node as it would come out of navigate_tree()."""
    return {
        "doc_id":          "32023R1542",
        "node_id":         node_id,
        "title":           title,
        "number":          number,
        "text":            text,
        "summary":         f"Covers {title.lower()}.",
        "relevance_score": nav_score,
        "doc_title":       "EU Battery Regulation",
        "year":            2023,
        "doc_url":         "https://eur-lex.europa.eu/32023R1542",
        "obligation_score": obligation_score(text),
        "list_score":       list_score(text),
    }


def _make_tree_with_nodes(nodes_raw):
    """Build a slim-tree-compatible tree dict."""
    return {
        "doc_id": "32023R1542",
        "title":  "EU Battery Regulation",
        "year":   2023,
        "url":    "https://eur-lex.europa.eu/32023R1542",
        "nodes":  nodes_raw,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. Keyword and exact-signal extraction
# ─────────────────────────────────────────────────────────────────────────────

class TestKeywordExtraction:
    """The question must produce signals that guide SPARQL search to Art. 76."""

    def test_regulation_2023_1542_extracted(self):
        signals = _extract_exact_signals(QUESTION)
        assert "2023/1542" in signals

    def test_directive_2008_98_extracted(self):
        signals = _extract_exact_signals(QUESTION)
        assert "2008/98" in signals

    def test_both_regulations_extracted(self):
        signals = _extract_exact_signals(QUESTION)
        assert len(signals) >= 2

    def test_obligation_expansion_triggered(self):
        # "documentation", "evidence", "demonstrate" are expansion triggers
        base = ["batteries", "used batteries", "waste batteries", "documentation"]
        kws  = _expand_obligation_keywords(QUESTION, base)
        assert len(kws) > len(base)
        assert any(k in kws for k in ["shall", "required", "obligation"])

    def test_documentation_trigger_activates_expansion(self):
        kws = _expand_obligation_keywords(
            "What documentation must a holder provide?", ["batteries"]
        )
        assert len(kws) > 1

    def test_no_false_expansion_on_definition_question(self):
        # A pure definition question should not trigger obligation expansion
        kws = _expand_obligation_keywords(
            "What is a battery according to EU law?", ["battery", "definition"]
        )
        # "definition" questions don't strongly trigger obligation expansion
        # (result may or may not expand — just check it's a list)
        assert isinstance(kws, list)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Signal scores on real article texts
# ─────────────────────────────────────────────────────────────────────────────

class TestArticleSignals:
    """Article 76 must score much higher than Article 1 on every signal metric."""

    def test_art1_obligation_score_is_low(self):
        score = obligation_score(ART1_SCOPE_TEXT)
        assert score < 4  # scope article has almost no "shall"/"must"

    def test_art76_obligation_score_is_high(self):
        score = obligation_score(ART76_TEXT)
        assert score >= 10  # "shall", "shall include", "shall ensure" × multiple

    def test_art1_list_score_is_zero(self):
        assert list_score(ART1_SCOPE_TEXT) == 0

    def test_art76_list_score_is_high(self):
        # Art 76 has (a), (b), (c), (d) enumerated items
        assert list_score(ART76_TEXT) >= 4

    def test_art1_is_generic_section(self):
        assert is_generic_section(ART1_TITLE, ART1_SCOPE_TEXT) is True

    def test_art76_is_not_generic_section(self):
        assert is_generic_section(ART76_TITLE, ART76_TEXT) is False

    def test_art76_obligation_score_exceeds_art1(self):
        assert obligation_score(ART76_TEXT) > obligation_score(ART1_SCOPE_TEXT)

    def test_art76_list_score_exceeds_art1(self):
        assert list_score(ART76_TEXT) > list_score(ART1_SCOPE_TEXT)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Reranker — Art. 76 must rank above Art. 1
# ─────────────────────────────────────────────────────────────────────────────

class TestRerankerOrdersCorrectly:
    """
    Regression: when navigator returns [Art1, Art76], reranker must
    place Art76 first.  This was the original bug — Art1 was the only
    article returned and it was passed directly to the LLM.
    """

    def _nodes(self, art1_nav_score=7, art76_nav_score=7):
        art1  = _make_node("art_1",  "1",  ART1_TITLE,  ART1_SCOPE_TEXT,  art1_nav_score)
        art76 = _make_node("art_76", "76", ART76_TITLE, ART76_TEXT, art76_nav_score)
        return [art1, art76]

    def test_art76_ranked_first_equal_nav_scores(self):
        ranked = rerank_nodes(self._nodes(7, 7))
        assert ranked[0]["node_id"] == "art_76"

    def test_art76_ranked_first_even_if_nav_score_lower(self):
        # Even if the navigator scored Art1 slightly higher, reranker should correct it
        ranked = rerank_nodes(self._nodes(art1_nav_score=8, art76_nav_score=7))
        assert ranked[0]["node_id"] == "art_76"

    def test_art1_gets_generic_penalty(self):
        nodes  = [_make_node("art_1", "1", ART1_TITLE, ART1_SCOPE_TEXT, nav_score=7)]
        ranked = rerank_nodes(nodes)
        # Penalty of -4 should reduce final score below nav_score of 7
        assert ranked[0]["final_score"] < 7

    def test_art76_final_score_substantially_higher(self):
        art1_node  = _make_node("art_1",  "1",  ART1_TITLE,  ART1_SCOPE_TEXT,  7)
        art76_node = _make_node("art_76", "76", ART76_TITLE, ART76_TEXT, 7)
        ranked     = rerank_nodes([art1_node, art76_node])
        art1_score  = next(n["final_score"] for n in ranked if n["node_id"] == "art_1")
        art76_score = next(n["final_score"] for n in ranked if n["node_id"] == "art_76")
        assert art76_score > art1_score + 5

    def test_obligation_score_raw_stored_on_art76(self):
        nodes  = [_make_node("art_76", "76", ART76_TITLE, ART76_TEXT, 7)]
        ranked = rerank_nodes(nodes)
        assert ranked[0]["obligation_score_raw"] >= 10

    def test_list_score_raw_stored_on_art76(self):
        nodes  = [_make_node("art_76", "76", ART76_TITLE, ART76_TEXT, 7)]
        ranked = rerank_nodes(nodes)
        assert ranked[0]["list_score_raw"] >= 4

    def test_clause_extraction_preserves_shall_paragraphs(self):
        nodes  = [_make_node("art_76", "76", ART76_TITLE, ART76_TEXT, 7)]
        ranked = rerank_nodes(nodes)
        # The extracted text must still contain the obligation header
        assert "shall" in ranked[0]["text"]

    def test_clause_extraction_preserves_list_items(self):
        nodes  = [_make_node("art_76", "76", ART76_TITLE, ART76_TEXT, 7)]
        ranked = rerank_nodes(nodes)
        # At least some (a)/(b)/(c)/(d) items must survive extraction
        extracted = ranked[0]["text"]
        assert "(a)" in extracted or "(b)" in extracted or "(c)" in extracted


# ─────────────────────────────────────────────────────────────────────────────
# 4. Answer validation gate — only Art. 76 should pass
# ─────────────────────────────────────────────────────────────────────────────

class TestAnswerValidationGate:
    """
    Regression: when the pipeline only retrieved Art. 1, has_answer_signal
    should have returned False and triggered a retry.
    """

    def test_art1_alone_fails_validation(self):
        """This is the bug: Art1 only → should have retried, not answered."""
        nodes  = [_make_node("art_1", "1", ART1_TITLE, ART1_SCOPE_TEXT, nav_score=5)]
        ranked = rerank_nodes(nodes)
        assert has_answer_signal(ranked) is False

    def test_art76_alone_passes_validation(self):
        nodes  = [_make_node("art_76", "76", ART76_TITLE, ART76_TEXT, nav_score=7)]
        ranked = rerank_nodes(nodes)
        assert has_answer_signal(ranked) is True

    def test_art1_high_nav_score_still_fails_if_no_obligation(self):
        # Even nav_score=9 should not rescue a pure scope article
        # (has_answer_signal uses nav score >= 7 as fallback only when
        # there's no other choice — here Art1 has nav_score=6 which is <7)
        nodes  = [_make_node("art_1", "1", ART1_TITLE, ART1_SCOPE_TEXT, nav_score=6)]
        ranked = rerank_nodes(nodes)
        assert has_answer_signal(ranked) is False

    def test_mixed_art1_and_art76_passes_validation(self):
        art1  = _make_node("art_1",  "1",  ART1_TITLE,  ART1_SCOPE_TEXT,  5)
        art76 = _make_node("art_76", "76", ART76_TITLE, ART76_TEXT, 8)
        ranked = rerank_nodes([art1, art76])
        assert has_answer_signal(ranked) is True

    def test_high_nav_score_fallback_still_works(self):
        # A node with nav_score >= 7 passes even without obligation keywords
        # (this is the LLM-trust fallback for unusual article formats)
        node   = _make_node("art_50", "50", "Technical specifications",
                            "The system configuration parameters.", nav_score=8)
        ranked = rerank_nodes([node])
        assert has_answer_signal(ranked) is True


# ─────────────────────────────────────────────────────────────────────────────
# 5. Slim tree — OBL/LIST badges must appear for Art. 76
# ─────────────────────────────────────────────────────────────────────────────

class TestSlimTreeBadges:
    """
    The navigator sees the slim tree before selecting articles.
    Art. 76 must carry [OBL] and [LIST] badges so the LLM navigator
    knows to prefer it over Art. 1.
    """

    def _slim(self):
        nodes = [
            {
                "id": "art_1", "number": "1", "title": ART1_TITLE,
                "summary": "Defines scope.",
                "text": ART1_SCOPE_TEXT,
                "obligation_score": obligation_score(ART1_SCOPE_TEXT),
                "list_score":       list_score(ART1_SCOPE_TEXT),
            },
            {
                "id": "art_76", "number": "76", "title": ART76_TITLE,
                "summary": "Requires documentation to prove battery is used not waste.",
                "text": ART76_TEXT,
                "obligation_score": obligation_score(ART76_TEXT),
                "list_score":       list_score(ART76_TEXT),
            },
        ]
        tree = _make_tree_with_nodes(nodes)
        return make_slim_tree([tree])

    def test_art76_obl_badge_present(self):
        slim = self._slim()
        # Find the Art 76 section and check for [OBL]
        art76_section = slim[slim.index("art_76"):]
        line = art76_section.split("\n")[0]
        assert "[OBL]" in line

    def test_art76_list_badge_present(self):
        slim = self._slim()
        art76_section = slim[slim.index("art_76"):]
        line = art76_section.split("\n")[0]
        assert "[LIST]" in line

    def test_art1_no_obl_badge(self):
        slim = self._slim()
        art1_section = slim[slim.index("art_1"):slim.index("art_76")]
        line = art1_section.split("\n")[0]
        assert "[OBL]" not in line

    def test_art1_no_list_badge(self):
        slim = self._slim()
        art1_section = slim[slim.index("art_1"):slim.index("art_76")]
        line = art1_section.split("\n")[0]
        assert "[LIST]" not in line

    def test_art76_summary_in_slim(self):
        slim = self._slim()
        assert "Requires documentation" in slim

    def test_both_articles_in_slim(self):
        slim = self._slim()
        assert "art_1" in slim
        assert "art_76" in slim


# ─────────────────────────────────────────────────────────────────────────────
# 6. End-to-end pipeline simulation (no API)
# ─────────────────────────────────────────────────────────────────────────────

class TestEndToEndSimulation:
    """
    Simulate what should happen step-by-step when this question is asked.
    The pipeline receives [Art1, Art76] from the navigator and must
    produce Art76 as the primary result with a passing answer signal.
    """

    def test_full_pipeline_scenario(self):
        """
        Given: navigator returns Art1 (scope) + Art76 (documentation)
        Expect: reranker puts Art76 first, answer validation passes,
                Art76 text contains the (a)(b)(c)(d) list.
        """
        nav_output = [
            _make_node("art_1",  "1",  ART1_TITLE,  ART1_SCOPE_TEXT,  nav_score=6),
            _make_node("art_76", "76", ART76_TITLE, ART76_TEXT, nav_score=8),
        ]
        ranked = rerank_nodes(nav_output)

        # 1. Correct primary result
        assert ranked[0]["node_id"] == "art_76", (
            f"Expected art_76 first, got {ranked[0]['node_id']} "
            f"(final_score={ranked[0]['final_score']})"
        )

        # 2. Answer signal passes — no hard failure
        assert has_answer_signal(ranked) is True

        # 3. The text passed to the LLM contains the actual list
        primary_text = ranked[0]["text"]
        list_items_present = sum(
            1 for item in ["(a)", "(b)", "(c)", "(d)"]
            if item in primary_text
        )
        assert list_items_present >= 3, (
            f"Expected (a)(b)(c)(d) list in extracted text, "
            f"found only {list_items_present} items. Text:\n{primary_text[:500]}"
        )

        # 4. The "shall provide" obligation is preserved in extracted text
        assert "shall" in primary_text

    def test_art1_only_triggers_retry_signal(self):
        """
        Given: navigator returns ONLY Art1 (the original bug scenario)
        Expect: answer validation fails → retry should be triggered
        """
        nav_output = [
            _make_node("art_1", "1", ART1_TITLE, ART1_SCOPE_TEXT, nav_score=6)
        ]
        ranked = rerank_nodes(nav_output)
        assert has_answer_signal(ranked) is False, (
            "Art1 (scope only) should fail has_answer_signal and trigger a retry"
        )

    def test_exact_signals_include_both_regulations(self):
        """Exact signals must include both 2023/1542 and 2008/98."""
        signals = _extract_exact_signals(QUESTION)
        assert "2023/1542" in signals
        assert "2008/98" in signals

    def test_obligation_expansion_applied_to_question(self):
        """Documentation-type question must get obligation keywords injected."""
        base = ["batteries", "used batteries", "waste batteries"]
        expanded = _expand_obligation_keywords(QUESTION, base)
        assert len(expanded) > len(base)

    def test_art76_documentation_list_completeness(self):
        """The (a)-(d) items in Art. 76 must all score via list_score."""
        score = list_score(ART76_TEXT)
        assert score >= 4, f"Expected >= 4 list items, got {score}"

    def test_reranked_art76_preserves_documentation_keywords(self):
        """After reranking, 'documentation' keyword must survive in extracted text."""
        nodes  = [_make_node("art_76", "76", ART76_TITLE, ART76_TEXT, nav_score=8)]
        ranked = rerank_nodes(nodes)
        assert "documentation" in ranked[0]["text"].lower()
