# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Tests for graph filter expression normalization."""

from __future__ import annotations

from operator import getitem
import pytest

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


def test_filter_expr_does_not_split_or_inside_quoted_text():
    params = {}

    conditions = build_filter_where(
        {"display_name": "'NORTH OR SOUTH'"},
        "e",
        params,
        "fp",
    )

    assert conditions == ["e.display_name = $fp_display_name_0"]
    assert params["fp_display_name_0"] == "NORTH OR SOUTH"


def test_filter_expr_treats_neo4j_sequence_like_duration_as_one_scalar():
    from neo4j.time import Duration

    duration = Duration.from_iso_format("P1D")
    params = {}

    conditions = build_filter_where(
        {"elapsed": duration},
        "e",
        params,
        "fp",
    )

    assert conditions == ["e.elapsed = $fp_elapsed_0"]
    assert params["fp_elapsed_0"] is duration


def test_filter_expr_rejects_container_for_ordered_comparison():
    with pytest.raises(ValueError, match="合法图数据库参数类型"):
        build_filter_where(
            {"tags": {"gt": [1, 2]}},
            "e",
            {},
            "fp",
        )
