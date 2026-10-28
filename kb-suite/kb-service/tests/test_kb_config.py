import pytest

from kb_service import kb_config


def test_legacy_flat_config_maps_to_chunking():
    cfg = kb_config.load({"chunk_size": 800, "chunk_overlap": 100})
    assert cfg.chunking.chunk_size == 800
    assert cfg.chunking.chunk_overlap == 100
    assert cfg.questions.enabled is False


def test_explicit_zero_and_false_are_kept():
    cfg = kb_config.load({"chunking": {"chunk_overlap": 0}, "summary": {"enabled": False}})
    assert cfg.chunking.chunk_overlap == 0
    assert cfg.summary.enabled is False


def test_invalid_stored_section_falls_back_to_defaults_only_for_that_section():
    cfg = kb_config.load({"chunking": {"chunk_size": 5}, "questions": {"enabled": True, "count": 4}})
    assert cfg.chunking.chunk_size == kb_config.defaults().chunking.chunk_size
    assert cfg.questions.enabled is True and cfg.questions.count == 4


def test_apply_patch_validates_and_merges_sections():
    stored = kb_config.apply_patch({}, {"wiki": {"enabled": True}})
    stored = kb_config.apply_patch(stored, {"wiki": {"granularity": "focused"}})
    assert stored["wiki"]["enabled"] is True
    assert stored["wiki"]["granularity"] == "focused"
    with pytest.raises(kb_config.ConfigError):
        kb_config.apply_patch(stored, {"chunking": {"chunk_size": 200, "chunk_overlap": 150}})
    with pytest.raises(kb_config.ConfigError):
        kb_config.apply_patch(stored, {"nope": {}})


def test_overrides_keep_only_overridable_keys():
    clean = kb_config.validate_overrides(
        {"chunking": {"chunk_size": 1000, "separators": ["\n"]}, "retrieval": {"rerank_enabled": False},
         "questions": {"enabled": True, "instructions": "x"}}
    )
    assert clean == {"chunking": {"chunk_size": 1000, "separators": ["\n"]}, "questions": {"enabled": True}}
    with pytest.raises(kb_config.ConfigError):
        kb_config.validate_overrides({"questions": {"count": 50}})


def test_merge_retrieval_takes_max_counts_and_any_switch_then_options():
    a = kb_config.load({"retrieval": {"vector_top_k": 10, "mmr_enabled": False, "query_expansion": False}})
    b = kb_config.load({"retrieval": {"vector_top_k": 60, "mmr_enabled": False, "query_expansion": True}})
    rc = kb_config.merge_retrieval([a, b], {"mmr_enabled": True, "unknown": 1})
    assert rc.vector_top_k == 60
    assert rc.query_expansion is True
    assert rc.mmr_enabled is True
    with pytest.raises(kb_config.ConfigError):
        kb_config.merge_retrieval([a], {"mmr_lambda": 3})
