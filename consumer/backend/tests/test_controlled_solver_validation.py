from __future__ import annotations

import ctypes
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from scripts import run_controlled_solver_validation as runner
from scripts.run_controlled_solver_validation import (
    EXPECTED_SCHEMA_VERSION,
    INPUT_SCHEMA_VERSION,
    MANIFEST_SCHEMA_VERSION,
    MAX_FILE_BYTES,
    ControlledValidationError,
    load_bundle,
    main,
    run_bundle,
)


def test_windows_handle_conversion_reads_ctypes_value_instead_of_int():
    handle = ctypes.c_void_p(12345)

    assert runner.native_handle_value_for_test(handle) == 12345
    with pytest.raises(ControlledValidationError, match="invalid"):
        runner.native_handle_value_for_test(ctypes.c_void_p(None))


ITEM_IDS = tuple("ITEM_SYNTHETIC_{:04d}".format(index) for index in range(1, 5))

ROUTE_FACT = {
    "metric_scale": 100,
    "rounding": "ROUND_HALF_EVEN",
    "capacity_limit": "10.00",
    "items": {
        "ITEM_SYNTHETIC_0001": {
            "OPTION_0": {"primary_score": "3.00", "load": "1.00", "safety": "4.00"},
            "OPTION_1": {"primary_score": "1.00", "load": "1.20", "safety": "3.00"},
        },
        "ITEM_SYNTHETIC_0002": {
            "OPTION_0": {"primary_score": "3.00", "load": "1.00", "safety": "4.00"},
            "OPTION_1": {"primary_score": "1.00", "load": "1.20", "safety": "3.00"},
        },
        "ITEM_SYNTHETIC_0003": {
            "OPTION_0": {"primary_score": "1.00", "load": "1.20", "safety": "3.00"},
            "OPTION_1": {"primary_score": "4.00", "load": "1.00", "safety": "4.00"},
        },
        "ITEM_SYNTHETIC_0004": {
            "OPTION_0": {"primary_score": "1.00", "load": "1.20", "safety": "3.00"},
            "OPTION_1": {"primary_score": "4.00", "load": "1.00", "safety": "4.00"},
        },
    },
}

OBJECTIVES = [
    {"metric": "primary_score", "sense": "maximize"},
    {"metric": "transition_count", "sense": "minimize"},
    {"metric": "safety_margin", "sense": "maximize"},
    {"metric": "candidate_rank", "sense": "minimize"},
]


def _canonical(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _input_payload() -> dict:
    return {
        "schema_version": INPUT_SCHEMA_VERSION,
        "values": {
            "items": [
                {
                    "item_order": order,
                    "item_id": item_id,
                    "quantity": "1.00",
                    "duration": "1.00",
                }
                for order, item_id in enumerate(ITEM_IDS, start=1)
            ]
        },
        "decision_model": {
            "profile_id": "ordered_binary_route.v1",
            "required_facts": ["route_metrics"],
            "fact_source": {"kind": "locked_model"},
            "materialized_facts": [{"name": "route_metrics", "value": deepcopy(ROUTE_FACT)}],
            "display_mapping": {
                "OPTION_0": "SYNTHETIC_ROUTE_A",
                "OPTION_1": "SYNTHETIC_ROUTE_B",
            },
            "objectives": deepcopy(OBJECTIVES),
        },
    }


def _expected_payload() -> dict:
    assignment = {item_id: "OPTION_0" if index < 2 else "OPTION_1" for index, item_id in enumerate(ITEM_IDS)}
    return {
        "schema_version": EXPECTED_SCHEMA_VERSION,
        "status": "completed",
        "feasibility": "feasible",
        "optimality": "proven_optimal",
        "proof_kind": "verifier_exhaustive_check",
        "summary": {
            "candidate_id": "OPTION_0_TO_OPTION_1_K0002",
            "combination_no": 6,
            "assignment": assignment,
            "items": [
                {
                    "item_id": item_id,
                    "route": "SYNTHETIC_ROUTE_A" if index < 2 else "SYNTHETIC_ROUTE_B",
                }
                for index, item_id in enumerate(ITEM_IDS)
            ],
            "objective_values": {
                "primary_score": 1400,
                "transition_count": 1,
                "safety_margin": 400,
                "candidate_rank": 3,
            },
        },
    }


def _write_manifest(
    root: Path,
    *,
    input_bytes: bytes,
    expected_bytes: bytes,
    declared_trust: str | None = None,
) -> str:
    input_digest = _digest(input_bytes)
    expected_digest = _digest(expected_bytes)
    projection = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "profile_id": "ordered_binary_route.v1",
        "file_digests": {
            "input.json": input_digest,
            "expected.json": expected_digest,
        },
    }
    trust_root = _digest(_canonical(projection))
    manifest = {
        **projection,
        "trust_root_digest": declared_trust or trust_root,
        "file_digests": {
            **projection["file_digests"],
            "manifest.json": trust_root,
        },
    }
    (root / "manifest.json").write_bytes(_canonical(manifest))
    return trust_root


def write_synthetic_bundle(root: Path) -> tuple[Path, str]:
    root.mkdir(parents=True)
    input_bytes = _canonical(_input_payload())
    expected_bytes = _canonical(_expected_payload())
    (root / "input.json").write_bytes(input_bytes)
    (root / "expected.json").write_bytes(expected_bytes)
    trust_root = _write_manifest(
        root,
        input_bytes=input_bytes,
        expected_bytes=expected_bytes,
    )
    return root, trust_root


def _repository_root(tmp_path: Path) -> Path:
    value = tmp_path / "repository"
    value.mkdir()
    return value


def test_main_returns_two_without_opt_in_and_does_not_resolve_paths(monkeypatch, capsys):
    monkeypatch.delenv("SOLVER_CONTROLLED_VALIDATION_ROOT", raising=False)
    monkeypatch.delenv("SOLVER_CONTROLLED_VALIDATION_TRUST_ROOT", raising=False)
    monkeypatch.delenv("SOLVER_CONTROLLED_VALIDATION_SERVICE_ENDPOINT", raising=False)
    monkeypatch.delenv("SOLVER_CONTROLLED_VALIDATION_SERVICE_CREDENTIAL_REF", raising=False)
    monkeypatch.delenv("SOLVER_CONTROLLED_VALIDATION_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("SOLVER_CONTROLLED_VALIDATION_SERVICE_TOKEN_FILE", raising=False)
    monkeypatch.setattr(
        runner,
        "_default_repository_root",
        lambda: (_ for _ in ()).throw(AssertionError("filesystem was touched")),
    )

    assert main() == 2
    assert capsys.readouterr().out == ""


def test_loader_rejects_a_root_inside_the_repository(tmp_path):
    repository_root = _repository_root(tmp_path)
    root = repository_root / "synthetic-bundle"
    root.mkdir()

    with pytest.raises(ControlledValidationError, match="outside"):
        load_bundle(
            root=root,
            repository_root=repository_root,
            approved_trust_root="sha256:" + "1" * 64,
        )


def test_loader_rejects_a_bundle_under_the_main_repo_from_a_linked_worktree(tmp_path, monkeypatch):
    main_repository = tmp_path / "main-repository"
    linked_worktree = main_repository / ".worktrees" / "synthetic-worktree"
    linked_worktree.mkdir(parents=True)
    bundle, trust_root = write_synthetic_bundle(main_repository / "controlled-bundle")
    monkeypatch.setattr(runner, "_default_repository_root", lambda: main_repository)

    with pytest.raises(ControlledValidationError, match="outside"):
        load_bundle(root=bundle, approved_trust_root=trust_root)


def _register_external_worktree(main_repository: Path, worktree: Path) -> None:
    worktree.mkdir(parents=True)
    metadata = main_repository / ".git" / "worktrees" / "external-synthetic"
    metadata.mkdir(parents=True)
    worktree_git_file = worktree / ".git"
    worktree_git_file.write_text("gitdir: {}\n".format(metadata), encoding="utf-8")
    (metadata / "gitdir").write_text(str(worktree_git_file.resolve()), encoding="utf-8")


def test_loader_rejects_a_bundle_in_an_external_registered_worktree(tmp_path):
    main_repository = tmp_path / "main-repository"
    (main_repository / ".git").mkdir(parents=True)
    external_worktree = tmp_path / "external-worktree"
    _register_external_worktree(main_repository, external_worktree)
    bundle, trust_root = write_synthetic_bundle(external_worktree / "controlled-bundle")

    with pytest.raises(ControlledValidationError, match="outside"):
        load_bundle(
            root=bundle,
            repository_root=main_repository,
            approved_trust_root=trust_root,
        )


def test_loader_rejects_unlisted_files(tmp_path):
    root, trust_root = write_synthetic_bundle(tmp_path / "bundle")
    (root / "extra.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ControlledValidationError, match="allowlist"):
        load_bundle(
            root=root,
            repository_root=_repository_root(tmp_path),
            approved_trust_root=trust_root,
        )


def test_loader_rejects_files_over_the_size_limit(tmp_path):
    root, trust_root = write_synthetic_bundle(tmp_path / "bundle")
    (root / "input.json").write_bytes(b"x" * (MAX_FILE_BYTES + 1))

    with pytest.raises(ControlledValidationError, match="size"):
        load_bundle(
            root=root,
            repository_root=_repository_root(tmp_path),
            approved_trust_root=trust_root,
        )


def test_loader_rejects_an_unapproved_trust_root(tmp_path):
    root, trust_root = write_synthetic_bundle(tmp_path / "bundle")

    with pytest.raises(ControlledValidationError, match="trust"):
        load_bundle(
            root=root,
            repository_root=_repository_root(tmp_path),
            approved_trust_root=("sha256:" + ("0" if trust_root[-1] != "0" else "1") * 64),
        )


def test_loader_rejects_content_that_changed_after_approval(tmp_path):
    root, trust_root = write_synthetic_bundle(tmp_path / "bundle")
    payload = json.loads((root / "input.json").read_text(encoding="utf-8"))
    payload["values"]["items"][0]["quantity"] = "2.00"
    (root / "input.json").write_bytes(_canonical(payload))

    with pytest.raises(ControlledValidationError, match="digest"):
        load_bundle(
            root=root,
            repository_root=_repository_root(tmp_path),
            approved_trust_root=trust_root,
        )


def test_loader_rejects_duplicate_json_keys_after_digest_validation(tmp_path):
    root, _trust_root = write_synthetic_bundle(tmp_path / "bundle")
    input_bytes = b'{"schema_version":"a","schema_version":"b"}'
    expected_bytes = (root / "expected.json").read_bytes()
    (root / "input.json").write_bytes(input_bytes)
    trust_root = _write_manifest(
        root,
        input_bytes=input_bytes,
        expected_bytes=expected_bytes,
    )

    with pytest.raises(ControlledValidationError, match="duplicate"):
        load_bundle(
            root=root,
            repository_root=_repository_root(tmp_path),
            approved_trust_root=trust_root,
        )


@pytest.mark.parametrize("rendered_float", ("1400.0", "1.4e3", "-0.0"))
def test_loader_rejects_floating_point_json_after_digest_validation(tmp_path, rendered_float):
    root, _trust_root = write_synthetic_bundle(tmp_path / "bundle")
    input_bytes = (root / "input.json").read_bytes()
    expected_bytes = (
        (root / "expected.json")
        .read_bytes()
        .replace(
            b'"primary_score":1400',
            '"primary_score":{}'.format(rendered_float).encode("ascii"),
        )
    )
    (root / "expected.json").write_bytes(expected_bytes)
    trust_root = _write_manifest(
        root,
        input_bytes=input_bytes,
        expected_bytes=expected_bytes,
    )

    with pytest.raises(ControlledValidationError, match="floating-point"):
        load_bundle(
            root=root,
            repository_root=_repository_root(tmp_path),
            approved_trust_root=trust_root,
        )


def test_loader_rejects_boolean_expected_objective_values(tmp_path):
    root, _trust_root = write_synthetic_bundle(tmp_path / "bundle")
    input_bytes = (root / "input.json").read_bytes()
    expected = _expected_payload()
    expected["summary"]["objective_values"]["candidate_rank"] = True
    expected_bytes = _canonical(expected)
    (root / "expected.json").write_bytes(expected_bytes)
    trust_root = _write_manifest(
        root,
        input_bytes=input_bytes,
        expected_bytes=expected_bytes,
    )

    with pytest.raises(ControlledValidationError, match="objective"):
        load_bundle(
            root=root,
            repository_root=_repository_root(tmp_path),
            approved_trust_root=trust_root,
        )


def test_loader_rejects_a_linked_bundle_file(tmp_path, monkeypatch):
    root, trust_root = write_synthetic_bundle(tmp_path / "bundle")
    input_path = root / "input.json"
    external = tmp_path / "external-input.json"
    external.write_bytes(input_path.read_bytes())
    input_path.unlink()
    try:
        input_path.symlink_to(external)
    except OSError:
        original = runner.unsafe_link_for_test
        runner.replace_unsafe_link_for_test(
            monkeypatch,
            lambda path: path.name == "input.json" or original(path)
        )
        input_path.write_bytes(external.read_bytes())

    with pytest.raises(ControlledValidationError, match="link"):
        load_bundle(
            root=root,
            repository_root=_repository_root(tmp_path),
            approved_trust_root=trust_root,
        )


@pytest.mark.skipif(runner.os.name != "nt", reason="Windows handle semantics only")
def test_windows_bundle_reader_uses_a_no_follow_handle(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.json"
    path.write_bytes(b"{}")
    opened: list[Path] = []
    real_open = runner.open_windows_no_follow_for_test

    def recording_open(selected_path: Path) -> int:
        opened.append(selected_path)
        return real_open(selected_path)

    monkeypatch.setattr(runner, "_open_windows_no_follow", recording_open)

    assert runner.read_bounded_file_for_test(path) == b"{}"
    assert opened == [path]


def _connector_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        "SOLVER_CONTROLLED_VALIDATION_SERVICE_ENDPOINT",
        "https://solver-connector.example.net",
    )
    monkeypatch.setenv(
        "SOLVER_CONTROLLED_VALIDATION_SERVICE_CREDENTIAL_REF",
        "controlled-validation-credential",
    )
    monkeypatch.setenv(
        "SOLVER_CONTROLLED_VALIDATION_SERVICE_TOKEN",
        "synthetic-controlled-validation-token",
    )
    monkeypatch.delenv("SOLVER_CONTROLLED_VALIDATION_SERVICE_TOKEN_FILE", raising=False)
    monkeypatch.delenv("SOLVER_CONTROLLED_VALIDATION_ALLOW_HTTP", raising=False)


def test_controlled_connector_configuration_rejects_http_without_explicit_opt_in(
    monkeypatch,
):
    _connector_environment(monkeypatch)
    monkeypatch.setenv(
        "SOLVER_CONTROLLED_VALIDATION_SERVICE_ENDPOINT",
        "http://ortools-solver:8080",
    )

    with pytest.raises(ControlledValidationError, match="endpoint"):
        runner.connector_configuration_from_env_for_test()


def test_controlled_connector_configuration_accepts_private_http_with_opt_in(
    monkeypatch,
):
    _connector_environment(monkeypatch)
    monkeypatch.setenv(
        "SOLVER_CONTROLLED_VALIDATION_SERVICE_ENDPOINT",
        "http://ortools-solver:8080",
    )
    monkeypatch.setenv("SOLVER_CONTROLLED_VALIDATION_ALLOW_HTTP", "true")

    configuration = runner.connector_configuration_from_env_for_test()

    assert configuration.endpoint == "http://ortools-solver:8080"


def test_run_bundle_uses_ortools_connector_and_emits_only_status_and_digests(tmp_path, capsys, monkeypatch):
    root, trust_root = write_synthetic_bundle(tmp_path / "bundle")
    expected = _expected_payload()
    _connector_environment(monkeypatch)
    seen: dict[str, object] = {}
    local_backend_module = sys.modules.get("solver_coordination.backends.local_process")

    class SyntheticOrToolsConnector:
        def __init__(self, **kwargs):
            seen["client_kwargs"] = kwargs

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def capabilities(self, *, binding):
            seen["capabilities_binding"] = dict(binding)
            return {
                "protocol_versions": ["ortools.solver.v1"],
                "connector": {
                    "name": "synthetic-connector",
                    "version": "1.0.0",
                    "build": "sha256:" + "1" * 64,
                },
                "execution": {
                    "async_jobs": True,
                    "cancellation": True,
                    "prefer_wait": False,
                },
                "problem_families": [
                    {
                        "name": "binary_ordered_route",
                        "versions": ["1.0"],
                        "features": ["lexicographic_objective"],
                        "result_assurances": [
                            "feasible_solution",
                            "best_bound",
                        ],
                        "limits": {
                            "max_payload_bytes": 1048576,
                            "max_solve_time_ms": 60000,
                            "max_workers": 1,
                        },
                    }
                ],
            }

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def submit(self, **kwargs):
            seen["submit"] = kwargs
            return {"job_id": "job_synthetic001", "status": "accepted"}

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def poll(self, **kwargs):
            seen["poll"] = kwargs
            return {"job_id": "job_synthetic001", "status": "completed"}

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def result(self, **kwargs):
            seen["result"] = kwargs
            from solver_coordination.contracts import SolverResult

            return SolverResult(
                problem_id=seen["submit"]["problem"].problem_id,
                status="optimal",
                assignment={
                    "candidate_id": expected["summary"]["candidate_id"],
                    **expected["summary"]["assignment"],
                },
                objective_values=expected["summary"]["objective_values"],
                diagnostics=(
                    {
                        "kind": "connector_assurance",
                        "best_bound": None,
                        "gap": None,
                        "certificate": None,
                    },
                ),
                engine={
                    "name": "synthetic-cp-sat",
                    "version": "1.0",
                    "connector_build": "sha256:" + "1" * 64,
                },
            )

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def cancel(self, **kwargs):
            seen["cancel"] = kwargs
            raise AssertionError("completed connector job must not be cancelled")

    monkeypatch.setattr(
        runner,
        "_connector",
        lambda configuration: SyntheticOrToolsConnector(
            credential_resolver=lambda reference: (
                configuration.token if reference == configuration.credential_ref else None
            )
        ),
    )

    assert (
        run_bundle(
            root=root,
            repository_root=_repository_root(tmp_path),
            approved_trust_root=trust_root,
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"status", "input_digest", "result_digest"}
    assert output == {
        "status": "passed",
        "input_digest": _digest((root / "input.json").read_bytes()),
        "result_digest": _digest(_canonical(expected)),
    }
    submit = seen["submit"]
    assert submit["problem"].problem_family == "candidate_selection.v1"
    assert submit["result_quality"] == "optimal"
    assert "request_ref" not in submit
    assert "capabilities" not in submit
    assert "limits" not in submit
    assert submit["idempotency_key"].startswith("hmac-sha256:")
    binding = submit["binding"]
    assert binding["endpoint"] == "https://solver-connector.example.net"
    assert binding["service_credential_ref"] == "controlled-validation-credential"
    assert "token" not in json.dumps(binding).lower()
    assert (
        seen["client_kwargs"]["credential_resolver"]("controlled-validation-credential")
        == "synthetic-controlled-validation-token"
    )
    assert sys.modules.get("solver_coordination.backends.local_process") is local_backend_module


def test_run_bundle_requires_explicit_connector_configuration(tmp_path, monkeypatch):
    root, trust_root = write_synthetic_bundle(tmp_path / "bundle")
    for name in (
        "SOLVER_CONTROLLED_VALIDATION_SERVICE_ENDPOINT",
        "SOLVER_CONTROLLED_VALIDATION_SERVICE_CREDENTIAL_REF",
        "SOLVER_CONTROLLED_VALIDATION_SERVICE_TOKEN",
        "SOLVER_CONTROLLED_VALIDATION_SERVICE_TOKEN_FILE",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ControlledValidationError, match="Connector"):
        run_bundle(
            root=root,
            repository_root=_repository_root(tmp_path),
            approved_trust_root=trust_root,
        )


def test_connector_token_file_cannot_live_in_repository_or_bundle(tmp_path, monkeypatch):
    root, trust_root = write_synthetic_bundle(tmp_path / "bundle")
    repository = _repository_root(tmp_path)
    token_file = repository / "connector-token"
    token_file.write_text("synthetic-controlled-validation-token", encoding="utf-8")
    monkeypatch.setenv(
        "SOLVER_CONTROLLED_VALIDATION_SERVICE_ENDPOINT",
        "https://solver-connector.example.net",
    )
    monkeypatch.setenv(
        "SOLVER_CONTROLLED_VALIDATION_SERVICE_CREDENTIAL_REF",
        "controlled-validation-credential",
    )
    monkeypatch.delenv("SOLVER_CONTROLLED_VALIDATION_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("SOLVER_CONTROLLED_VALIDATION_SERVICE_TOKEN_FILE", str(token_file))

    with pytest.raises(ControlledValidationError, match="credential file"):
        runner.connector_configuration_from_env_for_test(
            protected_repository_roots=(repository,), bundle_root=root
        )


def test_connector_job_is_cancelled_when_polling_fails(tmp_path, monkeypatch):
    root, trust_root = write_synthetic_bundle(tmp_path / "bundle")
    _connector_environment(monkeypatch)
    cancelled: list[dict[str, object]] = []

    class FailingConnector:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def capabilities(self, **_kwargs):
            return {
                "protocol_versions": ["ortools.solver.v1"],
                "connector": {
                    "name": "synthetic-connector",
                    "version": "1.0.0",
                    "build": "sha256:" + "1" * 64,
                },
                "execution": {
                    "async_jobs": True,
                    "cancellation": True,
                    "prefer_wait": False,
                },
                "problem_families": [
                    {
                        "name": "binary_ordered_route",
                        "versions": ["1.0"],
                        "features": ["lexicographic_objective"],
                        "result_assurances": ["feasible_solution", "best_bound"],
                        "limits": {
                            "max_payload_bytes": 1048576,
                            "max_solve_time_ms": 60000,
                            "max_workers": 1,
                        },
                    }
                ],
            }

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def submit(self, **_kwargs):
            return {"job_id": "job_synthetic001", "status": "accepted"}

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def poll(self, **_kwargs):
            raise RuntimeError("synthetic transport failure")

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def cancel(self, **kwargs):
            cancelled.append(dict(kwargs))
            return {"job_id": "job_synthetic001", "status": "cancelled"}

    monkeypatch.setattr(runner, "_connector", lambda _configuration: FailingConnector())

    with pytest.raises(ControlledValidationError, match="polling"):
        run_bundle(
            root=root,
            repository_root=_repository_root(tmp_path),
            approved_trust_root=trust_root,
        )
    assert len(cancelled) == 1
    assert cancelled[0]["job_id"] == "job_synthetic001"


def test_main_requires_connector_opt_in_before_resolving_paths(monkeypatch, capsys):
    monkeypatch.setenv("SOLVER_CONTROLLED_VALIDATION_ROOT", "synthetic-root")
    monkeypatch.setenv("SOLVER_CONTROLLED_VALIDATION_TRUST_ROOT", "sha256:" + "1" * 64)
    _connector_environment(monkeypatch)
    for name in (
        "SOLVER_CONTROLLED_VALIDATION_SERVICE_ENDPOINT",
        "SOLVER_CONTROLLED_VALIDATION_SERVICE_CREDENTIAL_REF",
        "SOLVER_CONTROLLED_VALIDATION_SERVICE_TOKEN",
        "SOLVER_CONTROLLED_VALIDATION_SERVICE_TOKEN_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        runner,
        "_default_repository_root",
        lambda: (_ for _ in ()).throw(AssertionError("filesystem was touched")),
    )

    assert main() == 2
    assert capsys.readouterr().out == ""


def test_main_failure_output_is_generic_and_contains_no_exception_detail(monkeypatch, capsys):
    sentinel = "SYNTHETIC_PRIVATE_SENTINEL"
    monkeypatch.setenv("SOLVER_CONTROLLED_VALIDATION_ROOT", "synthetic-invalid-root")
    monkeypatch.setenv("SOLVER_CONTROLLED_VALIDATION_TRUST_ROOT", "sha256:" + "1" * 64)
    _connector_environment(monkeypatch)
    monkeypatch.setattr(
        runner,
        "run_bundle",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError(sentinel)),
    )

    assert main() == 1
    rendered = capsys.readouterr()
    assert json.loads(rendered.out) == {
        "status": "failed",
        "error_code": "controlled_validation_failed",
    }
    assert sentinel not in rendered.out
    assert rendered.err == ""


def test_runner_has_no_local_solver_or_temporary_workspace_path():
    source = Path(runner.__file__).read_text(encoding="utf-8")

    assert "LocalProcessSolverBackend" not in source
    assert "TemporaryDirectory" not in source
    assert "_reap_local_worker" not in source
