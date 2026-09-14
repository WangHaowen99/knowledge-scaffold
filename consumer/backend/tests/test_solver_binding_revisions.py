from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest
from solver_coordination.bindings import (
    BindingContractError,
    DurableSolverBindingResolver,
    SolverBindingRevision,
)
from solver_coordination.canonical import canonical_json_bytes
from solver_coordination.connectors.endpoint_policy import EndpointPolicy
from solver_coordination.job_store import JobStoreConflict, SolverJobStore
from solver_coordination.service import SolverCoordinationService, SolverServiceError


def _endpoint_policy() -> EndpointPolicy:
    return EndpointPolicy(
        allowed_hosts=("service.example.net",),
        allowed_ports=(8080,),
        allow_http=True,
        allow_localhost=False,
    )


def _configuration() -> dict:
    return {
        "endpoint": "http://service.example.net:8080",
        "connector_type": "ortools",
        "service_api_version": "ortools.solver.v1",
        "service_credential_ref": "secret-ref://solver/connector-a",
        "capability_fingerprint": "sha256:" + "a" * 64,
        "allowed_families": {"binary_ordered_route": ["1.0"]},
        "neutral_limits": {
            "max_payload_bytes": 1048576,
            "max_solve_time_ms": 60000,
            "max_workers": 4,
        },
        "execution_profile_ref": "profile_default",
        "enabled": True,
    }


def _capability_fingerprint(payload: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def test_binding_revision_round_trips_as_an_immutable_direct_ortools_contract():
    binding = SolverBindingRevision.from_configuration(
        revision_id="binding-revision-a",
        workspace_id="workspace-a",
        configuration=_configuration(),
        endpoint_policy=_endpoint_policy(),
    )

    assert binding.revision_id == "binding-revision-a"
    assert binding.workspace_id == "workspace-a"
    assert binding.endpoint == "http://service.example.net:8080"
    assert binding.connector_type == "ortools"
    assert binding.service_api_version == "ortools.solver.v1"
    assert binding.allowed_families == {"binary_ordered_route": ("1.0",)}
    assert binding.to_configuration() == _configuration()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.__setitem__("endpoint", "http://plain.example.com:8080"), "allowlist"),
        (lambda value: value.__setitem__("connector_type", "vendor"), "connector type"),
        (lambda value: value.__setitem__("service_api_version", "solver.job.v1"), "service API version"),
        (lambda value: value.__setitem__("service_credential_ref", "token-secret"), "credential reference"),
        (lambda value: value.__setitem__("enabled", "yes"), "enabled"),
        (lambda value: value.__setitem__("vendor", "ortools"), "fields"),
        (lambda value: value.__setitem__("token", "plaintext"), "fields"),
    ],
)
def test_binding_revision_rejects_unsafe_or_vendor_specific_configuration(mutation, message):
    configuration = _configuration()
    mutation(configuration)

    with pytest.raises(BindingContractError, match=message):
        SolverBindingRevision.from_configuration(
            revision_id="binding-revision-a",
            workspace_id="workspace-a",
            configuration=configuration,
            endpoint_policy=_endpoint_policy(),
        )


def test_binding_revision_rejects_the_legacy_solver_job_protocol():
    configuration = _configuration()
    configuration.pop("connector_type")
    configuration.pop("service_api_version")
    configuration["protocol_version"] = "solver.job.v1"

    with pytest.raises(BindingContractError, match="fields"):
        SolverBindingRevision.from_configuration(
            revision_id="binding-revision-a",
            workspace_id="workspace-a",
            configuration=configuration,
            endpoint_policy=_endpoint_policy(),
        )


def test_store_registers_and_reads_a_strong_binding_revision(tmp_path):
    store = SolverJobStore(tmp_path / "jobs.sqlite3")

    registered = store.register_connector_binding_revision(
        workspace_id="workspace-a",
        owner_id="owner-a",
        binding_revision_id="binding-revision-a",
        configuration=_configuration(),
        endpoint_policy=_endpoint_policy(),
    )
    loaded = store.get_binding_revision(
        "binding-revision-a",
        workspace_id="workspace-a",
        owner_id="owner-a",
    )

    assert registered == loaded
    assert loaded["configuration"] == _configuration()
    assert (
        store.get_binding_revision(
            "binding-revision-a",
            workspace_id="workspace-b",
            owner_id="owner-a",
        )
        is None
    )


def test_binding_revision_is_immutable_in_the_store(tmp_path):
    store = SolverJobStore(tmp_path / "jobs.sqlite3")
    store.register_connector_binding_revision(
        workspace_id="workspace-a",
        owner_id="owner-a",
        binding_revision_id="binding-revision-a",
        configuration=_configuration(),
        endpoint_policy=_endpoint_policy(),
    )
    changed = deepcopy(_configuration())
    changed["execution_profile_ref"] = "profile_other"

    with pytest.raises(JobStoreConflict, match="different configuration"):
        store.register_connector_binding_revision(
            workspace_id="workspace-a",
            owner_id="owner-a",
            binding_revision_id="binding-revision-a",
            configuration=changed,
            endpoint_policy=_endpoint_policy(),
        )


def test_service_rejects_legacy_arbitrary_binding_configuration_before_new_run():
    with pytest.raises(SolverServiceError, match="binding"):
        SolverCoordinationService.binding_for_test(
            {
                "binding_revision_id": "legacy-binding",
                "configuration": {"backend": "local-process"},
            }
        )


def test_durable_resolver_is_workspace_scoped_and_rejects_disabled_binding(tmp_path):
    store = SolverJobStore(tmp_path / "jobs.sqlite3")
    disabled = _configuration()
    disabled["enabled"] = False
    store.register_connector_binding_revision(
        workspace_id="workspace-a",
        owner_id="owner-a",
        binding_revision_id="binding-revision-a",
        configuration=disabled,
        endpoint_policy=_endpoint_policy(),
    )
    resolver = DurableSolverBindingResolver(
        store=store,
        revision_id="binding-revision-a",
        endpoint_policy=_endpoint_policy(),
    )

    with pytest.raises(BindingContractError, match="disabled"):
        resolver.resolve(workspace_id="workspace-a", owner_id="owner-a")
    with pytest.raises(BindingContractError, match="not configured"):
        resolver.resolve(workspace_id="workspace-b", owner_id="owner-a")


def test_store_lists_all_binding_revisions_within_owner_scope(tmp_path):
    store = SolverJobStore(tmp_path / "jobs.sqlite3")
    for revision in ("binding-revision-b", "binding-revision-a"):
        store.register_connector_binding_revision(
            workspace_id="workspace-a",
            owner_id="owner-a",
            binding_revision_id=revision,
            configuration=_configuration(),
            endpoint_policy=_endpoint_policy(),
        )
    store.register_connector_binding_revision(
        workspace_id="workspace-b",
        owner_id="owner-a",
        binding_revision_id="binding-revision-other-scope",
        configuration=_configuration(),
        endpoint_policy=_endpoint_policy(),
    )

    revisions = store.list_binding_revisions(workspace_id="workspace-a", owner_id="owner-a")

    assert [item["binding_revision_id"] for item in revisions] == [
        "binding-revision-a",
        "binding-revision-b",
    ]


def test_dynamic_binding_resolution_selects_the_unique_enabled_family_match(tmp_path):
    store = SolverJobStore(tmp_path / "jobs.sqlite3")
    mismatched = _configuration()
    mismatched["allowed_families"] = {"other_family": ["1.0"]}
    store.register_connector_binding_revision(
        workspace_id="workspace-a",
        owner_id="owner-a",
        binding_revision_id="binding-revision-mismatch",
        configuration=mismatched,
        endpoint_policy=_endpoint_policy(),
    )
    store.register_connector_binding_revision(
        workspace_id="workspace-a",
        owner_id="owner-a",
        binding_revision_id="binding-revision-match",
        configuration=_configuration(),
        endpoint_policy=_endpoint_policy(),
    )
    resolver = DurableSolverBindingResolver(store=store, endpoint_policy=_endpoint_policy())

    resolved = resolver.resolve(
        workspace_id="workspace-a",
        owner_id="owner-a",
        operation_key="solve_route",
        problem_family="binary_ordered_route",
        family_version="1.0",
        capabilities=("lexicographic_objective",),
    )

    assert resolved["binding_revision_id"] == "binding-revision-match"


def test_dynamic_binding_resolution_fails_when_no_compatible_binding_exists(tmp_path):
    store = SolverJobStore(tmp_path / "jobs.sqlite3")
    configuration = _configuration()
    configuration["allowed_families"] = {"other_family": ["1.0"]}
    store.register_connector_binding_revision(
        workspace_id="workspace-a",
        owner_id="owner-a",
        binding_revision_id="binding-revision-other-family",
        configuration=configuration,
        endpoint_policy=_endpoint_policy(),
    )
    resolver = DurableSolverBindingResolver(store=store, endpoint_policy=_endpoint_policy())

    with pytest.raises(BindingContractError, match="compatible"):
        resolver.resolve(
            workspace_id="workspace-a",
            owner_id="owner-a",
            operation_key="solve_route",
            problem_family="binary_ordered_route",
            family_version="1.0",
        )


def test_dynamic_binding_resolution_fails_on_ambiguous_compatible_bindings(tmp_path):
    store = SolverJobStore(tmp_path / "jobs.sqlite3")
    for revision in ("binding-revision-a", "binding-revision-b"):
        store.register_connector_binding_revision(
            workspace_id="workspace-a",
            owner_id="owner-a",
            binding_revision_id=revision,
            configuration=_configuration(),
            endpoint_policy=_endpoint_policy(),
        )
    resolver = DurableSolverBindingResolver(store=store, endpoint_policy=_endpoint_policy())

    with pytest.raises(BindingContractError, match="ambiguous"):
        resolver.resolve(
            workspace_id="workspace-a",
            owner_id="owner-a",
            operation_key="solve_route",
            problem_family="binary_ordered_route",
            family_version="1.0",
        )


def test_dynamic_binding_resolution_accepts_internal_family_alias_and_capability_mapping(
    tmp_path,
):
    store = SolverJobStore(tmp_path / "jobs.sqlite3")
    store.register_connector_binding_revision(
        workspace_id="workspace-a",
        owner_id="owner-a",
        binding_revision_id="binding-revision-a",
        configuration=_configuration(),
        endpoint_policy=_endpoint_policy(),
    )
    resolver = DurableSolverBindingResolver(store=store, endpoint_policy=_endpoint_policy())

    resolved = resolver.resolve(
        workspace_id="workspace-a",
        owner_id="owner-a",
        operation_key="solve_route",
        capability={
            "problem_family": "candidate_selection.v1",
            "family_version": "1.0",
            "engine_capabilities": ["lexicographic_objective"],
        },
    )

    assert resolved["binding_revision_id"] == "binding-revision-a"


def test_dynamic_binding_resolution_accepts_internal_family_key_in_persisted_binding(
    tmp_path,
):
    store = SolverJobStore(tmp_path / "jobs.sqlite3")
    configuration = _configuration()
    configuration["allowed_families"] = {"candidate_selection.v1": ["1.0"]}
    store.register_connector_binding_revision(
        workspace_id="workspace-a",
        owner_id="owner-a",
        binding_revision_id="binding-revision-a",
        configuration=configuration,
        endpoint_policy=_endpoint_policy(),
    )
    resolver = DurableSolverBindingResolver(store=store, endpoint_policy=_endpoint_policy())

    resolved = resolver.resolve(
        workspace_id="workspace-a",
        owner_id="owner-a",
        problem_family="candidate_selection.v1",
        family_version="1.0",
    )

    assert resolved["binding_revision_id"] == "binding-revision-a"


def test_dynamic_binding_resolution_filters_a_capability_fingerprint_mismatch(tmp_path):
    store = SolverJobStore(tmp_path / "jobs.sqlite3")
    first = _configuration()
    first["capability_fingerprint"] = "sha256:" + "b" * 64
    store.register_connector_binding_revision(
        workspace_id="workspace-a",
        owner_id="owner-a",
        binding_revision_id="binding-revision-mismatch",
        configuration=first,
        endpoint_policy=_endpoint_policy(),
    )
    matching = _configuration()
    store.register_connector_binding_revision(
        workspace_id="workspace-a",
        owner_id="owner-a",
        binding_revision_id="binding-revision-match",
        configuration=matching,
        endpoint_policy=_endpoint_policy(),
    )
    resolver = DurableSolverBindingResolver(store=store, endpoint_policy=_endpoint_policy())

    resolved = resolver.resolve(
        workspace_id="workspace-a",
        owner_id="owner-a",
        problem_family="binary_ordered_route",
        family_version="1.0",
        capability_fingerprint=matching["capability_fingerprint"],
    )

    assert resolved["binding_revision_id"] == "binding-revision-match"


def test_explicit_binding_resolution_reports_family_incompatibility(tmp_path):
    store = SolverJobStore(tmp_path / "jobs.sqlite3")
    configuration = _configuration()
    configuration["allowed_families"] = {"other_family": ["1.0"]}
    store.register_connector_binding_revision(
        workspace_id="workspace-a",
        owner_id="owner-a",
        binding_revision_id="binding-revision-a",
        configuration=configuration,
        endpoint_policy=_endpoint_policy(),
    )
    resolver = DurableSolverBindingResolver(
        store=store,
        revision_id="binding-revision-a",
        endpoint_policy=_endpoint_policy(),
    )

    with pytest.raises(BindingContractError, match="incompatible"):
        resolver.resolve(
            workspace_id="workspace-a",
            owner_id="owner-a",
            problem_family="binary_ordered_route",
            family_version="1.0",
        )


def test_binding_capability_fingerprint_rejects_live_drift_before_submission():
    live = {
        "protocol_versions": ["ortools.solver.v1"],
        "connector": {
            "name": "ortools",
            "version": "1.0.0",
            "build": "sha256:" + "a" * 64,
        },
        "execution": {"async_jobs": True, "cancellation": True, "prefer_wait": True},
        "problem_families": [
            {
                "name": "binary_ordered_route",
                "versions": ["1.0"],
                "features": ["lexicographic_objective"],
                "result_assurances": ["feasible_solution", "best_bound", "gap"],
                "limits": {
                    "max_payload_bytes": 1048576,
                    "max_solve_time_ms": 60000,
                    "max_workers": 1,
                },
            }
        ],
    }
    configuration = _configuration()
    configuration["capability_fingerprint"] = _capability_fingerprint(live)
    binding = SolverBindingRevision.from_configuration(
        revision_id="binding-revision-a",
        workspace_id="workspace-a",
        configuration=configuration,
        endpoint_policy=_endpoint_policy(),
    )

    binding.assert_capabilities(live)
    changed = deepcopy(live)
    changed["connector"]["build"] = "sha256:" + "b" * 64
    with pytest.raises(BindingContractError, match="drift"):
        binding.assert_capabilities(changed)


def test_binding_rejects_non_allowlisted_endpoint_host():
    configuration = _configuration()
    configuration["endpoint"] = "https://other.example.net:8080"

    with pytest.raises(BindingContractError, match="allowlist"):
        SolverBindingRevision.from_configuration(
            revision_id="binding-revision-a",
            workspace_id="workspace-a",
            configuration=configuration,
            endpoint_policy=_endpoint_policy(),
        )


def test_binding_accepts_an_explicit_administrator_host_allowlist():
    configuration = _configuration()
    configuration["endpoint"] = "https://connector.private.example.com"
    policy = EndpointPolicy(allowed_hosts=("connector.private.example.com",))

    binding = SolverBindingRevision.from_configuration(
        revision_id="binding-revision-a",
        workspace_id="workspace-a",
        configuration=configuration,
        endpoint_policy=policy,
    )

    assert binding.endpoint == configuration["endpoint"]


def test_endpoint_policy_from_environment_requires_explicit_private_host_allowlist():
    policy = EndpointPolicy.from_env(
        {
            "SOLVER_SERVICE_ALLOWED_HOSTS": "solver.private.example.com",
            "SOLVER_SERVICE_ALLOWED_PORTS": "443,8443",
            "SOLVER_SERVICE_ALLOW_LOCALHOST": "false",
        }
    )

    assert policy.allowed_hosts == ("solver.private.example.com",)
    assert policy.allowed_ports == (443, 8443)
    assert policy.allow_localhost is False
    assert policy.validate("https://solver.private.example.com:8443") == ("https://solver.private.example.com:8443")
    with pytest.raises(ValueError, match="allowlist"):
        policy.validate("https://solver.example.com")
    with pytest.raises(ValueError, match="allowlist"):
        policy.validate("https://solver.example.org")


def test_endpoint_policy_rejects_plain_http_by_default():
    policy = EndpointPolicy.from_env(
        {
            "SOLVER_SERVICE_ALLOWED_HOSTS": "service.example.net",
            "SOLVER_SERVICE_ALLOWED_PORTS": "8080",
        }
    )

    with pytest.raises(ValueError, match="HTTP"):
        policy.validate("http://service.example.net:8080")


def test_endpoint_policy_accepts_plain_http_only_when_explicitly_enabled():
    policy = EndpointPolicy.from_env(
        {
            "SOLVER_SERVICE_ALLOWED_HOSTS": "service.example.net",
            "SOLVER_SERVICE_ALLOWED_PORTS": "8080",
            "SOLVER_SERVICE_ALLOW_HTTP": "true",
            "SOLVER_SERVICE_ALLOW_LOCALHOST": "false",
        }
    )

    assert policy.allow_http is True
    assert policy.validate("http://service.example.net:8080") == ("http://service.example.net:8080")
    with pytest.raises(ValueError, match="allowlist"):
        policy.validate("http://other.example.net:8080")
    with pytest.raises(ValueError):
        policy.validate("http://service.example.net:8080/v1")


def test_http_binding_requires_the_explicit_administrator_policy():
    configuration = _configuration()
    configuration["endpoint"] = "http://service.example.net:8080"
    policy = EndpointPolicy(
        allowed_hosts=("service.example.net",),
        allowed_ports=(8080,),
        allow_http=True,
        allow_localhost=False,
    )

    binding = SolverBindingRevision.from_configuration(
        revision_id="binding-revision-http-a",
        workspace_id="workspace-a",
        configuration=configuration,
        endpoint_policy=policy,
    )

    assert binding.endpoint == "http://service.example.net:8080"


def test_endpoint_policy_private_allowlist_disables_localhost_by_default():
    policy = EndpointPolicy.from_env({"SOLVER_SERVICE_ALLOWED_HOSTS": "solver.private.example.com"})

    with pytest.raises(ValueError, match="allowlist"):
        policy.validate("https://localhost")


def test_explicit_endpoint_policy_allowlist_does_not_retain_localhost_exception():
    policy = EndpointPolicy(allowed_hosts=("solver.private.example.com",))

    with pytest.raises(ValueError, match="allowlist"):
        policy.validate("https://localhost")


@pytest.mark.parametrize(
    "environment",
    [
        {"SOLVER_SERVICE_ALLOWED_HOSTS": "solver.private.example.com,,other.private.example.com"},
        {"SOLVER_SERVICE_ALLOWED_PORTS": "443,nope"},
        {"SOLVER_SERVICE_ALLOW_LOCALHOST": "maybe"},
        {"SOLVER_SERVICE_ALLOW_HTTP": "maybe"},
    ],
)
def test_endpoint_policy_rejects_malformed_environment(environment):
    with pytest.raises(ValueError):
        EndpointPolicy.from_env(environment)


def test_endpoint_policy_default_keeps_reserved_synthetic_hosts_without_private_targets():
    policy = EndpointPolicy.from_env({})

    assert policy.validate("https://solver.example.com") == "https://solver.example.com"
    with pytest.raises(ValueError, match="port is invalid"):
        policy.validate("https://192.0.2.1")


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://solver.example.com/v1",
        "https://solver.example.com/%2Froute",
        "https://solver.example.com\\@other.example.com",
        "https://solver.example.com%2f@other.example.com",
        "https://solver.example.com:443\\route",
        " https://solver.example.com",
        "https://[not-an-ipv6-host",
    ],
)
def test_endpoint_policy_rejects_path_whitespace_and_malformed_url_forms(endpoint):
    policy = EndpointPolicy.from_env({})

    with pytest.raises(ValueError):
        policy.validate(endpoint)


def test_endpoint_policy_canonicalizes_a_root_path_before_connector_calls():
    policy = EndpointPolicy.from_env({})

    assert policy.validate("https://solver.example.com/") == ("https://solver.example.com")


def test_durable_binding_resolver_uses_administrator_endpoint_policy(tmp_path):
    store = SolverJobStore(tmp_path / "jobs.sqlite3")
    configuration = _configuration()
    configuration["endpoint"] = "https://solver.private.example.com"
    store.register_binding_revision(
        workspace_id="workspace-a",
        owner_id="owner-a",
        binding_revision_id="binding-revision-a",
        configuration=configuration,
    )
    resolver = DurableSolverBindingResolver(
        store=store,
        revision_id="binding-revision-a",
        endpoint_policy=EndpointPolicy(allowed_hosts=("solver.private.example.com",)),
    )

    resolved = resolver.resolve(workspace_id="workspace-a", owner_id="owner-a")

    assert resolved["configuration"]["endpoint"] == "https://solver.private.example.com"


def test_store_strict_registration_uses_administrator_endpoint_policy(tmp_path):
    store = SolverJobStore(tmp_path / "jobs.sqlite3")
    configuration = _configuration()
    configuration["endpoint"] = "https://solver.private.example.com"

    registered = store.register_connector_binding_revision(
        workspace_id="workspace-a",
        owner_id="owner-a",
        binding_revision_id="binding-revision-a",
        configuration=configuration,
        endpoint_policy=EndpointPolicy(allowed_hosts=("solver.private.example.com",)),
    )

    assert registered["configuration"]["endpoint"] == "https://solver.private.example.com"


def test_service_binding_validation_accepts_only_explicit_policy_for_private_host():
    configuration = _configuration()
    configuration["endpoint"] = "https://solver.private.example.com"
    policy = EndpointPolicy(allowed_hosts=("solver.private.example.com",))

    revision, normalized = SolverCoordinationService.binding_for_test(
        {
            "binding_revision_id": "binding-revision-a",
            "configuration": configuration,
        },
        endpoint_policy=policy,
    )

    assert revision == "binding-revision-a"
    assert normalized["endpoint"] == "https://solver.private.example.com"
