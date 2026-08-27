"""Golden and safety tests for ontology HashFrame/v1."""

import pytest

from ontology.hash_contract import (
    field_h,
    field_i,
    field_l,
    field_n,
    field_o,
    field_s,
    hash_frames,
    normalize_relative_path,
    value_h,
    value_o,
    value_s,
)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            [field_s("left", "ab"), field_s("right", "c")],
            "cc49cb5bda0c596900a4afd46406e8e80f50a2f8edbf052b2bc85bd71571d461",
        ),
        (
            [field_s("left", "a"), field_s("right", "bc")],
            "32f47e20aed926cdcd4e1fff04acc642549df811676f351c2a48926514dbb3ab",
        ),
        (
            [field_s("value", "")],
            "f607c6760702a364615c05c9446de9695dd4c49787e42fb8599ca62c579aea47",
        ),
        (
            [field_n("value")],
            "293f3e53ebb1ab835611b48c889cb65708fd612273cbf30d3b5794d3e219d9d0",
        ),
        (
            [field_i("ordinal", 12)],
            "62f237ca86a10a3c711392cf7f47fac1a65f17beaa654c50da60023dc6c8dbee",
        ),
    ],
)
def test_hash_frame_golden_vectors(fields, expected):
    assert hash_frames("test_concat/v1", fields) == expected


def test_nfc_and_container_order_are_deterministic():
    digest_a = hash_frames(
        "container_test/v1",
        [
            field_s("title", "中文 e\u0301 😀\x1f"),
            field_o("object", field_s("b", "2"), field_s("a", "1")),
            field_l("list", value_s("x"), value_o(field_s("path", "对象.md"))),
        ],
    )
    digest_b = hash_frames(
        "container_test/v1",
        [
            field_s("title", "中文 é 😀\x1f"),
            field_o("object", field_s("a", "1"), field_s("b", "2")),
            field_l("list", value_s("x"), value_o(field_s("path", "对象.md"))),
        ],
    )
    assert digest_a == digest_b


def test_hash_contract_rejects_implicit_or_ambiguous_values():
    with pytest.raises(ValueError):
        hash_frames("BadContract", [field_s("value", "x")])
    with pytest.raises(ValueError):
        hash_frames("hash_test/v1", [field_h("digest", "sha256:abc")])
    with pytest.raises(ValueError):
        hash_frames("hash_test/v1", [field_o("obj", field_s("é", "a"), field_s("e\u0301", "b"))])
    with pytest.raises(TypeError):
        hash_frames("hash_test/v1", [field_l("values", {"not": "typed"})])


def test_relative_path_contract():
    assert normalize_relative_path("objects/ECO受影响对象.md") == "objects/ECO受影响对象.md"
    for value in ("", "/absolute.md", "objects\\x.md", "objects/../x.md", "./objects/x.md"):
        with pytest.raises(ValueError):
            normalize_relative_path(value)


def test_hash_scalar_type_does_not_accept_digest_as_plain_container_value():
    digest = "a" * 64
    explicit = hash_frames("hash_test/v1", [field_l("values", value_h(digest))])
    string_value = hash_frames("hash_test/v1", [field_l("values", value_s(digest))])
    assert explicit != string_value
