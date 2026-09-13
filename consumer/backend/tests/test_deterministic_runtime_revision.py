import pytest
from agent.direct_execution import audit
from agent.direct_execution.build_revision import calculate_application_build_revision
from integration.sandbox.runtime_contract import (
    allowed_external_import_roots,
    allowed_runtime_imports,
    allowed_standard_library_imports,
    load_sandbox_runtime_contract,
    sandbox_runner_protocol_version,
    sandbox_runtime_revision,
    sandbox_runtime_revision_file,
    shared_sandbox_runtime_revision_file,
)


def test_sandbox_runtime_contract_is_pinned_and_architecture_specific():
    contract = load_sandbox_runtime_contract()

    assert contract["version"] == 1
    assert len(contract["upstream"]["commit"]) == 40
    assert contract["python"]["executable"] == "/usr/bin/python3"
    assert sandbox_runner_protocol_version() == "v2"
    assert allowed_external_import_roots() == frozenset()
    assert "json" in allowed_standard_library_imports()
    assert "io" not in allowed_standard_library_imports()
    assert "os" not in allowed_standard_library_imports()
    assert allowed_runtime_imports()["shared_capabilities.ontology_script_sdk"] == (
        frozenset(
            {
                "DeterministicProviderCallError",
                "DeterministicProviderClient",
                "write_artifact",
            }
        )
    )
    assert sandbox_runtime_revision_file() == ("/app/sandbox-runtime/python/.runtime-revision")
    assert shared_sandbox_runtime_revision_file() == ("/run/onto-runtime-attestation/revision")
    assert sandbox_runtime_revision("amd64").startswith("sha256:")
    assert sandbox_runtime_revision("amd64") != sandbox_runtime_revision("arm64")
    assert sandbox_runtime_revision("amd64", "sha256:" + "a" * 64) != sandbox_runtime_revision(
        "amd64", "sha256:" + "b" * 64
    )


def test_deployed_runtime_revision_comes_from_shared_attestation(tmp_path, monkeypatch):
    from integration.sandbox import runtime_contract as module

    revision_path = tmp_path / "revision"
    revision_path.write_text("sha256:" + "c" * 64 + "\n", encoding="utf-8")
    contract = dict(module.load_sandbox_runtime_contract())
    contract["shared_revision_file"] = str(revision_path)
    monkeypatch.setattr(module, "load_sandbox_runtime_contract", lambda: contract)
    monkeypatch.delenv("SANDBOX_RUNTIME_REVISION", raising=False)

    assert module.sandbox_runtime_revision() == "sha256:" + "c" * 64


def test_shared_attestation_takes_precedence_over_environment(tmp_path, monkeypatch):
    from integration.sandbox import runtime_contract as module

    revision_path = tmp_path / "revision"
    revision_path.write_text("sha256:" + "d" * 64 + "\n", encoding="utf-8")
    contract = dict(module.load_sandbox_runtime_contract())
    contract["shared_revision_file"] = str(revision_path)
    monkeypatch.setattr(module, "load_sandbox_runtime_contract", lambda: contract)
    monkeypatch.setenv("SANDBOX_RUNTIME_REVISION", "sha256:" + "e" * 64)

    assert module.sandbox_runtime_revision() == "sha256:" + "d" * 64


def test_runtime_revision_uses_application_and_sandbox_runtime_revisions(monkeypatch):
    monkeypatch.setenv("APP_BUILD_SHA", "app-build-a")
    monkeypatch.setattr(audit, "sandbox_runtime_revision", lambda: "sha256:sandbox-a")
    audit._application_build_revision.cache_clear()

    first = audit.runtime_revision()

    monkeypatch.setenv("APP_BUILD_SHA", "app-build-b")
    audit._application_build_revision.cache_clear()
    second = audit.runtime_revision()

    assert first == "deterministic-runner:v2:app-build-a:sha256:sandbox-a"
    assert second == "deterministic-runner:v2:app-build-b:sha256:sandbox-a"
    audit._application_build_revision.cache_clear()


def test_application_revision_covers_all_shipped_files_and_installed_versions(tmp_path):
    application_root = tmp_path / "backend"
    endpoint_security = application_root / "agent" / "tools" / "business_data" / "endpoint_security.py"
    endpoint_security.parent.mkdir(parents=True)
    endpoint_security.write_text("version-one", encoding="utf-8")
    runtime = {
        "implementation": "CPython",
        "python_version": "3.11.0",
        "architecture": "x86_64",
    }

    first = calculate_application_build_revision(
        application_root,
        distributions=[("jsonschema", "4.25.0")],
        runtime=runtime,
    )
    endpoint_security.write_text("version-two", encoding="utf-8")
    source_changed = calculate_application_build_revision(
        application_root,
        distributions=[("jsonschema", "4.25.0")],
        runtime=runtime,
    )
    dependency_changed = calculate_application_build_revision(
        application_root,
        distributions=[("jsonschema", "4.26.0")],
        runtime=runtime,
    )

    assert first != source_changed
    assert source_changed != dependency_changed


def test_baked_application_revision_precedes_environment_override(tmp_path, monkeypatch):
    backend_root = tmp_path / "backend"
    fake_audit_path = backend_root / "agent" / "direct_execution" / "audit.py"
    fake_audit_path.parent.mkdir(parents=True)
    fake_audit_path.write_text("# build probe", encoding="utf-8")
    baked_revision = "sha256:" + "a" * 64
    (backend_root / ".app-runtime-revision").write_text(
        baked_revision + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(audit, "__file__", str(fake_audit_path))
    monkeypatch.setenv("APP_BUILD_SHA", "stale-operator-value")
    audit._application_build_revision.cache_clear()

    assert audit._application_build_revision() == baked_revision
    audit._application_build_revision.cache_clear()


def test_invalid_baked_application_revision_fails_closed(tmp_path, monkeypatch):
    backend_root = tmp_path / "backend"
    fake_audit_path = backend_root / "agent" / "direct_execution" / "audit.py"
    fake_audit_path.parent.mkdir(parents=True)
    fake_audit_path.write_text("# build probe", encoding="utf-8")
    (backend_root / ".app-runtime-revision").write_text("invalid\n", encoding="utf-8")
    monkeypatch.setattr(audit, "__file__", str(fake_audit_path))
    audit._application_build_revision.cache_clear()

    with pytest.raises(RuntimeError, match="application build revision is invalid"):
        audit._application_build_revision()
    audit._application_build_revision.cache_clear()


def test_empty_baked_application_revision_fails_closed(tmp_path, monkeypatch):
    backend_root = tmp_path / "backend"
    fake_audit_path = backend_root / "agent" / "direct_execution" / "audit.py"
    fake_audit_path.parent.mkdir(parents=True)
    fake_audit_path.write_text("# build probe", encoding="utf-8")
    (backend_root / ".app-runtime-revision").write_text("\n", encoding="utf-8")
    monkeypatch.setattr(audit, "__file__", str(fake_audit_path))
    monkeypatch.setenv("APP_BUILD_SHA", "must-not-fallback")
    audit._application_build_revision.cache_clear()

    with pytest.raises(RuntimeError, match="application build revision is invalid"):
        audit._application_build_revision()
    audit._application_build_revision.cache_clear()


def test_unreadable_baked_application_revision_fails_closed(tmp_path, monkeypatch):
    backend_root = tmp_path / "backend"
    fake_audit_path = backend_root / "agent" / "direct_execution" / "audit.py"
    fake_audit_path.parent.mkdir(parents=True)
    fake_audit_path.write_text("# build probe", encoding="utf-8")
    revision_path = backend_root / ".app-runtime-revision"
    revision_path.write_text("sha256:" + "a" * 64, encoding="utf-8")
    original_read_text = type(revision_path).read_text

    def unreadable(path, *args, **kwargs):
        if path == revision_path:
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(audit, "__file__", str(fake_audit_path))
    monkeypatch.setenv("APP_BUILD_SHA", "must-not-fallback")
    monkeypatch.setattr(type(revision_path), "read_text", unreadable)
    audit._application_build_revision.cache_clear()

    with pytest.raises(RuntimeError, match="could not be read"):
        audit._application_build_revision()
    audit._application_build_revision.cache_clear()


def test_empty_shared_sandbox_revision_fails_closed(tmp_path, monkeypatch):
    from integration.sandbox import runtime_contract as module

    revision_path = tmp_path / "revision"
    revision_path.write_text("\n", encoding="utf-8")
    contract = dict(module.load_sandbox_runtime_contract())
    contract["shared_revision_file"] = str(revision_path)
    monkeypatch.setattr(module, "load_sandbox_runtime_contract", lambda: contract)
    monkeypatch.setenv("SANDBOX_RUNTIME_REVISION", "sha256:" + "e" * 64)

    with pytest.raises(RuntimeError, match="shared sandbox runtime revision is invalid"):
        module.sandbox_runtime_revision()


def test_unreadable_shared_sandbox_revision_fails_closed(tmp_path, monkeypatch):
    from integration.sandbox import runtime_contract as module

    revision_path = tmp_path / "revision"
    revision_path.write_text("sha256:" + "a" * 64, encoding="utf-8")
    contract = dict(module.load_sandbox_runtime_contract())
    contract["shared_revision_file"] = str(revision_path)
    monkeypatch.setattr(module, "load_sandbox_runtime_contract", lambda: contract)
    monkeypatch.setenv("SANDBOX_RUNTIME_REVISION", "sha256:" + "e" * 64)
    original_read_text = type(revision_path).read_text

    def unreadable(path, *args, **kwargs):
        if path == revision_path:
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(revision_path), "read_text", unreadable)

    with pytest.raises(RuntimeError, match="could not be read"):
        module.sandbox_runtime_revision()
