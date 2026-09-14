# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

from internal_access import internal
import errno
import importlib
import json
import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import workspace.workspaces as workspaces


def _catalog_module():
    return importlib.import_module("workspace.catalog_snapshot")


def _write_workspace(
    runtime_root,
    workspace_id: str,
    *,
    name: str,
    owner: str | None = None,
    members: list[str] | None = None,
    endpoint_id: str = "0123456789abcdef",
):
    workspace_dir = runtime_root / "workspaces" / workspace_id
    workspace_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = workspace_dir / "workspace.json"
    metadata_path.write_text(
        json.dumps(
            {
                "id": workspace_id,
                "name": name,
                "description": "",
                "mcp_endpoint_id": endpoint_id,
                "created_at": "2026-08-31T00:00:00+00:00",
                "updated_at": "2026-08-31T00:00:00+00:00",
                "owner": owner,
                "members": list(members or []),
            }
        ),
        encoding="utf-8",
    )
    return metadata_path


def _enable_snapshot(monkeypatch, runtime_root) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.setenv("PERF_WORKSPACE_SNAPSHOT_ENABLED", "true")


def _symlink_or_skip(link, target, *, target_is_directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        if target_is_directory and os.name == "nt":
            command_prompt = shutil.which("cmd")
            if command_prompt is None:
                pytest.skip("Windows command prompt is unavailable")
            result = subprocess.run(
                [str(Path(command_prompt).resolve()), "/c", "mklink", "/J", str(link), str(target)],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return
        pytest.skip(f"symbolic links are unavailable: {exc}")


def test_catalog_public_api_exists():
    catalog = _catalog_module()

    assert catalog.WorkspaceCatalogSnapshot
    assert callable(workspaces.get_workspace_catalog_snapshot)
    assert callable(workspaces.get_workspace_catalog_generation)
    assert callable(workspaces.get_workspace_config_generation)
    assert callable(workspaces.invalidate_workspace_catalog)


def test_workspaces_root_symlink_is_rejected_by_signature_list_and_catalog(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime"
    external_runtime_root = tmp_path / "external-runtime"
    _write_workspace(external_runtime_root, "evil", name="External Evil")
    runtime_root.mkdir()
    _symlink_or_skip(
        runtime_root / "workspaces",
        external_runtime_root / "workspaces",
        target_is_directory=True,
    )
    _enable_snapshot(monkeypatch, runtime_root)

    with pytest.raises(ValueError, match="symlink|junction"):
        internal(workspaces).workspace_source_signature(runtime_root)
    with pytest.raises(ValueError, match="symlink|junction"):
        internal(workspaces).list_workspace_dirs()
    with pytest.raises(ValueError, match="symlink|junction"):
        workspaces.list_workspaces()


def test_workspace_directory_symlink_never_enters_catalog_or_mutation_paths(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime"
    external_runtime_root = tmp_path / "external-runtime"
    external_metadata = _write_workspace(
        external_runtime_root,
        "evil",
        name="External Evil",
    )
    (runtime_root / "workspaces").mkdir(parents=True)
    linked_workspace = runtime_root / "workspaces" / "evil"
    _symlink_or_skip(
        linked_workspace,
        external_metadata.parent,
        target_is_directory=True,
    )
    _enable_snapshot(monkeypatch, runtime_root)
    original_external_bytes = external_metadata.read_bytes()

    with pytest.raises(ValueError, match="symlink|junction"):
        internal(workspaces).workspace_source_signature(runtime_root)
    with pytest.raises(ValueError, match="symlink|junction"):
        internal(workspaces).list_workspace_dirs()
    with pytest.raises(ValueError, match="symlink|junction"):
        workspaces.get_workspace_paths("evil")
    with pytest.raises(ValueError, match="symlink|junction"):
        workspaces.update_workspace("evil", name="Compromised")
    with pytest.raises(ValueError, match="symlink|junction"):
        workspaces.delete_workspace("evil", allow_default=True)

    decision = workspaces.evaluate_workspace_subject_access("mallory", "evil")
    assert decision.allowed is False
    assert decision.reason == "workspace_metadata_invalid"
    assert external_metadata.read_bytes() == original_external_bytes
    assert internal(workspaces).is_link_or_junction(linked_workspace)


def test_workspace_metadata_symlink_is_rejected_across_all_catalog_paths(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime"
    external_runtime_root = tmp_path / "external-runtime"
    external_metadata = _write_workspace(
        external_runtime_root,
        "evil",
        name="External Evil",
    )
    workspace_dir = runtime_root / "workspaces" / "evil"
    workspace_dir.mkdir(parents=True)
    linked_metadata = workspace_dir / "workspace.json"
    _symlink_or_skip(
        linked_metadata,
        external_metadata,
        target_is_directory=False,
    )
    _enable_snapshot(monkeypatch, runtime_root)
    original_external_bytes = external_metadata.read_bytes()

    with pytest.raises(ValueError, match="symlink|junction"):
        internal(workspaces).workspace_source_signature(runtime_root)
    with pytest.raises(ValueError, match="symlink|junction"):
        internal(workspaces).list_workspace_dirs()
    with pytest.raises(ValueError, match="symlink|junction"):
        internal(workspaces).read_workspace_metadata_raw(linked_metadata)
    with pytest.raises(ValueError, match="symlink|junction"):
        workspaces.get_workspace_paths("evil")
    with pytest.raises(ValueError, match="symlink|junction"):
        workspaces.update_workspace("evil", name="Compromised")

    decision = workspaces.evaluate_workspace_subject_access("mallory", "evil")
    assert decision.allowed is False
    assert decision.reason == "workspace_metadata_invalid"
    assert external_metadata.read_bytes() == original_external_bytes
    assert linked_metadata.is_symlink()


def test_metadata_link_detection_is_wired_into_signature_list_and_paths(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime"
    metadata_path = _write_workspace(runtime_root, "alpha", name="Alpha")
    _enable_snapshot(monkeypatch, runtime_root)
    catalog = _catalog_module()

    def metadata_is_link(path):
        return path == metadata_path

    monkeypatch.setattr(catalog, "_is_link_or_junction", metadata_is_link)
    monkeypatch.setattr(workspaces, "_is_link_or_junction", metadata_is_link)

    with pytest.raises(ValueError, match="symlink|junction"):
        internal(workspaces).workspace_source_signature(runtime_root)
    with pytest.raises(ValueError, match="symlink|junction"):
        internal(workspaces).list_workspace_dirs()
    with pytest.raises(ValueError, match="symlink|junction"):
        workspaces.get_workspace_paths("alpha")


def test_metadata_reader_rejects_paths_outside_direct_trusted_child(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    external_metadata = _write_workspace(
        tmp_path / "external-runtime",
        "evil",
        name="External Evil",
    )
    nested_metadata = runtime_root / "workspaces" / "alpha" / "nested" / "workspace.json"
    nested_metadata.parent.mkdir(parents=True)
    nested_metadata.write_text(external_metadata.read_text(encoding="utf-8"), encoding="utf-8")

    for metadata_path in (external_metadata, nested_metadata):
        with pytest.raises(ValueError, match="trusted|direct child"):
            internal(workspaces).read_workspace_metadata_raw(metadata_path)


def test_acl_fails_closed_when_workspace_path_validation_fails(monkeypatch):
    def invalid_workspace_path(_workspace_id):
        raise ValueError("workspace path escaped trusted root")

    monkeypatch.setattr(workspaces, "get_workspace_paths", invalid_workspace_path)

    decision = workspaces.evaluate_workspace_subject_access("alice", "alpha")

    assert decision.allowed is False
    assert decision.reason == "workspace_metadata_invalid"


def test_warm_catalog_reads_do_no_authoritative_io_and_resolve_in_constant_time(
    tmp_path,
    monkeypatch,
):
    from api.auth.auth import AuthUser

    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    workspaces.create_workspace("Beta", workspace_id="beta", auto_set_default=False)
    snapshot = workspaces.get_workspace_catalog_snapshot()

    def unexpected_io(*_args, **_kwargs):
        raise AssertionError("warm catalog read performed authoritative I/O")

    monkeypatch.setattr(workspaces, "_list_workspace_dirs", unexpected_io)
    monkeypatch.setattr(workspaces, "_read_workspace_metadata_raw", unexpected_io)
    monkeypatch.setattr(workspaces, "_workspace_source_signature", unexpected_io)
    monkeypatch.setattr(
        workspaces,
        "_current_request_user",
        lambda: AuthUser(username="bob", role="user"),
    )
    monkeypatch.setattr(workspaces, "list_workspaces", unexpected_io)
    monkeypatch.setattr(workspaces, "workspace_exists", unexpected_io)
    monkeypatch.setattr(workspaces, "get_default_workspace_id", unexpected_io)

    assert workspaces.get_workspace_catalog_snapshot() is snapshot
    assert snapshot.copy_items()[0]["id"] == "alpha"
    assert snapshot.copy_item("beta")["name"] == "Beta"
    assert snapshot.default_workspace_id == "alpha"
    assert snapshot.contains("beta") is True
    assert workspaces.resolve_workspace_id(SimpleNamespace(headers={workspaces.WORKSPACE_HEADER: "beta"})) == "beta"


def test_catalog_and_public_reads_are_defensive_copies(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", owner="alice", set_as_default=True)
    workspaces.update_workspace("alpha", members=["bob"])

    snapshot = workspaces.get_workspace_catalog_snapshot()
    with pytest.raises(TypeError):
        snapshot.items_by_id["alpha"]["name"] = "corrupted"
    assert isinstance(snapshot.items_by_id["alpha"]["members"], tuple)

    listed = workspaces.list_workspaces()
    listed[0]["name"] = "corrupted"
    listed[0]["members"].append("mallory")
    fetched = workspaces.get_workspace("alpha")
    fetched["description"] = "corrupted"

    assert workspaces.list_workspaces()[0]["name"] == "Alpha"
    assert workspaces.get_workspace("alpha")["members"] == ["bob"]
    assert workspaces.get_workspace("alpha")["description"] == ""


def test_external_drift_is_checked_only_after_throttle_window(tmp_path, monkeypatch):
    catalog = _catalog_module()
    now = [100.0]
    monkeypatch.setattr(catalog.time, "monotonic", lambda: now[0])
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    assert [item["id"] for item in workspaces.list_workspaces()] == ["alpha"]

    _write_workspace(
        runtime_root,
        "beta",
        name="Beta",
        endpoint_id="fedcba9876543210",
    )
    now[0] += 1.0
    assert [item["id"] for item in workspaces.list_workspaces()] == ["alpha"]

    now[0] += 0.51
    assert [item["id"] for item in workspaces.list_workspaces()] == [
        "alpha",
        "beta",
    ]


def test_catalog_generation_refreshes_external_drift_after_throttle(
    tmp_path,
    monkeypatch,
):
    catalog = _catalog_module()
    now = [200.0]
    monkeypatch.setattr(catalog.time, "monotonic", lambda: now[0])
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    before = workspaces.get_workspace_catalog_snapshot()

    _write_workspace(
        runtime_root,
        "beta",
        name="Beta",
        endpoint_id="fedcba9876543210",
    )
    now[0] += 1.51

    generation = workspaces.get_workspace_catalog_generation()

    assert generation > before.generation
    assert workspaces.get_workspace_catalog_snapshot().contains("beta")


@pytest.mark.parametrize(
    "error_number",
    [errno.ENOENT, errno.EAGAIN, errno.EINTR, errno.EBUSY],
)
def test_transient_drift_failure_serves_last_good_snapshot(
    tmp_path,
    monkeypatch,
    error_number,
):
    catalog = _catalog_module()
    now = [10.0]
    monkeypatch.setattr(catalog.time, "monotonic", lambda: now[0])
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    before = workspaces.get_workspace_catalog_snapshot()

    monkeypatch.setattr(
        workspaces,
        "_workspace_source_signature",
        lambda _root: object(),
    )

    def transient_failure(_root):
        raise OSError(
            error_number,
            "external atomic replacement in progress",
        )

    monkeypatch.setattr(
        workspaces,
        "_load_workspace_catalog_data",
        transient_failure,
    )
    now[0] += 1.51

    assert workspaces.get_workspace_catalog_snapshot() is before
    assert workspaces.list_workspaces()[0]["id"] == "alpha"


@pytest.mark.parametrize("error_number", [errno.ENOTDIR, errno.ELOOP])
def test_structural_drift_os_errors_fail_closed(
    tmp_path,
    monkeypatch,
    error_number,
):
    catalog = _catalog_module()
    now = [15.0]
    monkeypatch.setattr(catalog.time, "monotonic", lambda: now[0])
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    before = workspaces.get_workspace_catalog_snapshot()

    monkeypatch.setattr(
        workspaces,
        "_workspace_source_signature",
        lambda _root: object(),
    )

    def structural_failure(_root):
        raise OSError(error_number, "workspace path is structurally invalid")

    monkeypatch.setattr(
        workspaces,
        "_load_workspace_catalog_data",
        structural_failure,
    )
    now[0] += 1.51

    with pytest.raises(OSError) as raised:
        workspaces.get_workspace_catalog_snapshot()

    assert raised.value.errno == error_number
    assert workspaces.get_workspace_catalog_generation() > before.generation


def test_security_drift_failure_is_fail_closed(tmp_path, monkeypatch):
    catalog = _catalog_module()
    now = [20.0]
    monkeypatch.setattr(catalog.time, "monotonic", lambda: now[0])
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    before = workspaces.get_workspace_catalog_snapshot()
    original_signature = internal(workspaces).workspace_source_signature
    original_loader = internal(workspaces).load_workspace_catalog_data

    monkeypatch.setattr(
        workspaces,
        "_workspace_source_signature",
        lambda _root: object(),
    )

    def security_failure(_root):
        raise PermissionError("workspace metadata access denied")

    monkeypatch.setattr(
        workspaces,
        "_load_workspace_catalog_data",
        security_failure,
    )
    now[0] += 1.51

    with pytest.raises(PermissionError, match="access denied"):
        workspaces.get_workspace_catalog_snapshot()
    failed_generation = workspaces.get_workspace_catalog_generation()
    assert failed_generation > before.generation
    with pytest.raises(PermissionError, match="access denied"):
        workspaces.get_workspace_catalog_snapshot()

    monkeypatch.setattr(
        workspaces,
        "_workspace_source_signature",
        original_signature,
    )
    monkeypatch.setattr(
        workspaces,
        "_load_workspace_catalog_data",
        original_loader,
    )
    recovered = workspaces.get_workspace_catalog_snapshot()
    assert recovered.generation == failed_generation
    assert recovered.copy_item("alpha")["name"] == "Alpha"


def test_feature_flag_false_uses_original_authoritative_scan(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.setenv("PERF_WORKSPACE_SNAPSHOT_ENABLED", "false")
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)

    calls = {"dirs": 0, "json": 0}
    original_dirs = internal(workspaces).list_workspace_dirs
    original_json = internal(workspaces).read_workspace_metadata_raw

    def count_dirs():
        calls["dirs"] += 1
        return original_dirs()

    def count_json(path):
        calls["json"] += 1
        return original_json(path)

    monkeypatch.setattr(workspaces, "_list_workspace_dirs", count_dirs)
    monkeypatch.setattr(workspaces, "_read_workspace_metadata_raw", count_json)

    assert workspaces.list_workspaces()[0]["id"] == "alpha"
    assert workspaces.list_workspaces()[0]["id"] == "alpha"
    assert calls["dirs"] >= 2
    assert calls["json"] >= 2


def test_metadata_mutations_invalidate_catalog_before_return(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    generation = workspaces.get_workspace_catalog_snapshot().generation

    updated = workspaces.update_workspace("alpha", name="Alpha 2")
    assert updated["name"] == "Alpha 2"
    assert workspaces.get_workspace_catalog_generation() > generation
    generation = workspaces.get_workspace_catalog_generation()

    updated = workspaces.update_workspace("alpha", members=["alice"])
    assert updated["members"] == ["alice"]
    assert workspaces.get_workspace_catalog_generation() > generation
    generation = workspaces.get_workspace_catalog_generation()

    created = workspaces.create_workspace("Beta", workspace_id="beta", auto_set_default=False)
    assert created["id"] == "beta"
    assert workspaces.get_workspace_catalog_generation() > generation
    generation = workspaces.get_workspace_catalog_generation()

    default = workspaces.set_default_workspace("beta")
    assert default["is_default"] is True
    assert workspaces.get_workspace_catalog_generation() > generation
    generation = workspaces.get_workspace_catalog_generation()

    workspaces.delete_workspace("alpha")
    assert workspaces.get_workspace_catalog_generation() > generation
    assert workspaces.workspace_exists("alpha") is False


def test_failed_workspace_delete_preserves_default_and_catalog_generation(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    before = workspaces.get_workspace_catalog_snapshot()

    monkeypatch.setattr(
        workspaces.shutil,
        "rmtree",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(OSError, match="still exists"):
        workspaces.delete_workspace("alpha", allow_default=True)

    assert workspaces.get_default_workspace_id() == "alpha"
    assert workspaces.get_workspace_catalog_generation() == before.generation
    assert workspaces.get_workspace("alpha")["id"] == "alpha"


def test_failed_metadata_write_does_not_advance_catalog_generation(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    before = workspaces.get_workspace_catalog_snapshot().generation

    def fail_write(_paths, _payload):
        raise OSError("simulated write failure")

    monkeypatch.setattr(workspaces, "_write_workspace_metadata", fail_write)
    with pytest.raises(OSError, match="simulated write failure"):
        workspaces.update_workspace("alpha", name="not persisted")

    assert workspaces.get_workspace_catalog_generation() == before
    assert workspaces.get_workspace("alpha")["name"] == "Alpha"


def test_workspace_metadata_write_is_durable_before_catalog_publication(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    before_generation = workspaces.get_workspace_catalog_snapshot().generation
    metadata_path = runtime_root / "workspaces" / "alpha" / "workspace.json"
    calls = []
    original_fsync = workspaces.os.fsync
    original_replace = workspaces.os.replace

    def recording_fsync(descriptor):
        calls.append("file_fsync")
        return original_fsync(descriptor)

    def recording_replace(source, target):
        calls.append(("replace", source, target))
        return original_replace(source, target)

    monkeypatch.setattr(workspaces.os, "fsync", recording_fsync)
    monkeypatch.setattr(workspaces.os, "replace", recording_replace)
    monkeypatch.setattr(
        workspaces,
        "_fsync_directory",
        lambda path: calls.append(("directory_fsync", path)),
        raising=False,
    )

    workspaces.update_workspace("alpha", name="Alpha Updated")

    replace_call = next(call for call in calls if isinstance(call, tuple) and call[0] == "replace")
    assert replace_call[1].parent == metadata_path.parent
    assert replace_call[2] == metadata_path
    assert calls[0] == "file_fsync"
    assert calls.index(replace_call) < calls.index(("directory_fsync", metadata_path.parent))
    assert workspaces.get_workspace_catalog_generation() > before_generation


def test_directory_fsync_uses_and_closes_directory_descriptor_on_posix(
    tmp_path,
    monkeypatch,
):
    calls = []
    expected_flags = workspaces.os.O_RDONLY | getattr(
        workspaces.os,
        "O_DIRECTORY",
        0,
    )
    monkeypatch.setattr(workspaces, "_IS_WINDOWS", False)
    monkeypatch.setattr(
        workspaces.os,
        "open",
        lambda path, flags, mode: calls.append(("open", path, flags, mode)) or 73,
    )
    monkeypatch.setattr(
        workspaces.os,
        "fsync",
        lambda descriptor: calls.append(("fsync", descriptor)),
    )
    monkeypatch.setattr(
        workspaces.os,
        "close",
        lambda descriptor: calls.append(("close", descriptor)),
    )

    internal(workspaces).fsync_directory(tmp_path)

    assert calls == [
        ("open", tmp_path, expected_flags, 0o700),
        ("fsync", 73),
        ("close", 73),
    ]


def test_directory_fsync_is_a_noop_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(workspaces, "_IS_WINDOWS", True)

    def unexpected_open(*_args, **_kwargs):
        raise AssertionError("Windows directory fsync must not open a directory")

    monkeypatch.setattr(workspaces.os, "open", unexpected_open)

    internal(workspaces).fsync_directory(tmp_path)


def test_pre_replace_failure_and_cleanup_failure_preserve_primary_error(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    before_generation = workspaces.get_workspace_catalog_snapshot().generation
    replace_error = OSError("synthetic replace failure")
    original_unlink = workspaces.Path.unlink

    def fail_replace(_source, _target):
        raise replace_error

    def fail_temporary_cleanup(path, *args, **kwargs):
        if path.name.endswith(".tmp"):
            raise OSError("synthetic cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(workspaces.os, "replace", fail_replace)
    monkeypatch.setattr(workspaces.Path, "unlink", fail_temporary_cleanup)

    with pytest.raises(OSError) as raised:
        workspaces.update_workspace("alpha", name="Not Persisted")

    assert raised.value is replace_error
    assert workspaces.get_workspace_catalog_generation() == before_generation
    assert workspaces.get_workspace("alpha")["name"] == "Alpha"


def test_committed_metadata_durability_error_invalidates_before_rethrow(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    before_generation = workspaces.get_workspace_catalog_snapshot().generation
    fsync_error = OSError("synthetic directory fsync failure")

    def fail_directory_fsync(_path):
        raise fsync_error

    monkeypatch.setattr(workspaces, "_fsync_directory", fail_directory_fsync)

    with pytest.raises(workspaces.WorkspaceDurabilityError) as raised:
        workspaces.update_workspace("alpha", name="Alpha Committed")

    assert raised.value.committed is True
    assert raised.value.__cause__ is fsync_error
    assert workspaces.get_workspace_catalog_generation() > before_generation
    assert workspaces.get_workspace("alpha")["name"] == "Alpha Committed"


def test_committed_settings_durability_error_invalidates_default_before_rethrow(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    workspaces.create_workspace("Beta", workspace_id="beta", auto_set_default=False)
    before_generation = workspaces.get_workspace_catalog_snapshot().generation

    def fail_directory_fsync(_path):
        raise OSError("directory fsync failed")

    monkeypatch.setattr(workspaces, "_fsync_directory", fail_directory_fsync)

    with pytest.raises(workspaces.WorkspaceDurabilityError):
        workspaces.set_default_workspace("beta")

    assert workspaces.get_workspace_catalog_generation() > before_generation
    assert workspaces.get_default_workspace_id() == "beta"


def test_runtime_config_advances_only_corresponding_config_generation(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    workspaces.create_workspace("Beta", workspace_id="beta", auto_set_default=False)
    snapshot = workspaces.get_workspace_catalog_snapshot()
    catalog_generation = snapshot.generation
    alpha_generation = workspaces.get_workspace_config_generation("alpha")
    beta_generation = workspaces.get_workspace_config_generation("beta")

    def unexpected_reload(_root):
        raise AssertionError("runtime config update reloaded workspace catalog")

    monkeypatch.setattr(
        workspaces,
        "_load_workspace_catalog_data",
        unexpected_reload,
    )
    updated = workspaces.update_workspace_runtime_config("alpha", {"lang": {"enabled": True}})

    assert updated["lang"]["enabled"] is True
    assert workspaces.get_workspace_config_generation("alpha") > alpha_generation
    assert workspaces.get_workspace_config_generation("beta") == beta_generation
    assert workspaces.get_workspace_catalog_generation() == catalog_generation
    assert workspaces.get_workspace_catalog_snapshot() is snapshot


def test_failed_runtime_config_write_does_not_advance_config_generation(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    before = workspaces.get_workspace_config_generation("alpha")

    def fail_write(_path, _payload):
        raise OSError("simulated runtime write failure")

    monkeypatch.setattr(workspaces, "_write_json_atomic", fail_write)
    with pytest.raises(OSError, match="simulated runtime write failure"):
        workspaces.update_workspace_runtime_config("alpha", {"lang": {"enabled": True}})

    assert workspaces.get_workspace_config_generation("alpha") == before


def test_committed_runtime_config_durability_error_advances_only_workspace_generation(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    workspaces.create_workspace("Beta", workspace_id="beta", auto_set_default=False)
    alpha_before = workspaces.get_workspace_config_generation("alpha")
    beta_before = workspaces.get_workspace_config_generation("beta")

    def fail_directory_fsync(_path):
        raise OSError("directory fsync failed")

    monkeypatch.setattr(workspaces, "_fsync_directory", fail_directory_fsync)

    with pytest.raises(workspaces.WorkspaceDurabilityError):
        workspaces.update_workspace_runtime_config("alpha", {"lang": {"enabled": True}})

    assert workspaces.get_workspace_config_generation("alpha") > alpha_before
    assert workspaces.get_workspace_config_generation("beta") == beta_before
    assert workspaces.get_workspace_runtime_config("alpha")["lang"]["enabled"] is True


def test_legacy_runtime_config_migration_advances_workspace_config_generation(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    runtime_config = runtime_root / "workspaces" / "alpha" / "runtime_config.json"
    runtime_config.write_text(
        json.dumps({"llm": {"default_profile_ref": {"scope": "global", "id": "main"}}}),
        encoding="utf-8",
    )
    before = workspaces.get_workspace_config_generation("alpha")

    loaded = workspaces.get_workspace_runtime_config("alpha")

    assert loaded["llm"]["default_main_profile_ref"] == {
        "scope": "global",
        "id": "main",
    }
    assert workspaces.get_workspace_config_generation("alpha") > before


def test_legacy_workspace_and_mcp_migrations_invalidate_catalog(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    _write_workspace(runtime_root, "default", name="Legacy Workspace")
    initial_generation = workspaces.get_workspace_catalog_generation()

    migrated = workspaces.list_workspaces()

    assert migrated[0]["id"] != "default"
    assert workspaces.get_workspace_catalog_generation() > initial_generation

    migrated_id = migrated[0]["id"]
    metadata_path = runtime_root / "workspaces" / migrated_id / "workspace.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload.pop("mcp_endpoint_id", None)
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")
    workspaces.invalidate_workspace_catalog(workspace_id=migrated_id)
    before_mcp = workspaces.get_workspace_catalog_snapshot().generation

    result = workspaces.ensure_all_workspace_mcp_endpoint_ids(runtime_root)

    assert result["ok"] is True
    assert result["updated_workspace_ids"] == [migrated_id]
    assert workspaces.get_workspace_catalog_generation() > before_mcp


def test_committed_mcp_migration_error_invalidates_catalog(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    metadata_path = runtime_root / "workspaces" / "alpha" / "workspace.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload.pop("mcp_endpoint_id")
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")
    workspaces.invalidate_workspace_catalog(workspace_id="alpha")
    before_generation = workspaces.get_workspace_catalog_snapshot().generation

    def fail_directory_fsync(_path):
        raise OSError("directory fsync failed")

    monkeypatch.setattr(workspaces, "_fsync_directory", fail_directory_fsync)

    result = workspaces.ensure_all_workspace_mcp_endpoint_ids(runtime_root)

    assert result["ok"] is False
    assert result["error_codes"] == ["WORKSPACE_MCP_ENDPOINT_WRITE_FAILED"]
    assert workspaces.get_workspace_catalog_generation() > before_generation
    persisted = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert workspaces.validate_mcp_endpoint_id(persisted["mcp_endpoint_id"])


def test_user_cleanup_member_write_invalidates_catalog(tmp_path, monkeypatch):
    from api.auth import user_deletion

    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Private", workspace_id="private", owner="admin", set_as_default=True)
    workspaces.update_workspace("private", members=["alice"])
    before = workspaces.get_workspace_catalog_snapshot().generation
    monkeypatch.setattr(user_deletion, "_archive_workspace_runtime", lambda *_args: [])
    monkeypatch.setattr(user_deletion, "_archive_memory", lambda *_args: [])
    monkeypatch.setattr(user_deletion, "revoke_all_mcp_tokens_for_user", lambda **_kwargs: None)
    monkeypatch.setattr(user_deletion, "_write_deletion_manifest", lambda **_kwargs: None)

    completed, errors = internal(user_deletion).cleanup_deleted_user_artifacts(
        "alice",
        operation_id="a" * 32,
    )

    assert errors == {}
    assert "workspace_members" in completed
    assert workspaces.get_workspace_catalog_generation() > before
    assert workspaces.get_workspace("private")["members"] == []


def test_user_cleanup_boundary_backstops_member_catalog_invalidation(
    tmp_path,
    monkeypatch,
):
    from api.auth import user_deletion

    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Private", workspace_id="private", owner="admin", set_as_default=True)
    before = workspaces.get_workspace_catalog_snapshot().generation
    monkeypatch.setattr(
        user_deletion,
        "remove_user_from_workspace_members",
        lambda _username: ["private"],
    )

    updated = internal(user_deletion).remove_user_from_workspace_members_and_invalidate("alice")

    assert updated == ["private"]
    assert workspaces.get_workspace_catalog_generation() > before


def test_catalog_generation_never_regresses_when_runtime_root_changes(
    tmp_path,
    monkeypatch,
):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    current_root = [first_root]
    monkeypatch.setenv("PERF_WORKSPACE_SNAPSHOT_ENABLED", "true")
    monkeypatch.setattr(
        workspaces,
        "get_runtime_root",
        lambda: current_root[0].resolve(),
    )
    workspaces.create_workspace("First", workspace_id="first", set_as_default=True)
    first_generation = workspaces.get_workspace_catalog_snapshot().generation

    current_root[0] = second_root
    workspaces.create_workspace("Second", workspace_id="second", set_as_default=True)
    second_generation = workspaces.get_workspace_catalog_snapshot().generation

    current_root[0] = first_root
    returned_generation = workspaces.get_workspace_catalog_snapshot().generation

    assert second_generation > first_generation
    assert returned_generation > second_generation


def test_catalog_publication_has_no_stale_window_during_metadata_write(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    workspaces.get_workspace_catalog_snapshot()
    persisted = threading.Event()
    allow_return = threading.Event()
    original_write = internal(workspaces).write_workspace_metadata

    def blocking_write(paths, payload):
        original_write(paths, payload)
        persisted.set()
        assert allow_return.wait(timeout=5)

    monkeypatch.setattr(workspaces, "_write_workspace_metadata", blocking_write)
    with ThreadPoolExecutor(max_workers=2) as executor:
        update_future = executor.submit(
            workspaces.update_workspace,
            "alpha",
            name="Alpha Updated",
        )
        assert persisted.wait(timeout=5)
        read_future = executor.submit(workspaces.get_workspace, "alpha")
        assert not read_future.done()
        allow_return.set()

        assert update_future.result(timeout=5)["name"] == "Alpha Updated"
        assert read_future.result(timeout=5)["name"] == "Alpha Updated"


def test_acl_is_re_evaluated_per_request_instead_of_cached_in_catalog(
    tmp_path,
    monkeypatch,
):
    from api.auth.auth import AuthUser
    from api.exceptions import APIError

    runtime_root = tmp_path / "runtime"
    _enable_snapshot(monkeypatch, runtime_root)
    workspaces.create_workspace(
        "Private",
        workspace_id="private",
        owner="alice",
        set_as_default=False,
        auto_set_default=False,
    )
    snapshot = workspaces.get_workspace_catalog_snapshot()
    assert snapshot.copy_item("private")["members"] == []
    current_user = [AuthUser(username="bob", role="user")]
    monkeypatch.setattr(
        workspaces,
        "_current_request_user",
        lambda: current_user[0],
    )

    request = SimpleNamespace(headers={workspaces.WORKSPACE_HEADER: "private"})
    with pytest.raises(APIError):
        workspaces.resolve_workspace_id(request)

    current_user[0] = AuthUser(username="alice", role="user")
    assert workspaces.resolve_workspace_id(request) == "private"
    assert workspaces.get_workspace_catalog_snapshot() is snapshot
    assert snapshot.copy_item("private")["members"] == []
