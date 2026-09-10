from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from agent.tools.business_data.deterministic import (
    DeterministicProviderError,
    resolve_deterministic_provider_binding,
)
from backend_synthetic_credentials import synthetic_password
from integration.sandbox.errors import SandboxUnavailableError

LEGACY_FIELDS = {
    "data_revision",
    "snapshot_selector",
    "access_scope_revision",
    "deterministic_read_only",
    "deterministic_snapshot_immutable",
    "deterministic_scope_isolated",
}


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
        "uri": "bolt://neo4j.example.net:7687",
        "database": "neo4j",
        "username": "reader",
        "password": synthetic_password("deterministic-provider-password"),
        "workspace_property": "d2o_workspace_id",
        "workspace_value": "workspace-a",
        "ontology_property": "d2o_ontology_id",
        "ontology_value": "ontology-a",
        **overrides,
    }
    return SimpleNamespace(provider=provider, config=config)


def _resolve(monkeypatch, source=None, **kwargs):
    from agent.tools.business_data import deterministic as module

    monkeypatch.setattr(
        module,
        "resolve_business_data_source",
        lambda **_kwargs: source or _source(),
    )
    return resolve_deterministic_provider_binding(
        workspace_id=kwargs.pop("workspace_id", "workspace-a"),
        ontology_id=kwargs.pop("ontology_id", "ontology-a"),
        ontology_version=kwargs.pop("ontology_version", "1.0.0"),
        **kwargs,
    )


def test_neo4j_binding_derives_revision_and_scope_without_manual_fields(monkeypatch):
    binding = _resolve(monkeypatch)

    assert binding.provider == "neo4j"
    assert binding.data_revision.startswith("sha256:")
    assert binding.scope_digest.startswith("sha256:")
    assert binding.network_endpoint == "bolt://192.0.2.24:7687"
    assert binding.effective_connection.options["database"] == "neo4j"
    assert binding.effective_connection.options["workspace_value"] == "workspace-a"
    assert binding.effective_connection.options["ontology_value"] == "ontology-a"
    assert not LEGACY_FIELDS.intersection(binding.effective_connection.options)
    assert synthetic_password("deterministic-provider-password") not in binding.scope_digest


def test_binding_revision_tracks_physical_database_and_scope(monkeypatch):
    from agent.tools.business_data import deterministic as module

    active = _source()
    monkeypatch.setattr(module, "resolve_business_data_source", lambda **_: active)
    first = resolve_deterministic_provider_binding(
        workspace_id="workspace-a", ontology_id="ontology-a", ontology_version="1.0.0"
    )

    active = _source(
        database="neo4j_snapshot_20260904",
        workspace_value="workspace-b",
    )
    second = resolve_deterministic_provider_binding(
        workspace_id="workspace-a", ontology_id="ontology-a", ontology_version="1.0.0"
    )

    assert first.data_revision != second.data_revision
    assert first.scope_digest != second.scope_digest


def test_binding_uses_runtime_scope_when_optional_values_are_empty(monkeypatch):
    binding = _resolve(
        monkeypatch,
        _source(workspace_value="", ontology_value=""),
    )

    assert binding.effective_connection.options["workspace_value"] == "workspace-a"
    assert binding.effective_connection.options["ontology_value"] == "ontology-a"


def test_binding_rejects_disabled_ontology_isolation(monkeypatch):
    with pytest.raises(DeterministicProviderError) as exc:
        _resolve(monkeypatch, _source(ontology_property="", ontology_value=""))

    assert exc.value.code == "provider_scope_invalid"


def test_legacy_attestation_values_do_not_control_automatic_binding(monkeypatch):
    first = _resolve(monkeypatch, _source())
    second = _resolve(
        monkeypatch,
        _source(
            data_revision="operator-value",
            snapshot_selector="wrong-database",
            access_scope_revision="operator-scope",
            deterministic_read_only=False,
            deterministic_snapshot_immutable=False,
            deterministic_scope_isolated=False,
        ),
    )

    assert first.data_revision == second.data_revision
    assert first.scope_digest == second.scope_digest


@pytest.mark.parametrize("provider", ["hugegraph", "dataagent"])
def test_only_neo4j_is_eligible_for_automatic_deterministic_binding(monkeypatch, provider):
    source = _source(provider=provider)

    with pytest.raises(DeterministicProviderError) as exc:
        _resolve(monkeypatch, source)

    assert exc.value.code == "provider_not_deterministic"


def test_provider_requires_workspace_and_ontology_before_resolution(monkeypatch):
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

    monkeypatch.setattr(module, "resolve_business_data_source", lambda **_: _source())
    monkeypatch.setattr(
        module,
        "sandbox_reachable_url",
        lambda _endpoint: (_ for _ in ()).throw(SandboxUnavailableError("synthetic resolution failure")),
    )

    with pytest.raises(DeterministicProviderError) as exc:
        resolve_deterministic_provider_binding(
            workspace_id="workspace-a",
            ontology_id="ontology-a",
            ontology_version="1.0.0",
        )

    assert exc.value.code == "provider_endpoint_unresolved"


@pytest.mark.parametrize("field", ["password", "token"])
def test_deterministic_provider_rejects_short_credentials(monkeypatch, field):
    overrides = {"username": "" if field == "token" else "reader", field: "short"}

    with pytest.raises(DeterministicProviderError) as exc:
        _resolve(monkeypatch, _source(**overrides))

    assert exc.value.code == "provider_credentials_unsafe"
    assert "short" not in str(exc.value)


@pytest.mark.parametrize(
    "uri",
    [
        "bolt://embedded:uri-secret@neo4j.example.net:7687",
        "bolt://neo4j.example.net:7687?access_token=query-secret",
        "bolt://neo4j.example.net:7687#fragment-secret",
    ],
)
def test_deterministic_provider_rejects_credentials_embedded_in_uri(monkeypatch, uri):
    with pytest.raises(DeterministicProviderError) as exc:
        _resolve(monkeypatch, _source(uri=uri))

    assert exc.value.code == "provider_scope_invalid"
    assert "secret" not in str(exc.value)
