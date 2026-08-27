from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from agent.tools.business_data.deterministic import (
    DeterministicProviderError,
    resolve_deterministic_provider_binding,
)
from integration.sandbox.errors import SandboxUnavailableError


@pytest.fixture(autouse=True)
def _pin_synthetic_provider_endpoint(monkeypatch):
    from agent.tools.business_data import deterministic as module

    def pin(endpoint):
        parsed = urlsplit(endpoint)
        port = ":{}".format(parsed.port) if parsed.port else ""
        return parsed._replace(netloc="192.0.2.24{}".format(port)).geturl()

    monkeypatch.setattr(module, "sandbox_reachable_url", pin)


def _source(provider="neo4j", **overrides):
    config = {
        "uri": "bolt://snapshot.example.com:7687",
        "database": "snapshot_20260808",
        "username": "reader",
        "password": "secret-password",
        "data_revision": "2026-08-08",
        "snapshot_selector": "snapshot_20260808",
        "access_scope_revision": "reader-scope-v1",
        "deterministic_read_only": True,
        "deterministic_snapshot_immutable": True,
        "deterministic_scope_isolated": True,
        **overrides,
    }
    return SimpleNamespace(provider=provider, config=config)


def test_neo4j_snapshot_binding_is_revision_pinned_and_scope_digest_is_secret_free(
    monkeypatch,
):
    from agent.tools.business_data import deterministic as module

    monkeypatch.setattr(module, "resolve_business_data_source", lambda **_kwargs: _source())

    binding = resolve_deterministic_provider_binding(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        ontology_version="1.0.0",
    )

    assert binding.provider == "neo4j"
    assert binding.data_revision == "2026-08-08"
    assert binding.network_endpoint == "bolt://192.0.2.24:7687"
    assert binding.scope_digest.startswith("sha256:")
    assert "secret-password" not in binding.scope_digest
    assert binding.effective_connection.options["workspace_id"] == "workspace-a"
    assert binding.effective_connection.options["ontology_id"] == "ontology-a"
    assert binding.effective_connection.options["ontology_version"] == "1.0.0"
    assert binding.effective_connection.options["snapshot_selector"] == "snapshot_20260808"
    assert binding.effective_connection.auth.username == "reader"
    assert binding.effective_connection.auth.password == "secret-password"
    assert "password" not in binding.effective_connection.options


@pytest.mark.parametrize(
    "source,code",
    [
        (_source(provider="dataagent"), "provider_not_deterministic"),
        (_source(data_revision=""), "dependency_revision_missing"),
        (_source(access_scope_revision=""), "provider_scope_invalid"),
        (_source(deterministic_read_only=False), "provider_not_deterministic"),
        (_source(deterministic_snapshot_immutable=False), "provider_snapshot_unverified"),
        (_source(deterministic_scope_isolated=False), "provider_scope_invalid"),
        (_source(snapshot_selector="current"), "provider_scope_invalid"),
    ],
)
def test_ineligible_or_unpinned_provider_fails_closed(monkeypatch, source, code):
    from agent.tools.business_data import deterministic as module

    monkeypatch.setattr(
        module,
        "resolve_business_data_source",
        lambda **_kwargs: source,
    )

    with pytest.raises(DeterministicProviderError) as exc:
        resolve_deterministic_provider_binding(
            workspace_id="workspace-a",
            ontology_id="ontology-a",
            ontology_version="1.0.0",
        )

    assert exc.value.code == code


def test_provider_requires_workspace_before_resolution(monkeypatch):
    from agent.tools.business_data import deterministic as module

    monkeypatch.setattr(
        module,
        "resolve_business_data_source",
        lambda **_kwargs: pytest.fail("resolver must not be called"),
    )

    with pytest.raises(DeterministicProviderError) as exc:
        resolve_deterministic_provider_binding(
            workspace_id=None,
            ontology_id="ontology-a",
            ontology_version="1.0.0",
        )

    assert exc.value.code == "provider_scope_invalid"


def test_provider_endpoint_resolution_failure_is_closed_before_execution(monkeypatch):
    from agent.tools.business_data import deterministic as module

    monkeypatch.setattr(module, "resolve_business_data_source", lambda **_kwargs: _source())
    monkeypatch.setattr(
        module,
        "sandbox_reachable_url",
        lambda _endpoint: (_ for _ in ()).throw(
            SandboxUnavailableError("synthetic resolution failure")
        ),
    )

    with pytest.raises(DeterministicProviderError) as exc:
        resolve_deterministic_provider_binding(
            workspace_id="workspace-a",
            ontology_id="ontology-a",
            ontology_version="1.0.0",
        )

    assert exc.value.code == "provider_endpoint_unresolved"


def test_provider_endpoint_change_changes_scope_digest(monkeypatch):
    from agent.tools.business_data import deterministic as module

    active_source = _source()
    monkeypatch.setattr(
        module,
        "resolve_business_data_source",
        lambda **_kwargs: active_source,
    )
    first = resolve_deterministic_provider_binding(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        ontology_version="1.0.0",
    )
    active_source = _source(uri="bolt://other-snapshot.example.com:7687")
    second = resolve_deterministic_provider_binding(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        ontology_version="1.0.0",
    )

    assert first.data_revision == second.data_revision
    assert first.scope_digest != second.scope_digest


def test_provider_resolved_ip_change_changes_scope_digest(monkeypatch):
    from agent.tools.business_data import deterministic as module

    active_ip = "192.0.2.24"
    monkeypatch.setattr(
        module,
        "resolve_business_data_source",
        lambda **_kwargs: _source(),
    )

    def pin(endpoint):
        parsed = urlsplit(endpoint)
        return parsed._replace(netloc="{}:7687".format(active_ip)).geturl()

    monkeypatch.setattr(module, "sandbox_reachable_url", pin)
    first = resolve_deterministic_provider_binding(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        ontology_version="1.0.0",
    )
    active_ip = "192.0.2.25"
    second = resolve_deterministic_provider_binding(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        ontology_version="1.0.0",
    )

    assert first.data_revision == second.data_revision
    assert first.scope_digest != second.scope_digest


def test_provider_access_scope_change_changes_scope_digest(monkeypatch):
    from agent.tools.business_data import deterministic as module

    active_source = _source(access_scope_revision="reader-scope-v1")
    monkeypatch.setattr(
        module,
        "resolve_business_data_source",
        lambda **_kwargs: active_source,
    )
    first = resolve_deterministic_provider_binding(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        ontology_version="1.0.0",
    )
    active_source = _source(access_scope_revision="reader-scope-v2")
    second = resolve_deterministic_provider_binding(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        ontology_version="1.0.0",
    )

    assert first.scope_digest != second.scope_digest


def test_provider_username_change_changes_scope_digest_without_exposing_username(
    monkeypatch,
):
    from agent.tools.business_data import deterministic as module

    active_source = _source(username="reader-a")
    monkeypatch.setattr(
        module,
        "resolve_business_data_source",
        lambda **_kwargs: active_source,
    )
    first = resolve_deterministic_provider_binding(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        ontology_version="1.0.0",
    )
    active_source = _source(username="reader-b")
    second = resolve_deterministic_provider_binding(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        ontology_version="1.0.0",
    )

    assert first.scope_digest != second.scope_digest
    assert "reader-a" not in first.scope_digest
    assert "reader-b" not in second.scope_digest


def test_provider_secret_rotation_does_not_change_scope_digest(monkeypatch):
    from agent.tools.business_data import deterministic as module

    active_source = _source(password="synthetic-password-before")
    monkeypatch.setattr(
        module,
        "resolve_business_data_source",
        lambda **_kwargs: active_source,
    )
    first = resolve_deterministic_provider_binding(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        ontology_version="1.0.0",
    )
    active_source = _source(password="synthetic-password-after")
    second = resolve_deterministic_provider_binding(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        ontology_version="1.0.0",
    )

    assert first.scope_digest == second.scope_digest


@pytest.mark.parametrize("field", ["password", "token"])
def test_deterministic_provider_rejects_short_credentials(monkeypatch, field):
    from agent.tools.business_data import deterministic as module

    overrides = {"username": "" if field == "token" else "reader", field: "short"}
    monkeypatch.setattr(
        module,
        "resolve_business_data_source",
        lambda **_kwargs: _source(**overrides),
    )

    with pytest.raises(DeterministicProviderError) as exc:
        resolve_deterministic_provider_binding(
            workspace_id="workspace-a",
            ontology_id="ontology-a",
            ontology_version="1.0.0",
        )

    assert exc.value.code == "provider_credentials_unsafe"
    assert "short" not in str(exc.value)


def test_hugegraph_graph_space_is_part_of_selector_and_scope_digest(monkeypatch):
    from agent.tools.business_data import deterministic as module

    active_source = _source(
        provider="hugegraph",
        url="https://snapshot.example.com",
        graph="hugegraph",
        graph_space="space-a",
        snapshot_selector="space-a/hugegraph",
    )
    monkeypatch.setattr(
        module,
        "resolve_business_data_source",
        lambda **_kwargs: active_source,
    )
    first = resolve_deterministic_provider_binding(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        ontology_version="1.0.0",
    )
    active_source = _source(
        provider="hugegraph",
        url="https://snapshot.example.com",
        graph="hugegraph",
        graph_space="space-b",
        snapshot_selector="space-b/hugegraph",
    )
    second = resolve_deterministic_provider_binding(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        ontology_version="1.0.0",
    )

    assert first.scope_digest != second.scope_digest
    assert first.effective_connection.options["physical_target"] == "space-a/hugegraph"
    assert second.effective_connection.options["physical_target"] == "space-b/hugegraph"


def test_hugegraph_selector_cannot_omit_graph_space(monkeypatch):
    from agent.tools.business_data import deterministic as module

    monkeypatch.setattr(
        module,
        "resolve_business_data_source",
        lambda **_kwargs: _source(
            provider="hugegraph",
            url="https://snapshot.example.com",
            graph="hugegraph",
            graph_space="space-a",
            snapshot_selector="hugegraph",
        ),
    )

    with pytest.raises(DeterministicProviderError) as exc:
        resolve_deterministic_provider_binding(
            workspace_id="workspace-a",
            ontology_id="ontology-a",
            ontology_version="1.0.0",
        )

    assert exc.value.code == "provider_scope_invalid"


@pytest.mark.parametrize(
    "source",
    [
        _source(uri="bolt://embedded:uri-secret@snapshot.example.com:7687"),
        _source(
            provider="hugegraph",
            url="https://embedded:uri-secret@snapshot.example.com",
            graph="hugegraph",
            snapshot_selector="hugegraph",
        ),
        _source(uri="bolt://snapshot.example.com:7687?access_token=query-secret"),
        _source(uri="bolt://snapshot.example.com:7687#fragment-secret"),
    ],
)
def test_deterministic_provider_rejects_credentials_embedded_in_endpoint(
    monkeypatch,
    source,
):
    from agent.tools.business_data import deterministic as module

    monkeypatch.setattr(
        module,
        "resolve_business_data_source",
        lambda **_kwargs: source,
    )

    with pytest.raises(DeterministicProviderError) as exc:
        resolve_deterministic_provider_binding(
            workspace_id="workspace-a",
            ontology_id="ontology-a",
            ontology_version="1.0.0",
        )

    assert exc.value.code == "provider_scope_invalid"
    assert "secret" not in str(exc.value)
