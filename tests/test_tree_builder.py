"""
Tests for pipeline/tree_builder.py

Covers:
  - parse_articles()          — Markdown → article node list
  - obligation_score / list_score pre-computed at parse time
"""

import pytest
from pipeline.tree_builder import parse_articles


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_MARKDOWN = """
Article 1
Scope

This Regulation establishes rules for vehicle type-approval.

Article 2
Definitions

For the purposes of this Regulation, the following definitions apply.

Article 5
Mandatory safety systems

From 6 July 2022, vehicles shall be equipped with:

(a) intelligent speed assistance;
(b) driver drowsiness warning;
(c) advanced emergency braking.

Member States must ensure compliance by that date.

Article 12
Penalties

Member States shall lay down rules on penalties applicable to infringements.
Penalties shall be effective, proportionate and dissuasive.
"""

ONE_ARTICLE_MARKDOWN = """
Article 1
Scope

This Regulation defines the scope of vehicle approvals.
"""

ANNEX_MARKDOWN = """
Article 6
Technical requirements

The following table lists mandatory systems:

| System | Applicable from |
| Speed assistance | 2022 |
| Emergency braking | 2024 |
"""


# ─────────────────────────────────────────────────────────────────────────────
# parse_articles — structure
# ─────────────────────────────────────────────────────────────────────────────

class TestParseArticles:
    def test_returns_list(self):
        assert isinstance(parse_articles(SAMPLE_MARKDOWN), list)

    def test_correct_article_count(self):
        articles = parse_articles(SAMPLE_MARKDOWN)
        assert len(articles) == 4  # Articles 1, 2, 5, 12

    def test_article_has_required_keys(self):
        articles = parse_articles(SAMPLE_MARKDOWN)
        for a in articles:
            assert "id"     in a
            assert "number" in a
            assert "title"  in a
            assert "text"   in a

    def test_article_ids_match_numbers(self):
        articles = parse_articles(SAMPLE_MARKDOWN)
        for a in articles:
            assert a["id"] == f"art_{a['number']}"

    def test_article_numbers_correct(self):
        articles = parse_articles(SAMPLE_MARKDOWN)
        numbers  = [a["number"] for a in articles]
        assert "1" in numbers
        assert "5" in numbers
        assert "12" in numbers

    def test_title_extracted(self):
        articles = parse_articles(SAMPLE_MARKDOWN)
        art5 = next(a for a in articles if a["number"] == "5")
        assert "Mandatory" in art5["title"] or "safety" in art5["title"].lower()

    def test_body_text_populated(self):
        articles = parse_articles(SAMPLE_MARKDOWN)
        art5 = next(a for a in articles if a["number"] == "5")
        assert len(art5["text"]) > 10

    def test_text_length_capped_at_6000(self):
        # Cap raised from 4000 → 6000 to preserve complex articles and annex refs
        huge_text = "Article 99\nHuge article\n\n" + ("word " * 3000)
        articles  = parse_articles(huge_text)
        assert all(len(a["text"]) <= 6000 for a in articles)

    def test_fallback_for_no_articles(self):
        articles = parse_articles("No article headers here at all.")
        assert len(articles) == 1
        assert articles[0]["id"] == "doc_body"

    def test_single_article(self):
        articles = parse_articles(ONE_ARTICLE_MARKDOWN)
        assert len(articles) == 1
        assert articles[0]["number"] == "1"


# ─────────────────────────────────────────────────────────────────────────────
# Pre-computed obligation / list scores (added by clause_ranker integration)
# ─────────────────────────────────────────────────────────────────────────────

class TestNodeSignals:
    def test_obligation_score_key_present(self):
        articles = parse_articles(SAMPLE_MARKDOWN)
        for a in articles:
            assert "obligation_score" in a

    def test_list_score_key_present(self):
        articles = parse_articles(SAMPLE_MARKDOWN)
        for a in articles:
            assert "list_score" in a

    def test_obligation_article_has_positive_score(self):
        articles = parse_articles(SAMPLE_MARKDOWN)
        art5 = next(a for a in articles if a["number"] == "5")
        assert art5["obligation_score"] > 0

    def test_list_article_has_positive_list_score(self):
        articles = parse_articles(SAMPLE_MARKDOWN)
        art5 = next(a for a in articles if a["number"] == "5")
        assert art5["list_score"] > 0

    def test_scope_article_low_obligation_score(self):
        articles  = parse_articles(SAMPLE_MARKDOWN)
        art1 = next(a for a in articles if a["number"] == "1")
        # Scope article should have lower obligation score than Article 5
        art5 = next(a for a in articles if a["number"] == "5")
        assert art1["obligation_score"] <= art5["obligation_score"]

    def test_annex_table_article_has_list_score(self):
        articles = parse_articles(ANNEX_MARKDOWN)
        assert articles[0]["list_score"] > 0

    def test_scores_are_non_negative_integers(self):
        articles = parse_articles(SAMPLE_MARKDOWN)
        for a in articles:
            assert isinstance(a["obligation_score"], int)
            assert isinstance(a["list_score"], int)
            assert a["obligation_score"] >= 0
            assert a["list_score"] >= 0
