"""
Tests for pipeline/keyword_extractor.py

Covers:
  - _extract_exact_signals()  — regex extraction of regulation/article references
  - _expand_obligation_keywords() — query expansion for requirement questions
"""

import pytest
from pipeline.keyword_extractor import _extract_exact_signals, _expand_obligation_keywords


# ─────────────────────────────────────────────────────────────────────────────
# _extract_exact_signals
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractExactSignals:
    def test_regulation_year_slash_number(self):
        signals = _extract_exact_signals("Under Regulation 2019/631 CO2 targets apply.")
        assert "2019/631" in signals

    def test_regulation_number_slash_year(self):
        signals = _extract_exact_signals("See Regulation 715/2007 for emission standards.")
        assert "715/2007" in signals

    def test_regulation_with_eu_suffix(self):
        signals = _extract_exact_signals("Directive 2014/65/EU on markets in financial instruments.")
        assert "2014/65" in signals

    def test_article_number_full_word(self):
        signals = _extract_exact_signals("What does Article 73 require?")
        assert "article 73" in signals

    def test_article_abbreviation(self):
        signals = _extract_exact_signals("Requirements under Art. 5 of GDPR.")
        assert "article 5" in signals

    def test_article_range(self):
        signals = _extract_exact_signals("Articles 10-12 set out the procedure.")
        assert any("article" in s for s in signals)

    def test_both_regulation_and_article(self):
        signals = _extract_exact_signals(
            "What does Article 73 of Regulation 2019/631 require?"
        )
        assert "2019/631" in signals
        assert "article 73" in signals

    def test_no_signals_in_plain_question(self):
        signals = _extract_exact_signals("What are the general data protection rules?")
        assert signals == []

    def test_multiple_regulations(self):
        signals = _extract_exact_signals(
            "How do Regulations 2019/631 and 2019/2144 relate?"
        )
        assert "2019/631" in signals
        assert "2019/2144" in signals

    def test_returns_list(self):
        assert isinstance(_extract_exact_signals("Article 5 GDPR"), list)


# ─────────────────────────────────────────────────────────────────────────────
# _expand_obligation_keywords
# ─────────────────────────────────────────────────────────────────────────────

class TestExpandObligationKeywords:
    def test_requirement_question_adds_terms(self):
        kws = _expand_obligation_keywords("What are the requirements?", ["vehicle"])
        assert len(kws) > 1

    def test_documentation_question_adds_terms(self):
        kws = _expand_obligation_keywords("What documents are required?", ["vehicle"])
        assert any(k in kws for k in ["shall", "required", "obligation", "documentation"])

    def test_non_requirement_question_unchanged(self):
        base = ["data protection", "GDPR"]
        kws  = _expand_obligation_keywords("When was GDPR adopted?", base)
        assert kws == base

    def test_no_duplicates_added(self):
        base = ["shall", "vehicle"]
        kws  = _expand_obligation_keywords("What are the requirements?", base)
        assert kws.count("shall") == 1

    def test_max_two_additions(self):
        base = ["vehicle"]
        kws  = _expand_obligation_keywords("What are the required documents?", base)
        assert len(kws) <= len(base) + 2

    def test_empty_base_keywords(self):
        kws = _expand_obligation_keywords("What must manufacturers provide?", [])
        assert isinstance(kws, list)

    def test_mandatory_trigger(self):
        kws = _expand_obligation_keywords("What is mandatory for type-approval?", ["approval"])
        assert len(kws) > 1
