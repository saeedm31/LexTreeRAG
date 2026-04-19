"""
Tests for pipeline/tree_navigator.py

Covers:
  - make_slim_tree()          — slim index formatting + OBL/LIST badges
  - _make_context_section()   — conversation context formatting
"""

import pytest
from pipeline.tree_navigator import make_slim_tree, _make_context_section


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _make_tree(doc_id="32019R2144", nodes=None):
    return {
        "doc_id": doc_id,
        "title":  "Vehicle Safety Regulation",
        "year":   2019,
        "url":    "https://eur-lex.europa.eu/example",
        "nodes":  nodes or [],
    }


def _make_node(node_id, number, title, summary, obligation_score=0, list_score=0, text=""):
    return {
        "id":               node_id,
        "number":           number,
        "title":            title,
        "summary":          summary,
        "text":             text,
        "obligation_score": obligation_score,
        "list_score":       list_score,
    }


# ─────────────────────────────────────────────────────────────────────────────
# make_slim_tree
# ─────────────────────────────────────────────────────────────────────────────

class TestMakeSlimTree:
    def test_returns_string(self):
        tree = _make_tree(nodes=[_make_node("art_1", "1", "Scope", "Defines scope.")])
        assert isinstance(make_slim_tree([tree]), str)

    def test_doc_id_in_output(self):
        tree = _make_tree(doc_id="32016R0679")
        slim = make_slim_tree([tree])
        assert "32016R0679" in slim

    def test_article_number_in_output(self):
        node = _make_node("art_5", "5", "Data processing principles", "Sets out principles.")
        slim = make_slim_tree([_make_tree(nodes=[node])])
        assert "Article 5" in slim

    def test_summary_in_output(self):
        node = _make_node("art_5", "5", "Principles", "Establishes key principles.")
        slim = make_slim_tree([_make_tree(nodes=[node])])
        assert "Establishes key principles." in slim

    def test_obl_badge_shown_when_obligation_score_high(self):
        node = _make_node("art_5", "5", "Obligations", "Sets obligations.", obligation_score=3)
        slim = make_slim_tree([_make_tree(nodes=[node])])
        assert "[OBL]" in slim

    def test_obl_badge_absent_when_obligation_score_low(self):
        node = _make_node("art_1", "1", "Scope", "Defines scope.", obligation_score=1)
        slim = make_slim_tree([_make_tree(nodes=[node])])
        assert "[OBL]" not in slim

    def test_list_badge_shown_when_list_score_high(self):
        node = _make_node("art_6", "6", "Requirements", "Lists requirements.", list_score=3)
        slim = make_slim_tree([_make_tree(nodes=[node])])
        assert "[LIST]" in slim

    def test_list_badge_absent_when_list_score_low(self):
        node = _make_node("art_1", "1", "Scope", "Defines scope.", list_score=0)
        slim = make_slim_tree([_make_tree(nodes=[node])])
        assert "[LIST]" not in slim

    def test_both_badges_shown(self):
        node = _make_node("art_5", "5", "Mandatory systems", "Lists mandatory systems.",
                          obligation_score=5, list_score=4)
        slim = make_slim_tree([_make_tree(nodes=[node])])
        assert "[OBL]" in slim
        assert "[LIST]" in slim

    def test_multiple_documents(self):
        tree1 = _make_tree(doc_id="32019R2144", nodes=[_make_node("art_1","1","Scope","S.")])
        tree2 = _make_tree(doc_id="32016R0679", nodes=[_make_node("art_5","5","Principles","P.")])
        slim  = make_slim_tree([tree1, tree2])
        assert "32019R2144" in slim
        assert "32016R0679" in slim

    def test_empty_trees(self):
        slim = make_slim_tree([])
        assert isinstance(slim, str)

    def test_tree_year_in_output(self):
        tree = _make_tree(nodes=[_make_node("art_1", "1", "Scope", "S.")])
        slim = make_slim_tree([tree])
        assert "2019" in slim


# ─────────────────────────────────────────────────────────────────────────────
# _make_context_section
# ─────────────────────────────────────────────────────────────────────────────

class TestMakeContextSection:
    def test_empty_list_returns_empty_string(self):
        assert _make_context_section([]) == ""

    def test_returns_string(self):
        node = {"doc_id": "32016R0679", "title": "Article 5", "summary": "Key principles."}
        assert isinstance(_make_context_section([node]), str)

    def test_context_header_present(self):
        node = {"doc_id": "32016R0679", "title": "Article 5", "summary": "Key principles."}
        result = _make_context_section([node])
        assert "PREVIOUS CONTEXT" in result

    def test_doc_id_present(self):
        node = {"doc_id": "32016R0679", "title": "Article 5", "summary": "Key principles."}
        result = _make_context_section([node])
        assert "32016R0679" in result

    def test_summary_present(self):
        node = {"doc_id": "32016R0679", "title": "Article 5", "summary": "Data processing rules."}
        result = _make_context_section([node])
        assert "Data processing rules." in result

    def test_caps_at_eight_nodes(self):
        nodes = [
            {"doc_id": f"DOC{i}", "title": f"Art {i}", "summary": f"Summary {i}."}
            for i in range(15)
        ]
        result = _make_context_section(nodes)
        # Should not include all 15 — only first 8
        assert "Summary 8" not in result
        assert "Summary 7" in result
