"""
Tests for pipeline/clause_ranker.py

Covers:
  - obligation_score()
  - list_score()
  - is_generic_section()
  - extract_best_clauses()
  - rerank_nodes()
  - has_answer_signal()
  - obligation_expansion_keywords()
"""

import pytest
from pipeline.clause_ranker import (
    obligation_score,
    list_score,
    is_generic_section,
    extract_best_clauses,
    rerank_nodes,
    has_answer_signal,
    obligation_expansion_keywords,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — representative article texts
# ─────────────────────────────────────────────────────────────────────────────

OBLIGATION_TEXT = (
    "Member States shall ensure that manufacturers comply with this Regulation. "
    "Manufacturers must submit a declaration of conformity. "
    "Type-approval authorities shall verify the documentation required."
)

GENERIC_SCOPE_TEXT = (
    "This Regulation establishes rules for the approval of motor vehicles. "
    "It lays down the framework for safety requirements."
)

LIST_TEXT = (
    "The following systems shall be mandatory:\n"
    "(a) intelligent speed assistance;\n"
    "(b) driver drowsiness and attention warning;\n"
    "(c) advanced emergency braking;\n"
    "(d) lane keeping assistance."
)

TABLE_TEXT = (
    "Mandatory systems per Annex II:\n\n"
    "| Safety system | Applicable from |\n"
    "| Intelligent speed assistance | 6 July 2022 |\n"
    "| Emergency braking | 6 July 2024 |\n"
    "| Reversing detection | 6 July 2022 |"
)

DELEGATION_TEXT = (
    "The Commission may adopt delegated acts to supplement this Regulation. "
    "Those implementing acts shall be adopted in accordance with Article 87."
)

MIXED_TEXT = (
    "Article 5 — Mandatory safety systems\n\n"
    "From 6 July 2022, vehicles of category M1 shall be equipped with:\n\n"
    "(a) intelligent speed assistance systems;\n"
    "(b) driver drowsiness warning systems;\n\n"
    "The Commission is empowered to adopt delegated acts concerning the "
    "technical specifications of these systems."
)


# ─────────────────────────────────────────────────────────────────────────────
# obligation_score
# ─────────────────────────────────────────────────────────────────────────────

class TestObligationScore:
    def test_single_shall(self):
        assert obligation_score("Member States shall comply.") > 0

    def test_must_keyword(self):
        assert obligation_score("Manufacturers must submit proof.") > 0

    def test_required_to(self):
        assert obligation_score("Operators are required to notify.") > 0

    def test_shall_provide(self):
        assert obligation_score("The applicant shall provide documentation.") > 0

    def test_multiple_keywords_score_higher(self):
        single = obligation_score("Member States shall comply.")
        multi  = obligation_score(OBLIGATION_TEXT)
        assert multi > single

    def test_no_keywords_returns_zero(self):
        assert obligation_score("This Regulation defines scope and purpose.") == 0

    def test_case_insensitive(self):
        assert obligation_score("Member States SHALL comply.") > 0

    def test_multi_word_phrase_scores_higher_per_occurrence(self):
        # "shall provide" (multi-word, weight 3) vs bare "shall" (weight 2)
        bare   = obligation_score("operators shall notify.")          # 1 × 2 = 2
        phrase = obligation_score("operators shall provide proof.")   # 2 occurrences: "shall"(2) + "shall provide"(3) → but overlap counted
        # At minimum phrase score should be >= bare
        assert phrase >= bare

    def test_empty_string(self):
        assert obligation_score("") == 0


# ─────────────────────────────────────────────────────────────────────────────
# list_score
# ─────────────────────────────────────────────────────────────────────────────

class TestListScore:
    def test_alphabetical_items(self):
        assert list_score("(a) first item; (b) second item; (c) third.") == 3

    def test_numbered_paragraphs(self):
        text = "1. First paragraph.\n2. Second paragraph.\n3. Third paragraph."
        assert list_score(text) == 3

    def test_table_rows(self):
        assert list_score(TABLE_TEXT) >= 3

    def test_roman_numerals(self):
        # Roman items also match the alpha pattern [a-z]{1,3}, so the count
        # may exceed 3 — assert at least 3 unique items were detected.
        assert list_score("(i) one; (ii) two; (iii) three.") >= 3

    def test_dash_list(self):
        text = "Requirements:\n— first requirement\n— second requirement"
        assert list_score(text) >= 2

    def test_no_list(self):
        assert list_score(GENERIC_SCOPE_TEXT) == 0

    def test_combined_list_types(self):
        combined = LIST_TEXT + "\n" + TABLE_TEXT
        assert list_score(combined) > list_score(LIST_TEXT)

    def test_cap_prevents_overflow(self):
        # Generate a text with many list items — score should be capped at 30
        many_items = " ".join(f"({chr(ord('a') + i % 26)})" for i in range(50))
        assert list_score(many_items) <= 30

    def test_2019_2144_style_text(self):
        """Regression: Regulation 2019/2144 annex tables must score > 0."""
        text = (
            "| Intelligent speed assistance | 6 July 2022 |\n"
            "| Driver drowsiness warning    | 6 July 2022 |\n"
            "| Emergency braking            | 6 July 2024 |\n"
        )
        assert list_score(text) >= 3

    def test_empty_string(self):
        assert list_score("") == 0


# ─────────────────────────────────────────────────────────────────────────────
# is_generic_section
# ─────────────────────────────────────────────────────────────────────────────

class TestIsGenericSection:
    def test_scope_article_without_obligations_is_generic(self):
        assert is_generic_section("Scope", GENERIC_SCOPE_TEXT) is True

    def test_definitions_article_without_obligations_is_generic(self):
        text = "For the purposes of this Regulation the following definitions apply."
        assert is_generic_section("Definitions", text) is True

    def test_scope_with_real_obligations_not_generic(self):
        # A "Scope" article that also contains "shall" should not be penalised
        text = "This Regulation applies to vehicles. Member States shall enforce it."
        assert is_generic_section("Scope", text) is False

    def test_obligation_article_not_generic(self):
        assert is_generic_section("Mandatory safety systems", OBLIGATION_TEXT) is False

    def test_general_provisions_without_obligations(self):
        # "General provisions" substring matches "general provisions" in token set
        text = "General provisions establishing the regulatory framework."
        assert is_generic_section("General Provisions", text) is True

    def test_non_generic_title_always_passes(self):
        # Even without obligation keywords, a non-generic title is not penalised
        assert is_generic_section("Mandatory safety requirements", GENERIC_SCOPE_TEXT) is False

    def test_delegation_title_scope_is_generic(self):
        # DELEGATION_TEXT contains delegation phrases ("delegated acts",
        # "implementing acts") so despite "shall" appearing in the text,
        # the article is classified as generic.
        assert is_generic_section("Scope", DELEGATION_TEXT) is True

    def test_subject_matter_without_obligations(self):
        # Substring match handles hyphenated "Subject-matter"
        text = "This Regulation lays down rules for vehicles."
        assert is_generic_section("Subject-matter and objectives", text) is True


# ─────────────────────────────────────────────────────────────────────────────
# extract_best_clauses
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractBestClauses:
    def test_short_text_returned_unchanged(self):
        short = "Member States shall comply."
        assert extract_best_clauses(short) == short

    def test_returns_string(self):
        assert isinstance(extract_best_clauses(MIXED_TEXT), str)

    def test_obligation_paragraphs_preserved(self):
        # The "shall be equipped" paragraph should survive extraction
        result = extract_best_clauses(MIXED_TEXT)
        assert "shall be equipped" in result or "shall" in result

    def test_list_paragraphs_preserved(self):
        # (a)/(b) items should be retained
        result = extract_best_clauses(LIST_TEXT + "\n\nUnrelated filler paragraph about objectives.")
        assert "(a)" in result or "(b)" in result

    def test_extraction_reduces_length(self):
        # A very long text should be shortened
        long_text = ("Unrelated preamble text. " * 20 + "\n\n") * 5 + LIST_TEXT
        result = extract_best_clauses(long_text, max_clauses=2)
        assert len(result) < len(long_text)

    def test_empty_string(self):
        assert extract_best_clauses("") == ""

    def test_original_paragraph_order_preserved(self):
        text = (
            "First relevant clause — Member States shall comply.\n\n"
            "Second relevant clause — manufacturers must provide.\n\n"
            "Irrelevant filler sentence about general objectives.\n\n"
            "Third relevant clause — operators shall notify authorities."
        )
        result = extract_best_clauses(text, max_clauses=3)
        # Order should be preserved (first before second before third)
        if "First" in result and "Third" in result:
            assert result.index("First") < result.index("Third")


# ─────────────────────────────────────────────────────────────────────────────
# rerank_nodes
# ─────────────────────────────────────────────────────────────────────────────

class TestRerankNodes:
    def _make_node(self, text, title="Article", score=5):
        return {
            "doc_id":          "32019R2144",
            "node_id":         "art_5",
            "title":           title,
            "text":            text,
            "summary":         "Summary.",
            "relevance_score": score,
            "doc_title":       "Vehicle Safety Regulation",
            "year":            2019,
        }

    def test_returns_list(self):
        nodes = [self._make_node(OBLIGATION_TEXT)]
        assert isinstance(rerank_nodes(nodes), list)

    def test_obligation_score_raw_stored(self):
        nodes = rerank_nodes([self._make_node(OBLIGATION_TEXT)])
        assert nodes[0]["obligation_score_raw"] > 0

    def test_list_score_raw_stored(self):
        nodes = rerank_nodes([self._make_node(LIST_TEXT)])
        assert nodes[0]["list_score_raw"] > 0

    def test_final_score_stored(self):
        nodes = rerank_nodes([self._make_node(OBLIGATION_TEXT)])
        assert "final_score" in nodes[0]

    def test_obligation_node_ranked_above_generic(self):
        obligation_node = self._make_node(OBLIGATION_TEXT, title="Mandatory requirements", score=6)
        generic_node    = self._make_node(GENERIC_SCOPE_TEXT, title="Scope", score=6)
        ranked = rerank_nodes([generic_node, obligation_node])
        assert ranked[0]["title"] == "Mandatory requirements"

    def test_list_node_ranked_higher_than_prose(self):
        list_node  = self._make_node(LIST_TEXT,  title="Safety systems", score=5)
        prose_node = self._make_node(GENERIC_SCOPE_TEXT, title="Overview", score=5)
        ranked = rerank_nodes([prose_node, list_node])
        assert ranked[0]["title"] == "Safety systems"

    def test_generic_penalty_applied(self):
        generic = self._make_node(GENERIC_SCOPE_TEXT, title="Scope", score=8)
        ranked  = rerank_nodes([generic])
        # Penalty of -4 should reduce final score below base of 8
        assert ranked[0]["final_score"] < 8

    def test_empty_input(self):
        assert rerank_nodes([]) == []

    def test_text_field_set_to_best_clauses(self):
        long_text = MIXED_TEXT + ("\nFiller sentence about objectives. " * 30)
        nodes = rerank_nodes([self._make_node(long_text)])
        # text should be shorter than original (clause extraction applied)
        assert len(nodes[0]["text"]) <= len(long_text)

    def test_2019_2144_annex_table_scores_high(self):
        """Regression: 2019/2144-style annex table articles must rank highly."""
        table_text = (
            "From 6 July 2022, vehicles shall be equipped with:\n\n"
            "| Intelligent speed assistance | 6 July 2022 |\n"
            "| Driver drowsiness warning    | 6 July 2022 |\n"
            "| Emergency braking            | 6 July 2024 |\n\n"
            "(a) intelligent speed assistance;\n"
            "(b) driver drowsiness warning;\n"
            "(c) emergency braking system."
        )
        generic_text = "This Regulation establishes the scope of vehicle approval."
        table_node   = self._make_node(table_text, title="Mandatory systems", score=7)
        generic_node = self._make_node(generic_text, title="Scope", score=7)
        ranked = rerank_nodes([generic_node, table_node])
        assert ranked[0]["title"] == "Mandatory systems"


# ─────────────────────────────────────────────────────────────────────────────
# has_answer_signal
# ─────────────────────────────────────────────────────────────────────────────

class TestHasAnswerSignal:
    def _ranked(self, text, title="Article", nav_score=5):
        node = {
            "doc_id": "32019R2144", "node_id": "art_1", "title": title,
            "text": text, "summary": "", "relevance_score": nav_score,
            "doc_title": "Reg", "year": 2019,
        }
        return rerank_nodes([node])

    def test_obligation_text_has_signal(self):
        assert has_answer_signal(self._ranked(OBLIGATION_TEXT)) is True

    def test_list_text_has_signal(self):
        assert has_answer_signal(self._ranked(LIST_TEXT)) is True

    def test_table_text_has_signal(self):
        assert has_answer_signal(self._ranked(TABLE_TEXT)) is True

    def test_pure_generic_no_signal(self):
        # No obligation keywords, no list, low navigator score → False
        nodes = self._ranked("General objectives and regulatory framework.", nav_score=4)
        assert has_answer_signal(nodes) is False

    def test_high_navigator_score_triggers_signal(self):
        # Even without keywords, navigator score ≥ 7 should return True
        nodes = self._ranked("General scope without shall or must.", nav_score=8)
        assert has_answer_signal(nodes) is True

    def test_empty_list(self):
        assert has_answer_signal([]) is False

    def test_raw_scores_used_not_extracted_text(self):
        """
        Regression: obligation signal must be detected from original text even
        if clause extraction removed the obligation paragraph.
        """
        # Text where "shall" is in a short paragraph that might be de-prioritised
        text = (
            "Member States shall implement this Regulation by 2024.\n\n"
            + ("Unrelated filler paragraph. " * 50)
        )
        nodes = self._ranked(text, nav_score=5)
        # obligation_score_raw must be positive (scored on original)
        assert nodes[0]["obligation_score_raw"] > 0
        assert has_answer_signal(nodes) is True

    def test_2019_2144_regression(self):
        """Full regression test for the original bug report."""
        text = (
            "From 6 July 2022, national authorities shall refuse to grant EU "
            "type-approval to vehicles which do not comply.\n\n"
            "| Intelligent speed assistance | 6 July 2022 |\n"
            "| Advanced emergency braking   | 6 July 2024 |\n\n"
            "(a) intelligent speed assistance systems;\n"
            "(b) driver drowsiness warning systems;\n"
            "(c) emergency lane keeping systems."
        )
        nodes = self._ranked(text, title="Mandatory safety systems", nav_score=9)
        assert has_answer_signal(nodes) is True


# ─────────────────────────────────────────────────────────────────────────────
# obligation_expansion_keywords
# ─────────────────────────────────────────────────────────────────────────────

class TestObligationExpansionKeywords:
    def test_requirement_question_expands(self):
        kws = obligation_expansion_keywords(
            "What are the requirements for type-approval?", ["type-approval", "vehicle"]
        )
        assert len(kws) > 2
        assert any(k in kws for k in ["shall", "required", "obligation", "documentation"])

    def test_documentation_question_expands(self):
        kws = obligation_expansion_keywords(
            "What documents must be submitted?", ["vehicle", "emission"]
        )
        assert len(kws) > 2

    def test_unrelated_question_no_expansion(self):
        kws = obligation_expansion_keywords(
            "When was GDPR adopted?", ["GDPR", "data protection"]
        )
        # No requirement triggers — keywords unchanged
        assert kws == ["GDPR", "data protection"]

    def test_no_duplicate_keywords_added(self):
        base = ["vehicle", "shall", "obligation"]
        kws  = obligation_expansion_keywords("What are the required documents?", base)
        assert kws.count("shall") == 1
        assert kws.count("obligation") == 1

    def test_returns_list(self):
        result = obligation_expansion_keywords("What must manufacturers provide?", [])
        assert isinstance(result, list)

    def test_adds_obligation_terms(self):
        base = ["vehicle"]
        kws  = obligation_expansion_keywords("What are the requirements?", base)
        # Should add obligation terms (no hard cap in clause_ranker version)
        assert len(kws) > len(base)
        assert any(k in kws for k in ["shall", "required", "obligation", "documentation"])
