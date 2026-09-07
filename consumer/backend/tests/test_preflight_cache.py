from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from agent.runtime.preflight_cache import (
    ImmutablePreflightCache,
    PreflightCacheKey,
)


def _key(**changes):
    values = {
        "workspace_id": "synthetic-workspace",
        "workspace_config_generation": 1,
        "system_config_generation": 1,
        "ontology_id": "synthetic-ontology",
        "ontology_version": "1.0.0",
        "mutation_revision": 1,
    }
    values.update(changes)
    return PreflightCacheKey(**values)


def test_cache_returns_one_recursive_immutable_product_for_the_same_key():
    cache = ImmutablePreflightCache(max_entries=4)
    calls = []

    first = cache.get_or_load(
        _key(),
        lambda: calls.append("load") or {"models": [{"id": "synthetic"}]},
    )
    second = cache.get_or_load(_key(), lambda: calls.append("unexpected") or {})

    assert first is second
    assert calls == ["load"]
    with pytest.raises(TypeError):
        first["models"] = ()
    with pytest.raises(TypeError):
        first["models"][0]["id"] = "changed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workspace_id", "other-workspace"),
        ("workspace_config_generation", 2),
        ("system_config_generation", 2),
        ("ontology_id", "other-ontology"),
        ("ontology_version", "2.0.0"),
        ("mutation_revision", 2),
    ],
)
def test_each_generation_or_package_identity_field_causes_a_miss(field, value):
    cache = ImmutablePreflightCache(max_entries=16)
    calls = []
    cache.get_or_load(_key(), lambda: calls.append("base") or {"value": "base"})
    cache.get_or_load(
        _key(**{field: value}),
        lambda: calls.append(field) or {"value": field},
    )
    assert calls == ["base", field]


def test_loader_exceptions_are_shared_with_waiters_but_never_cached():
    cache = ImmutablePreflightCache(max_entries=4)
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def fail():
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(timeout=2)
        raise RuntimeError("synthetic load failure")

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(cache.get_or_load, _key(), fail) for _ in range(4)]
        assert entered.wait(timeout=2)
        release.set()
        for future in futures:
            with pytest.raises(RuntimeError, match="synthetic load failure"):
                future.result(timeout=2)

    assert calls == 1
    assert cache.get_or_load(_key(), lambda: {"recovered": True})["recovered"] is True


def test_concurrent_callers_single_flight_and_lru_eviction_are_bounded():
    cache = ImmutablePreflightCache(max_entries=2)
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def load():
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(timeout=2)
        return {"loaded": calls}

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(cache.get_or_load, _key(), load) for _ in range(6)]
        assert entered.wait(timeout=2)
        release.set()
        products = [future.result(timeout=2) for future in futures]

    assert calls == 1
    assert all(product is products[0] for product in products)
    cache.get_or_load(_key(ontology_version="2.0.0"), lambda: {"version": 2})
    cache.get_or_load(_key(ontology_version="3.0.0"), lambda: {"version": 3})
    assert cache.size == 2
    cache.get_or_load(_key(), lambda: {"loaded": "again"})
    assert cache.metrics()["evictions"] == 2


def test_disabled_cache_loads_each_request(monkeypatch):
    monkeypatch.setenv("PERF_RUN_PREFLIGHT_CACHE_ENABLED", "false")
    cache = ImmutablePreflightCache(max_entries=4)
    calls = []

    cache.get_or_load(_key(), lambda: calls.append(1) or {"value": 1})
    cache.get_or_load(_key(), lambda: calls.append(2) or {"value": 2})

    assert calls == [1, 2]
    assert cache.size == 0
