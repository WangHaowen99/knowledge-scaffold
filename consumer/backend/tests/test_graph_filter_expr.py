"""Tests for graph filter expression normalization."""

from __future__ import annotations

from operator import getitem

from agent.tools.business_data.providers.graph.filter_expr import build_filter_where


def test_filter_expr_accepts_mongo_style_operator_aliases():
    params = {}

    conditions = build_filter_where(
        {
            "Name": {"$contains": "A001"},
            "Code": {"$in": ["A001", "B001"]},
            "Status": {"$ne": "停用"},
            "Kind": {"$nin": ["旧件"]},
        },
        "e",
        params,
        "fp",
    )

    assert "e.Name CONTAINS $fp_Name_0_0_contains" in conditions
    assert "e.Code IN $fp_Code_0_0_in" in conditions
    assert "e.Status <> $fp_Status_0_0_ne" in conditions
    assert "NOT (e.Kind IN $fp_Kind_0_0_not_in)" in conditions
    assert getitem(params, "fp_Name_0_0_contains") == "A001"
    assert getitem(params, "fp_Code_0_0_in") == ["A001", "B001"]
    assert getitem(params, "fp_Status_0_0_ne") == "停用"
    assert getitem(params, "fp_Kind_0_0_not_in") == ["旧件"]
