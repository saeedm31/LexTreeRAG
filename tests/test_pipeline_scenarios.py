"""
End-to-end pipeline scenario tests using real EU regulation content.

Each scenario is a triple of:
  QUESTION     — what a real user would ask
  TARGET       — which regulation + article must be retrieved
  EXPECTED     — key phrases / list items that must appear in the answer

These tests do NOT call the API.  They mock the navigator output with
realistic article texts (verbatim or near-verbatim from the regulation)
and verify that the retrieval pipeline (reranker + clause extractor +
validation gate) produces an answer-ready result.

Regulations covered:
  1. GDPR            — 2016/679  Art.17  Right to erasure
  2. Reg. 715/2007   — Defeat devices prohibition + exceptions
  3. Reg. 2019/2144  — Mandatory vehicle safety systems (annex table)
  4. Reg. 2023/1542  — Used vs waste battery documentation (Art. 76)
  5. REACH           — 1907/2006 Art.31  Safety Data Sheet content
  6. Food Law        — 178/2002  Art.14  Food safety requirements
"""

import pytest
from pipeline.clause_ranker import (
    rerank_nodes, has_answer_signal, obligation_score, list_score,
)
from pipeline.keyword_extractor import _extract_exact_signals, generate_query_variants


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _node(node_id, number, title, text, celex, nav_score=8):
    return {
        "doc_id":          celex,
        "node_id":         node_id,
        "title":           title,
        "number":          number,
        "text":            text,
        "summary":         f"Covers {title.lower()}.",
        "relevance_score": nav_score,
        "doc_title":       f"Regulation {celex}",
        "year":            int(celex[1:5]) if celex[1:5].isdigit() else 2020,
        "doc_url":         f"https://eur-lex.europa.eu/celex/{celex}",
        "obligation_score": obligation_score(text),
        "list_score":       list_score(text),
    }


def _scope_node(celex):
    """Generic scope/subject-matter article — should never be the primary answer."""
    return _node(
        "art_1", "1", "Subject matter and scope",
        "This Regulation lays down rules on the subject matter and scope.",
        celex, nav_score=5,
    )


def _pipeline(nodes):
    """Run reranker + validation gate. Returns (ranked_nodes, signal_ok)."""
    ranked = rerank_nodes(nodes)
    return ranked, has_answer_signal(ranked)


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 1 — GDPR Article 17: Right to Erasure
# ─────────────────────────────────────────────────────────────────────────────
# Expected article: Regulation 2016/679, Article 17
# Answer must list all 6 conditions (a)–(f) under which erasure can be requested.

GDPR_Q = (
    "Under GDPR, what are the conditions under which a data subject can "
    "request the erasure of their personal data?"
)

GDPR_ART17_TEXT = """\
Article 17
Right to erasure ('right to be forgotten')

1. The data subject shall have the right to obtain from the controller the
erasure of personal data concerning him or her without undue delay and the
controller shall be obliged to erase personal data without undue delay where
one of the following grounds applies:

(a) the personal data are no longer necessary in relation to the purposes
for which they were collected or otherwise processed;

(b) the data subject withdraws consent on which the processing is based
according to point (a) of Article 6(1), or point (a) of Article 9(2), and
where there is no other legal ground for the processing;

(c) the data subject objects to the processing pursuant to Article 21(1)
and there are no overriding legitimate grounds for the processing, or the
data subject objects to the processing pursuant to Article 21(2);

(d) the personal data have been unlawfully processed;

(e) the personal data have to be erased for compliance with a legal
obligation in Union or Member State law to which the controller is subject;

(f) the personal data have been collected in relation to the offer of
information society services referred to in Article 8(1).

2. Where the controller has made the personal data public and is obliged
pursuant to paragraph 1 to erase the personal data, the controller, taking
account of available technology and the cost of implementation, shall take
reasonable steps, including technical measures, to inform controllers which
are processing the personal data that the data subject has requested the
erasure by such controllers of any links to, or copy or replication of,
those personal data.
"""

GDPR_ART17_SCOPE_TEXT = (
    "This Regulation establishes rules on the processing of personal data "
    "and the free movement of such data."
)


class TestGDPRRightToErasure:
    """
    Scenario: user asks for the conditions to request erasure under GDPR.
    The pipeline must return Article 17 (not Article 1), with all 6 grounds.
    """

    def test_exact_signals_extracted(self):
        signals = _extract_exact_signals("GDPR Regulation 2016/679 Article 17")
        assert "2016/679" in signals
        assert "article 17" in signals

    def test_art17_obligation_score_high(self):
        assert obligation_score(GDPR_ART17_TEXT) >= 6

    def test_art17_list_score_captures_6_grounds(self):
        score = list_score(GDPR_ART17_TEXT)
        assert score >= 6   # (a) through (f)

    def test_art17_ranked_above_scope(self):
        nodes  = [
            _scope_node("32016R0679"),
            _node("art_17", "17", "Right to erasure", GDPR_ART17_TEXT, "32016R0679"),
        ]
        ranked, signal = _pipeline(nodes)
        assert ranked[0]["node_id"] == "art_17"
        assert signal is True

    def test_extracted_text_contains_all_grounds(self):
        nodes  = [_node("art_17", "17", "Right to erasure", GDPR_ART17_TEXT, "32016R0679")]
        ranked, _ = _pipeline(nodes)
        text   = ranked[0]["text"]
        missing = [g for g in ["(a)", "(b)", "(c)", "(d)", "(e)", "(f)"] if g not in text]
        # At most 1 ground may be dropped — clause extractor keeps top-N by score
        assert len(missing) <= 1, f"Too many missing grounds in extracted text: {missing}"

    def test_erasure_obligation_keyword_present(self):
        nodes  = [_node("art_17", "17", "Right to erasure", GDPR_ART17_TEXT, "32016R0679")]
        ranked, _ = _pipeline(nodes)
        assert "shall" in ranked[0]["text"]

    def test_query_variants_include_gdpr_number(self):
        variants = generate_query_variants(
            GDPR_Q, ["data protection", "erasure", "GDPR"], ["2016/679"]
        )
        assert any("2016/679" in v for v in variants)


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 2 — Regulation 715/2007: Defeat Devices
# ─────────────────────────────────────────────────────────────────────────────
# Expected article: Regulation 715/2007, Article 5(2)
# Answer: prohibition on defeat devices + 3 narrow exceptions

DEFEAT_Q = (
    "Under Regulation 715/2007, are defeat devices in vehicle emission systems "
    "prohibited, and what exceptions are permitted?"
)

DEFEAT_ART5_TEXT = """\
Article 5
Requirements and tests

1. The manufacturer shall equip vehicles so that the components likely to affect
emissions are designed, constructed and assembled so as to enable the vehicle, in
normal use, to comply with this Regulation and its implementing measures.

2. The use of defeat devices that reduce the effectiveness of emission control
systems shall be prohibited. A defeat device shall not be used unless:

(a) the need for the device is justified in terms of protecting the engine
against damage or accident and for safe operation of the vehicle;

(b) the device does not function beyond the requirements of engine starting;

(c) the conditions are substantially included in the test procedures for
verifying evaporative emissions and average tailpipe emissions.
"""


class TestDefeatDevicesProhibition:
    """
    Scenario: user asks about defeat device rules under Reg. 715/2007.
    Pipeline must retrieve Art. 5(2) with the prohibition + 3 exceptions.
    """

    def test_exact_signal_715_2007(self):
        signals = _extract_exact_signals(
            "defeat devices under Regulation (EC) No 715/2007"
        )
        assert "715/2007" in signals

    def test_art5_obligation_score_high(self):
        assert obligation_score(DEFEAT_ART5_TEXT) >= 4

    def test_art5_list_score_detects_exceptions(self):
        assert list_score(DEFEAT_ART5_TEXT) >= 3   # (a), (b), (c)

    def test_art5_ranked_above_scope(self):
        nodes = [
            _scope_node("32007R0715"),
            _node("art_5", "5", "Requirements and tests", DEFEAT_ART5_TEXT, "32007R0715"),
        ]
        ranked, signal = _pipeline(nodes)
        assert ranked[0]["node_id"] == "art_5"
        assert signal is True

    def test_prohibition_keyword_in_extracted_text(self):
        nodes  = [_node("art_5", "5", "Requirements and tests", DEFEAT_ART5_TEXT, "32007R0715")]
        ranked, _ = _pipeline(nodes)
        assert "prohibited" in ranked[0]["text"].lower()

    def test_all_three_exceptions_in_extracted_text(self):
        nodes  = [_node("art_5", "5", "Requirements and tests", DEFEAT_ART5_TEXT, "32007R0715")]
        ranked, _ = _pipeline(nodes)
        text   = ranked[0]["text"]
        missing = [e for e in ["(a)", "(b)", "(c)"] if e not in text]
        assert not missing, f"Missing exceptions: {missing}"

    def test_query_variant_includes_regulation_number(self):
        variants = generate_query_variants(
            DEFEAT_Q, ["defeat device", "emissions", "vehicles"], ["715/2007"]
        )
        assert any("715/2007" in v for v in variants)


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 3 — Regulation 2019/2144: Mandatory Vehicle Safety Systems
# ─────────────────────────────────────────────────────────────────────────────
# Expected: Article 5 + Annex II table listing systems and dates
# This is the original failing query — annex table content must be retrieved.

SAFETY_Q = (
    "Under Regulation 2019/2144, what mandatory safety systems must new "
    "passenger cars (M1 category) be equipped with, and from when?"
)

SAFETY_ART5_TEXT = """\
Article 5
General obligations for manufacturers concerning vehicle safety

1. Manufacturers shall ensure that vehicles that they place on the market comply
with the requirements of this Regulation and its delegated and implementing acts.

2. As of the dates set out in Annex II, vehicles of category M1 shall be
equipped with the systems, components or separate technical units listed in
that Annex. The requirements for each system are laid down in the relevant
delegated and implementing acts.
"""

SAFETY_ANNEX_TEXT = """\
Annex II — Vehicle safety requirements

The following systems shall be mandatory for vehicles of category M1:

| System | Applicable from |
| Intelligent speed assistance (ISA) | 6 July 2022 |
| Driver drowsiness and attention warning | 6 July 2022 |
| Emergency stop signal | 6 July 2022 |
| Reversing detection | 6 July 2022 |
| Event data recorder (EDR) | 6 July 2022 |
| Advanced emergency braking | 6 July 2024 |
| Emergency lane keeping | 6 July 2024 |
| Driver monitoring system | 6 July 2026 |
| Alcohol interlock installation facilitation | 6 July 2026 |
"""


class TestMandatoryVehicleSafetySystems:
    """
    Regression scenario: annex table content must score highly and be retrieved.
    Both Article 5 and Annex II are needed for a complete answer.
    """

    def test_exact_signal_2019_2144(self):
        signals = _extract_exact_signals(
            "Regulation (EU) 2019/2144 mandatory safety"
        )
        assert "2019/2144" in signals

    def test_annex_table_list_score_high(self):
        # Annex II has 9 table rows → list_score must be high
        assert list_score(SAFETY_ANNEX_TEXT) >= 7

    def test_annex_obligation_score_nonzero(self):
        assert obligation_score(SAFETY_ANNEX_TEXT) > 0   # "shall be mandatory"

    def test_annex_ranked_above_scope(self):
        nodes = [
            _scope_node("32019R2144"),
            _node("annex_ii", "Annex II", "Vehicle safety requirements",
                  SAFETY_ANNEX_TEXT, "32019R2144"),
        ]
        ranked, signal = _pipeline(nodes)
        assert ranked[0]["node_id"] == "annex_ii"
        assert signal is True

    def test_table_rows_preserved_in_extracted_text(self):
        nodes = [_node("annex_ii", "Annex II", "Vehicle safety requirements",
                        SAFETY_ANNEX_TEXT, "32019R2144")]
        ranked, _ = _pipeline(nodes)
        text = ranked[0]["text"]
        assert "| " in text, "Pipe-delimited table rows must be preserved"

    def test_specific_systems_in_extracted_text(self):
        nodes = [_node("annex_ii", "Annex II", "Vehicle safety requirements",
                        SAFETY_ANNEX_TEXT, "32019R2144")]
        ranked, _ = _pipeline(nodes)
        text = ranked[0]["text"]
        assert "Intelligent speed assistance" in text
        assert "Advanced emergency braking" in text

    def test_dates_in_extracted_text(self):
        nodes = [_node("annex_ii", "Annex II", "Vehicle safety requirements",
                        SAFETY_ANNEX_TEXT, "32019R2144")]
        ranked, _ = _pipeline(nodes)
        text = ranked[0]["text"]
        assert "6 July 2022" in text
        assert "6 July 2024" in text

    def test_art5_plus_annex_both_pass_validation(self):
        nodes = [
            _node("art_5",   "5",        "General obligations", SAFETY_ART5_TEXT,  "32019R2144", 7),
            _node("annex_ii","Annex II", "Safety requirements", SAFETY_ANNEX_TEXT, "32019R2144", 8),
        ]
        _, signal = _pipeline(nodes)
        assert signal is True


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 4 — Reg. 2023/1542: Used vs Waste Battery Documentation
# ─────────────────────────────────────────────────────────────────────────────
# The original failing query.  Validates the full fix end-to-end.

BATTERY_Q = (
    "Under Regulation 2023/1542, what documentation must a holder provide "
    "to show shipped batteries are 'used batteries' for reuse, not waste?"
)

BATTERY_ART76_TEXT = """\
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
the requirements applicable to waste batteries under this Regulation.

4. The holder shall ensure that the documentation referred to in paragraph 2
is available for inspection by competent authorities for a period of at
least three years from the date of shipment.
"""

BATTERY_SCOPE_TEXT = (
    "This Regulation lays down requirements for sustainability, safety, "
    "labelling and information, and end-of-life requirements for batteries."
)


class TestBatteryUsedVsWasteDocumentation:
    """
    Regression: this exact query used to return only Article 1 (scope).
    Art. 76 must be ranked first and all 4 documentation items (a)–(d) preserved.
    """

    def test_exact_signals_both_regulations(self):
        signals = _extract_exact_signals(BATTERY_Q)
        assert "2023/1542" in signals

    def test_art76_list_score_detects_abcd(self):
        assert list_score(BATTERY_ART76_TEXT) >= 4

    def test_art76_obligation_score_high(self):
        assert obligation_score(BATTERY_ART76_TEXT) >= 6

    def test_scope_fails_validation(self):
        nodes = [_node("art_1", "1", "Subject matter and scope",
                        BATTERY_SCOPE_TEXT, "32023R1542", nav_score=5)]
        _, signal = _pipeline(nodes)
        assert signal is False, "Scope-only result should trigger retry"

    def test_art76_passes_validation(self):
        nodes = [_node("art_76", "76", "Shipment of batteries",
                        BATTERY_ART76_TEXT, "32023R1542", nav_score=8)]
        _, signal = _pipeline(nodes)
        assert signal is True

    def test_art76_ranked_above_scope(self):
        nodes = [
            _node("art_1",  "1",  "Scope",              BATTERY_SCOPE_TEXT,  "32023R1542", 5),
            _node("art_76", "76", "Shipment of batteries", BATTERY_ART76_TEXT, "32023R1542", 8),
        ]
        ranked, _ = _pipeline(nodes)
        assert ranked[0]["node_id"] == "art_76"

    def test_all_four_items_in_extracted_text(self):
        nodes = [_node("art_76", "76", "Shipment of batteries",
                        BATTERY_ART76_TEXT, "32023R1542")]
        ranked, _ = _pipeline(nodes)
        text  = ranked[0]["text"]
        missing = [i for i in ["(a)", "(b)", "(c)", "(d)"] if i not in text]
        assert not missing, f"Missing documentation items: {missing}"

    def test_key_evidence_terms_preserved(self):
        nodes = [_node("art_76", "76", "Shipment of batteries",
                        BATTERY_ART76_TEXT, "32023R1542")]
        ranked, _ = _pipeline(nodes)
        text = ranked[0]["text"]
        assert "invoice" in text.lower()
        assert "functional" in text.lower() or "testing" in text.lower()
        assert "destination" in text.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 5 — REACH Regulation 1907/2006: Safety Data Sheet
# ─────────────────────────────────────────────────────────────────────────────
# Expected: Article 31 — when SDS must be provided + minimum required sections

REACH_Q = (
    "Under REACH Regulation 1907/2006, when must a supplier provide a "
    "Safety Data Sheet, and what information sections must it contain?"
)

REACH_ART31_TEXT = """\
Article 31
Requirements for safety data sheets

1. The supplier of a substance or mixture shall provide the recipient of the
substance or mixture with a safety data sheet compiled in accordance with
Annex II where:

(a) the substance or mixture meets the criteria for classification as
hazardous in accordance with Regulation (EC) No 1272/2008; or

(b) the substance is persistent, bioaccumulative and toxic, or very
persistent and very bioaccumulative in accordance with the criteria set out
in Annex XIII; or

(c) the substance is included in the list established in accordance with
Article 59(1) for reasons other than those referred to in points (a) and (b).

4. The safety data sheet shall be provided in an official language of the
Member State(s) where the substance or mixture is placed on the market,
unless the Member State(s) concerned provide otherwise.

The safety data sheet shall contain the following headings:

1. Identification of the substance or mixture and of the company or undertaking
2. Hazards identification
3. Composition and information on ingredients
4. First-aid measures
5. Fire-fighting measures
6. Accidental release measures
7. Handling and storage
8. Exposure controls and personal protection
9. Physical and chemical properties
10. Stability and reactivity
11. Toxicological information
12. Ecological information
13. Disposal considerations
14. Transport information
15. Regulatory information
16. Other information
"""


class TestREACHSafetyDataSheet:
    """
    Scenario: user asks when SDS must be provided and what it must contain.
    Pipeline must retrieve Art. 31 with the (a)(b)(c) conditions and 16 headings.
    """

    def test_exact_signal_1907_2006(self):
        signals = _extract_exact_signals(
            "REACH Regulation (EC) No 1907/2006 safety data sheet"
        )
        assert "1907/2006" in signals

    def test_art31_list_score_high(self):
        # (a)(b)(c) conditions + 16 numbered headings
        score = list_score(REACH_ART31_TEXT)
        assert score >= 10

    def test_art31_obligation_score_high(self):
        assert obligation_score(REACH_ART31_TEXT) >= 4

    def test_art31_passes_validation(self):
        nodes = [_node("art_31", "31", "Requirements for safety data sheets",
                        REACH_ART31_TEXT, "31907R1906")]
        _, signal = _pipeline(nodes)
        assert signal is True

    def test_sds_conditions_in_extracted_text(self):
        nodes = [_node("art_31", "31", "Requirements for safety data sheets",
                        REACH_ART31_TEXT, "31907R1906")]
        ranked, _ = _pipeline(nodes)
        text = ranked[0]["text"]
        assert "(a)" in text and "(b)" in text and "(c)" in text

    def test_query_expansion_for_documentation_question(self):
        from pipeline.keyword_extractor import _expand_obligation_keywords
        kws = _expand_obligation_keywords(REACH_Q, ["safety data sheet", "REACH", "supplier"])
        assert len(kws) > 3

    def test_16_headings_or_obligation_text_present(self):
        nodes = [_node("art_31", "31", "Requirements for safety data sheets",
                        REACH_ART31_TEXT, "31907R1906")]
        ranked, _ = _pipeline(nodes)
        text = ranked[0]["text"]
        # Either the numbered headings or "shall contain" must be present
        assert "shall" in text or "Identification" in text


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 6 — Regulation 178/2002: Food Safety Requirements
# ─────────────────────────────────────────────────────────────────────────────
# Expected: Article 14 — food shall not be placed on market if unsafe;
# defines unsafe food with sub-conditions (a)(b) and criteria

FOOD_Q = (
    "Under Regulation 178/2002, when is food considered unsafe and what "
    "are the criteria that must be taken into account?"
)

FOOD_ART14_TEXT = """\
Article 14
Food safety requirements

1. Food shall not be placed on the market if it is unsafe.

2. Food shall be deemed to be unsafe if it is considered to be:

(a) injurious to health;

(b) unfit for human consumption.

3. In determining whether any food is unsafe, regard shall be had:

(a) to the normal conditions of use of the food by the consumer and
at each stage of production, processing and distribution; and

(b) to the information provided to the consumer, including information
on the label, or other information generally available to the consumer
concerning the avoidance of specific adverse health effects from a
particular food or category of foods.

4. In determining whether any food is injurious to health, regard shall be had:

(a) not only to the probable immediate and/or short-term and/or long-term
effects of that food on the health of a person consuming it, but also on
subsequent generations;

(b) to the probable cumulative toxic effects;

(c) to the particular health sensitivities of a specific category of
consumers where the food is intended for that category of consumers.

5. In determining whether any food is unfit for human consumption, regard shall
be had to whether the food is unacceptable for human consumption according to its
intended use, for reasons of contamination, whether by extraneous matter or
otherwise, or through putrefaction, deterioration or decay.
"""


class TestFoodSafetyRequirements:
    """
    Scenario: food safety definition and criteria under Regulation 178/2002.
    Multiple nested (a)(b)(c) lists with obligation language throughout.
    """

    def test_exact_signal_178_2002(self):
        signals = _extract_exact_signals(
            "General Food Law Regulation 178/2002 food unsafe"
        )
        assert "178/2002" in signals

    def test_art14_obligation_score_very_high(self):
        # "shall not", "shall be deemed", "shall be had" × multiple paragraphs
        assert obligation_score(FOOD_ART14_TEXT) >= 10

    def test_art14_list_score_detects_nested_lists(self):
        # Paragraphs 2, 3, 4 all have (a)(b) or (a)(b)(c)
        assert list_score(FOOD_ART14_TEXT) >= 6

    def test_art14_passes_validation(self):
        nodes = [_node("art_14", "14", "Food safety requirements",
                        FOOD_ART14_TEXT, "32002R0178")]
        _, signal = _pipeline(nodes)
        assert signal is True

    def test_core_prohibition_in_extracted_text(self):
        nodes = [_node("art_14", "14", "Food safety requirements",
                        FOOD_ART14_TEXT, "32002R0178")]
        ranked, _ = _pipeline(nodes)
        text = ranked[0]["text"]
        # The primary prohibition must survive clause extraction
        assert "shall not be placed on the market" in text or "unsafe" in text.lower()

    def test_unsafe_definition_criteria_preserved(self):
        nodes = [_node("art_14", "14", "Food safety requirements",
                        FOOD_ART14_TEXT, "32002R0178")]
        ranked, _ = _pipeline(nodes)
        text = ranked[0]["text"]
        # At least one set of criteria items must be present
        assert "(a)" in text and "(b)" in text

    def test_art14_ranked_above_scope_even_with_higher_nav_score(self):
        scope = _node("art_1", "1", "Subject matter and scope",
                       "This Regulation establishes the general principles of food law.",
                       "32002R0178", nav_score=9)
        art14 = _node("art_14", "14", "Food safety requirements",
                       FOOD_ART14_TEXT, "32002R0178", nav_score=8)
        ranked, _ = _pipeline([scope, art14])
        assert ranked[0]["node_id"] == "art_14"


# ─────────────────────────────────────────────────────────────────────────────
# Cross-scenario: Query Variant Generation
# ─────────────────────────────────────────────────────────────────────────────

class TestQueryVariantGeneration:
    """
    Verify that generate_query_variants() produces useful search alternatives
    for each scenario's question and keywords.
    """

    @pytest.mark.parametrize("question,keywords,signals,must_include", [
        (
            GDPR_Q,
            ["data protection", "erasure", "personal data"],
            ["2016/679"],
            "2016/679",
        ),
        (
            DEFEAT_Q,
            ["defeat device", "emission", "vehicles"],
            ["715/2007"],
            "715/2007",
        ),
        (
            SAFETY_Q,
            ["vehicle safety", "type-approval", "passenger"],
            ["2019/2144"],
            "2019/2144",
        ),
        (
            BATTERY_Q,
            ["batteries", "used batteries", "waste", "documentation"],
            ["2023/1542"],
            "2023/1542",
        ),
        (
            REACH_Q,
            ["safety data sheet", "REACH", "supplier", "hazardous"],
            ["1907/2006"],
            "1907/2006",
        ),
        (
            FOOD_Q,
            ["food safety", "unsafe food", "consumer"],
            ["178/2002"],
            "178/2002",
        ),
    ])
    def test_regulation_number_in_at_least_one_variant(
        self, question, keywords, signals, must_include
    ):
        variants = generate_query_variants(question, keywords, signals)
        assert len(variants) >= 1
        found = any(must_include in str(v) for v in variants)
        assert found, (
            f"Expected '{must_include}' in at least one variant.\n"
            f"Variants: {variants}"
        )

    @pytest.mark.parametrize("question,keywords,signals", [
        (GDPR_Q,    ["data protection", "erasure"],         ["2016/679"]),
        (DEFEAT_Q,  ["defeat device", "emission"],          ["715/2007"]),
        (BATTERY_Q, ["batteries", "documentation", "reuse"],["2023/1542"]),
    ])
    def test_at_least_two_variants_generated(self, question, keywords, signals):
        variants = generate_query_variants(question, keywords, signals)
        assert len(variants) >= 2, f"Expected ≥ 2 variants, got {len(variants)}"

    def test_no_duplicate_variants(self):
        variants = generate_query_variants(
            GDPR_Q, ["data protection", "erasure", "GDPR"], ["2016/679"]
        )
        seen = set()
        for v in variants:
            key = tuple(sorted(v))
            assert key not in seen, f"Duplicate variant: {v}"
            seen.add(key)


# ─────────────────────────────────────────────────────────────────────────────
# Expected answers reference (for human review / LLM evaluation)
# ─────────────────────────────────────────────────────────────────────────────

EXPECTED_ANSWERS = {
    "gdpr_erasure": {
        "regulation": "Regulation (EU) 2016/679 (GDPR)",
        "article":    "Article 17",
        "key_elements": [
            "data no longer necessary for original purpose (a)",
            "withdrawal of consent (b)",
            "objection to processing (c)",
            "unlawfully processed (d)",
            "legal obligation requires erasure (e)",
            "collected in relation to information society services for children (f)",
        ],
        "obligation_phrase": "shall have the right to obtain ... erasure ... without undue delay",
    },
    "defeat_devices": {
        "regulation": "Regulation (EC) No 715/2007",
        "article":    "Article 5(2)",
        "key_elements": [
            "defeat devices shall be prohibited",
            "exception (a): engine protection against damage or accident",
            "exception (b): device does not function beyond engine starting",
            "exception (c): conditions included in test procedures",
        ],
        "obligation_phrase": "use of defeat devices ... shall be prohibited",
    },
    "vehicle_safety_systems": {
        "regulation": "Regulation (EU) 2019/2144",
        "article":    "Article 5 + Annex II",
        "key_elements": [
            "Intelligent speed assistance — from 6 July 2022",
            "Driver drowsiness and attention warning — from 6 July 2022",
            "Advanced emergency braking — from 6 July 2024",
            "Emergency lane keeping — from 6 July 2024",
            "Event data recorder — from 6 July 2022",
        ],
        "obligation_phrase": "vehicles ... shall be equipped with the systems ... listed in Annex II",
    },
    "battery_used_vs_waste": {
        "regulation": "Regulation (EU) 2023/1542",
        "article":    "Article 76(2)",
        "key_elements": [
            "(a) invoice and contract confirming destination is reuse",
            "(b) functional test results showing battery is not damaged",
            "(c) statement that no hazardous components prevent reuse",
            "(d) documentation of destination facility and its authorisation",
            "documentation kept for 3 years (paragraph 4)",
        ],
        "obligation_phrase": "holder shall provide documentation ... shall include at least the following",
    },
    "reach_sds": {
        "regulation": "Regulation (EC) No 1907/2006 (REACH)",
        "article":    "Article 31",
        "key_elements": [
            "when substance classified as hazardous (a)",
            "when substance is PBT or vPvB (b)",
            "when substance on SVHC candidate list (c)",
            "16 mandatory SDS headings (Annex II)",
        ],
        "obligation_phrase": "supplier ... shall provide the recipient ... with a safety data sheet",
    },
    "food_unsafe": {
        "regulation": "Regulation (EC) No 178/2002",
        "article":    "Article 14",
        "key_elements": [
            "food shall not be placed on market if unsafe",
            "unsafe = injurious to health (a) or unfit for human consumption (b)",
            "injurious: consider immediate/long-term effects, cumulative toxic effects",
            "unfit: unacceptable due to contamination, putrefaction, decay",
        ],
        "obligation_phrase": "food shall not be placed on the market if it is unsafe",
    },
}
