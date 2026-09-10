from __future__ import annotations

import errno
import json
import os
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from backend_synthetic_credentials import synthetic_secret
from config.config_snapshot import (
    ConfigSnapshot,
    ConfigSnapshotManager,
    ConfigSourceSignature,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SecurityConfigError(RuntimeError):
    pass


class DeepCopyProbe:
    def __init__(self) -> None:
        self.calls = 0

    def __deepcopy__(self, _memo):
        self.calls += 1
        return self


@pytest.fixture
def snapshot_manager(tmp_path):
    source_path = tmp_path / "system_config.json"
    source_path.write_text('{"context_max_rounds": 10}', encoding="utf-8")
    clock = FakeClock()
    calls = {"load": 0}

    def loader(environment):
        calls["load"] += 1
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        return {
            "agent": payload,
            "environment_value": environment["CONFIG_TEST_VALUE"],
        }

    environment = {"CONFIG_TEST_VALUE": "frozen"}
    manager = ConfigSnapshotManager(
        source_path=source_path,
        loader=loader,
        environment=environment,
        clock=clock,
    )
    return manager, source_path, environment, clock, calls


def test_snapshot_value_and_signature_are_immutable(tmp_path):
    source_path = tmp_path / "system_config.json"
    source_path.write_text("{}", encoding="utf-8")
    signature = ConfigSourceSignature.from_path(source_path)
    snapshot = ConfigSnapshot(
        generation=3,
        source_signature=signature,
        value={"agent": {"context_max_rounds": 10}},
    )

    with pytest.raises(FrozenInstanceError):
        signature.size = 42
    with pytest.raises(FrozenInstanceError):
        snapshot.generation = 4
    with pytest.raises(TypeError):
        snapshot.value["agent"]["context_max_rounds"] = 999


def test_snapshot_reuses_one_validated_load_until_generation_changes(snapshot_manager):
    manager, _, _, _, calls = snapshot_manager

    first = manager.load()
    second = manager.load()

    assert first == second
    assert calls["load"] == 1
    assert manager.generation == 0

    assert manager.invalidate() == 1
    assert manager.load() == first
    assert calls["load"] == 2
    assert manager.generation == 1


def test_snapshot_manager_preserves_monotonic_initial_generation(tmp_path):
    source_path = tmp_path / "system_config.json"
    source_path.write_text('{"value": 1}', encoding="utf-8")
    clock = FakeClock()
    manager = ConfigSnapshotManager(
        source_path=source_path,
        loader=lambda _environment: json.loads(source_path.read_text(encoding="utf-8")),
        environment={},
        clock=clock,
        initial_generation=7,
    )

    assert manager.generation == 7
    assert manager.load() == {"value": 1}
    assert manager.invalidate() == 8
    assert manager.load() == {"value": 1}

    source_path.write_text('{"value": 2, "changed": true}', encoding="utf-8")
    clock.advance(1.5)
    assert manager.load()["value"] == 2
    assert manager.generation == 9


def test_snapshot_returns_defensive_copy(snapshot_manager):
    manager, _, _, _, _ = snapshot_manager

    first = manager.load()
    first["agent"]["context_max_rounds"] = 999

    assert manager.load()["agent"]["context_max_rounds"] == 10


def test_snapshot_section_copy_does_not_traverse_other_sections(tmp_path):
    source_path = tmp_path / "system_config.json"
    source_path.write_text("{}", encoding="utf-8")
    probe = DeepCopyProbe()
    snapshot = ConfigSnapshot(
        generation=1,
        source_signature=ConfigSourceSignature.from_path(source_path),
        value={
            "storage": {"project_root": "/runtime/root"},
            "auth": {"secret_probe": probe},
        },
    )
    probe.calls = 0

    storage = snapshot.copy_section("storage")
    storage["project_root"] = "/mutated"

    assert probe.calls == 0
    assert snapshot.copy_section("storage")["project_root"] == "/runtime/root"


def test_snapshot_manager_section_copy_runs_outside_manager_lock(tmp_path, monkeypatch):
    import config.config_snapshot as snapshot_module

    source_path = tmp_path / "system_config.json"
    source_path.write_text("{}", encoding="utf-8")
    clock = FakeClock()
    manager = ConfigSnapshotManager(
        source_path=source_path,
        loader=lambda _environment: {
            "storage": "/runtime/root",
            "auth": {"auth_secret": synthetic_secret("config_snapshot-credential-04")},
        },
        environment={},
        clock=clock,
    )
    manager.refresh_generation()
    original_copy = snapshot_module._copy_value
    first_copy_started = threading.Event()
    second_copy_started = threading.Event()
    release_first_copy = threading.Event()
    copy_calls = []
    errors = []

    def controlled_copy(value):
        call_number = len(copy_calls) + 1
        copy_calls.append(call_number)
        if call_number == 1:
            first_copy_started.set()
            if not release_first_copy.wait(timeout=2):
                raise TimeoutError("second section copy was blocked by manager lock")
        else:
            second_copy_started.set()
        return original_copy(value)

    monkeypatch.setattr(snapshot_module, "_copy_value", controlled_copy)

    def load_storage():
        try:
            manager.load_section("storage")
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=load_storage)
    second = threading.Thread(target=load_storage)
    first.start()
    assert first_copy_started.wait(timeout=2)
    second.start()
    try:
        assert second_copy_started.wait(timeout=0.5)
    finally:
        release_first_copy.set()
        first.join(timeout=2)
        second.join(timeout=2)

    assert errors == []
    assert copy_calls == [1, 2]


def test_snapshot_freezes_environment_at_manager_creation(snapshot_manager):
    manager, _, environment, _, _ = snapshot_manager
    environment["CONFIG_TEST_VALUE"] = "changed"

    first = manager.load()
    manager.invalidate()
    second = manager.load()

    assert first["environment_value"] == "frozen"
    assert second["environment_value"] == "frozen"


def test_snapshot_throttles_source_signature_checks_for_one_point_five_seconds(
    snapshot_manager,
):
    manager, source_path, _, clock, calls = snapshot_manager
    assert manager.check_interval_seconds == 1.5
    assert manager.load()["agent"]["context_max_rounds"] == 10

    source_path.write_text('{"context_max_rounds": 20, "changed": true}', encoding="utf-8")
    clock.advance(1.49)
    assert manager.load()["agent"]["context_max_rounds"] == 10
    assert calls["load"] == 1

    clock.advance(0.01)
    assert manager.load()["agent"]["context_max_rounds"] == 20
    assert calls["load"] == 2
    assert manager.generation == 1


def test_snapshot_keeps_last_good_value_for_whitelisted_transient_drift_error(tmp_path):
    source_path = tmp_path / "system_config.json"
    source_path.write_text("valid", encoding="utf-8")
    clock = FakeClock()

    def loader(_environment):
        value = source_path.read_text(encoding="utf-8")
        if value.startswith("broken"):
            raise BlockingIOError(errno.EAGAIN, "temporary read failure")
        return {"value": value}

    manager = ConfigSnapshotManager(
        source_path=source_path,
        loader=loader,
        environment={},
        clock=clock,
        is_transient_error=lambda exc: (isinstance(exc, OSError) and exc.errno == errno.EAGAIN),
    )
    assert manager.load() == {"value": "valid"}

    source_path.write_text("broken and externally changed", encoding="utf-8")
    clock.advance(1.5)

    assert manager.load() == {"value": "valid"}
    assert manager.generation == 0


def test_snapshot_never_returns_stale_value_for_security_drift_error(tmp_path):
    source_path = tmp_path / "system_config.json"
    source_path.write_text("valid", encoding="utf-8")
    clock = FakeClock()

    def loader(_environment):
        value = source_path.read_text(encoding="utf-8")
        if value != "valid":
            raise SecurityConfigError("unsafe persisted config")
        return {"value": value}

    manager = ConfigSnapshotManager(
        source_path=source_path,
        loader=loader,
        environment={},
        clock=clock,
        is_security_error=lambda exc: isinstance(exc, SecurityConfigError),
    )
    assert manager.load() == {"value": "valid"}

    source_path.write_text("unsafe and externally changed", encoding="utf-8")
    clock.advance(1.5)

    with pytest.raises(SecurityConfigError, match="unsafe persisted config"):
        manager.load()
    assert manager.generation == 0


def test_snapshot_never_swallows_security_signature_check_error(tmp_path):
    source_path = tmp_path / "system_config.json"
    source_path.write_text("valid", encoding="utf-8")
    clock = FakeClock()
    signature_calls = {"count": 0}

    def signature_loader(path):
        signature_calls["count"] += 1
        if signature_calls["count"] > 1:
            raise SecurityConfigError("unsafe source metadata")
        return ConfigSourceSignature.from_path(path)

    manager = ConfigSnapshotManager(
        source_path=source_path,
        loader=lambda _environment: {"value": "valid"},
        environment={},
        clock=clock,
        is_security_error=lambda exc: isinstance(exc, SecurityConfigError),
        signature_loader=signature_loader,
    )
    assert manager.load() == {"value": "valid"}
    clock.advance(1.5)

    with pytest.raises(SecurityConfigError, match="unsafe source metadata"):
        manager.load()


def _prepare_system_config(monkeypatch, tmp_path) -> tuple[Any, Path]:
    import config.system_config as system_config_module

    config_path = tmp_path / "config" / "system_config.json"
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_path.parent)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    monkeypatch.setenv("PERF_CONFIG_SNAPSHOT_ENABLED", "true")
    return system_config_module, config_path


def test_system_config_section_warm_read_copies_only_requested_subtree(
    tmp_path,
    monkeypatch,
):
    system_config_module, config_path = _prepare_system_config(monkeypatch, tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("{}", encoding="utf-8")
    probe = DeepCopyProbe()
    manager = ConfigSnapshotManager(
        source_path=config_path,
        loader=lambda _environment: {
            "storage": {
                "project_root": "/runtime/root",
                "chat_storage_dir": "data/chats",
            },
            "auth": {"auth_secret": probe},
            "llm": {"profiles": [probe] * 100},
        },
        environment={},
    )
    manager.refresh_generation()
    probe.calls = 0
    monkeypatch.setattr(system_config_module, "_CONFIG_SNAPSHOT_MANAGER", manager)
    monkeypatch.setattr(system_config_module, "_SYSTEM_CONFIG_PROCESS_GENERATION", manager.generation)

    first = system_config_module.load_system_config_section("storage")
    first["project_root"] = "/mutated"
    second = system_config_module.load_system_config_section("storage")

    assert probe.calls == 0
    assert second["project_root"] == "/runtime/root"
    assert manager.generation == system_config_module.get_system_config_generation()


def test_system_config_section_rejects_unknown_section(tmp_path, monkeypatch):
    system_config_module, _ = _prepare_system_config(monkeypatch, tmp_path)

    with pytest.raises(
        system_config_module.SystemConfigValidationError,
        match="section",
    ):
        system_config_module.load_system_config_section("unknown")


def test_disabled_system_config_section_uses_full_authoritative_loader(
    tmp_path,
    monkeypatch,
):
    system_config_module, _ = _prepare_system_config(monkeypatch, tmp_path)
    monkeypatch.setenv("PERF_CONFIG_SNAPSHOT_ENABLED", "false")
    calls = []

    def load_authoritatively():
        calls.append(True)
        return {
            "storage": {"project_root": "/runtime/root"},
            "auth": {"auth_secret": synthetic_secret("config_snapshot-credential-04")},
        }

    monkeypatch.setattr(
        system_config_module,
        "_load_system_config_unlocked",
        load_authoritatively,
    )

    storage = system_config_module.load_system_config_section("storage")
    storage["project_root"] = "/mutated"
    second = system_config_module.load_system_config_section("storage")

    assert calls == [True, True]
    assert second["project_root"] == "/runtime/root"


def test_system_config_section_external_drift_advances_generation_once(
    tmp_path,
    monkeypatch,
):
    system_config_module, config_path = _prepare_system_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        system_config_module,
        "CONFIG_SNAPSHOT_CHECK_INTERVAL_SECONDS",
        0.0,
    )
    first = system_config_module.load_system_config_section("storage")
    before_generation = system_config_module.get_system_config_generation()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"storage": {"project_root": "/runtime/next"}}),
        encoding="utf-8",
    )

    second = system_config_module.load_system_config_section("storage")

    assert first["project_root"] != second["project_root"]
    assert second["project_root"] == "/runtime/next"
    assert system_config_module.get_system_config_generation() == before_generation + 1


def test_system_config_snapshot_loads_authoritatively_once_per_generation(
    tmp_path,
    monkeypatch,
):
    system_config_module, _ = _prepare_system_config(monkeypatch, tmp_path)
    original_loader = system_config_module._load_system_config_unlocked
    calls = {"load": 0}

    def counting_loader(*args, **kwargs):
        calls["load"] += 1
        return original_loader(*args, **kwargs)

    monkeypatch.setattr(system_config_module, "_load_system_config_unlocked", counting_loader)

    first = system_config_module.load_system_config()
    second = system_config_module.load_system_config()
    assert first == second
    assert calls["load"] == 1

    before = system_config_module.get_system_config_generation()
    assert system_config_module.invalidate_system_config_snapshot() == before + 1
    assert system_config_module.load_system_config() == first
    assert calls["load"] == 2


def test_system_config_snapshot_freezes_environment_across_invalidation(
    tmp_path,
    monkeypatch,
):
    system_config_module, _ = _prepare_system_config(monkeypatch, tmp_path)
    monkeypatch.setenv("LLM_MODEL", "frozen-model")

    assert system_config_module.load_system_config()["llm"]["model"] == "frozen-model"
    monkeypatch.setenv("LLM_MODEL", "changed-model")
    system_config_module.invalidate_system_config_snapshot()

    assert system_config_module.load_system_config()["llm"]["model"] == "frozen-model"


def test_system_config_generation_accessor_observes_throttled_external_drift(
    tmp_path,
    monkeypatch,
):
    system_config_module, config_path = _prepare_system_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        system_config_module,
        "CONFIG_SNAPSHOT_CHECK_INTERVAL_SECONDS",
        0.0,
    )
    system_config_module.load_system_config()
    before_generation = system_config_module.get_system_config_generation()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"agent": {"context_max_rounds": 22}}),
        encoding="utf-8",
    )

    assert system_config_module.get_system_config_generation() == before_generation + 1
    assert system_config_module.load_system_config()["agent"]["context_max_rounds"] == 22


def _os_error_with_errno(error_number: int) -> OSError:
    error = OSError("synthetic source metadata failure")
    error.errno = error_number
    return error


@pytest.mark.parametrize(
    "signature_error",
    [
        PermissionError("synthetic permission denial"),
        _os_error_with_errno(errno.EACCES),
        _os_error_with_errno(errno.EPERM),
    ],
    ids=["permission-error", "eacces", "eperm"],
)
def test_system_config_security_signature_errors_fail_closed(
    signature_error,
    tmp_path,
    monkeypatch,
):
    system_config_module, _ = _prepare_system_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        system_config_module,
        "CONFIG_SNAPSHOT_CHECK_INTERVAL_SECONDS",
        0.0,
    )
    system_config_module.update_system_config_section(
        "llm",
        {"api_key": synthetic_secret("config_snapshot-credential-04")},
    )
    manager = system_config_module._get_system_config_snapshot_manager()

    def fail_signature_check(_path):
        raise signature_error

    monkeypatch.setattr(manager, "_signature_loader", fail_signature_check)

    with pytest.raises(OSError) as exc_info:
        system_config_module.load_system_config()
    assert exc_info.value is signature_error


def test_system_config_transient_signature_error_keeps_last_good_snapshot(
    tmp_path,
    monkeypatch,
):
    system_config_module, _ = _prepare_system_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        system_config_module,
        "CONFIG_SNAPSHOT_CHECK_INTERVAL_SECONDS",
        0.0,
    )
    system_config_module.update_system_config_section(
        "llm",
        {"api_key": synthetic_secret("config_snapshot-credential-04")},
    )
    generation = system_config_module.get_system_config_generation()
    manager = system_config_module._get_system_config_snapshot_manager()

    def fail_signature_check(_path):
        raise _os_error_with_errno(errno.EAGAIN)

    monkeypatch.setattr(manager, "_signature_loader", fail_signature_check)

    config = system_config_module.load_system_config()
    assert config["llm"]["api_key"] == synthetic_secret("config_snapshot-credential-04")
    assert system_config_module.get_system_config_generation() == generation


def test_system_config_transient_loader_io_error_keeps_last_good_snapshot(
    tmp_path,
    monkeypatch,
):
    system_config_module, config_path = _prepare_system_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        system_config_module,
        "CONFIG_SNAPSHOT_CHECK_INTERVAL_SECONDS",
        0.0,
    )
    system_config_module.update_system_config_section(
        "llm",
        {"api_key": synthetic_secret("config_snapshot-credential-04")},
    )
    manager = system_config_module._get_system_config_snapshot_manager()
    generation = manager.generation
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    def fail_open(*_args, **_kwargs):
        raise BlockingIOError(errno.EAGAIN, "synthetic transient read failure")

    monkeypatch.setattr(system_config_module.os, "open", fail_open)

    config = system_config_module.load_system_config()
    assert config["llm"]["api_key"] == synthetic_secret("config_snapshot-credential-04")
    assert manager.generation == generation
    assert manager._snapshot is not None


def test_system_config_secret_error_with_transient_cause_still_fails_closed(
    tmp_path,
    monkeypatch,
):
    system_config_module, config_path = _prepare_system_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        system_config_module,
        "CONFIG_SNAPSHOT_CHECK_INTERVAL_SECONDS",
        0.0,
    )
    system_config_module.update_system_config_section(
        "llm",
        {"api_key": synthetic_secret("config_snapshot-credential-04")},
    )
    manager = system_config_module._get_system_config_snapshot_manager()
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    def fail_decrypt(*_args, **_kwargs):
        try:
            raise BlockingIOError(errno.EAGAIN, "synthetic nested transient cause")
        except BlockingIOError as cause:
            raise system_config_module.SecretEncryptionError("synthetic secret failure") from cause

    monkeypatch.setattr(system_config_module, "decrypt_secret", fail_decrypt)

    with pytest.raises(system_config_module.SystemConfigReadError) as exc_info:
        system_config_module.load_system_config()
    assert exc_info.value.code == "SYSTEM_CONFIG_SECRET_UNSAFE"
    assert manager._snapshot is None


@pytest.mark.parametrize(
    "program_error",
    [TypeError("synthetic loader type error"), AssertionError("synthetic loader assertion")],
    ids=["type-error", "assertion-error"],
)
def test_system_config_programming_error_propagates_without_discarding_last_good(
    program_error,
    tmp_path,
    monkeypatch,
):
    system_config_module, config_path = _prepare_system_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        system_config_module,
        "CONFIG_SNAPSHOT_CHECK_INTERVAL_SECONDS",
        0.0,
    )
    system_config_module.update_system_config_section(
        "agent",
        {"context_max_rounds": 10},
    )
    manager = system_config_module._get_system_config_snapshot_manager()
    healthy_snapshot = manager._snapshot
    generation = manager.generation
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    original_loader = system_config_module._load_system_config_unlocked

    def fail_loader(*_args, **_kwargs):
        raise program_error

    monkeypatch.setattr(system_config_module, "_load_system_config_unlocked", fail_loader)

    with pytest.raises(type(program_error)) as exc_info:
        system_config_module.load_system_config()
    assert exc_info.value is program_error
    assert manager._snapshot is healthy_snapshot
    assert manager.generation == generation

    monkeypatch.setattr(
        system_config_module,
        "_load_system_config_unlocked",
        original_loader,
    )
    assert system_config_module.load_system_config()["agent"]["context_max_rounds"] == 10
    assert manager.generation == generation + 1


def test_system_config_generation_remains_monotonic_across_path_changes(
    tmp_path,
    monkeypatch,
):
    system_config_module, _ = _prepare_system_config(monkeypatch, tmp_path)
    system_config_module.load_system_config()
    system_config_module.invalidate_system_config_snapshot()
    system_config_module.invalidate_system_config_snapshot()
    previous_generation = system_config_module.get_system_config_generation()

    next_path = tmp_path / "next-config" / "system_config.json"
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", next_path.parent)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", next_path)

    assert system_config_module.get_system_config_generation() == previous_generation + 1
    assert system_config_module.load_system_config() == system_config_module.load_system_config()
    assert system_config_module.get_system_config_generation() == previous_generation + 1


def test_disabled_system_config_snapshot_uses_authoritative_load_path(
    tmp_path,
    monkeypatch,
):
    system_config_module, _ = _prepare_system_config(monkeypatch, tmp_path)
    monkeypatch.setenv("PERF_CONFIG_SNAPSHOT_ENABLED", "false")
    original_loader = system_config_module._load_system_config_unlocked
    calls = {"load": 0}

    def counting_loader(*args, **kwargs):
        calls["load"] += 1
        return original_loader(*args, **kwargs)

    monkeypatch.setattr(system_config_module, "_load_system_config_unlocked", counting_loader)

    first = system_config_module.load_system_config()
    second = system_config_module.load_system_config()

    assert first == second
    assert calls["load"] == 2


def test_successful_update_and_import_each_advance_generation(tmp_path, monkeypatch):
    system_config_module, _ = _prepare_system_config(monkeypatch, tmp_path)
    system_config_module.load_system_config()
    initial_generation = system_config_module.get_system_config_generation()

    system_config_module.update_system_config_section("agent", {"context_max_rounds": 11})
    after_update = system_config_module.get_system_config_generation()
    assert after_update == initial_generation + 1

    imported_sections, _ = system_config_module.import_system_config({"agent": {"context_max_rounds": 12}})
    assert imported_sections == ["agent"]
    assert system_config_module.get_system_config_generation() == after_update + 1


def test_failed_atomic_replace_preserves_file_and_generation(tmp_path, monkeypatch):
    system_config_module, config_path = _prepare_system_config(monkeypatch, tmp_path)
    system_config_module.update_system_config_section("agent", {"context_max_rounds": 11})
    before_bytes = config_path.read_bytes()
    before_generation = system_config_module.get_system_config_generation()

    def fail_replace(_source, _target):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(system_config_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="synthetic replace failure"):
        system_config_module.update_system_config_section("agent", {"context_max_rounds": 12})

    assert config_path.read_bytes() == before_bytes
    assert system_config_module.get_system_config_generation() == before_generation
    assert list(config_path.parent.glob("*.tmp")) == []


def test_failed_replace_is_not_masked_by_temporary_cleanup_failure(
    tmp_path,
    monkeypatch,
):
    system_config_module, _ = _prepare_system_config(monkeypatch, tmp_path)
    system_config_module.update_system_config_section("agent", {"context_max_rounds": 11})
    before_generation = system_config_module.get_system_config_generation()
    replace_error = OSError("synthetic replace failure")

    def fail_replace(_source, _target):
        raise replace_error

    def fail_unlink(_path, *args, **kwargs):
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(system_config_module.os, "replace", fail_replace)
    monkeypatch.setattr(system_config_module.Path, "unlink", fail_unlink)

    with pytest.raises(OSError) as exc_info:
        system_config_module.update_system_config_section("agent", {"context_max_rounds": 12})

    assert exc_info.value is replace_error
    assert system_config_module.get_system_config_generation() == before_generation


def test_system_config_write_fsyncs_parent_directory_after_replace(
    tmp_path,
    monkeypatch,
):
    system_config_module, config_path = _prepare_system_config(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        system_config_module,
        "_fsync_directory",
        lambda path: calls.append(path),
        raising=False,
    )

    system_config_module.update_system_config_section("agent", {"context_max_rounds": 11})

    assert calls == [config_path.parent]


def test_config_directory_fsync_uses_directory_descriptor_on_posix(
    tmp_path,
    monkeypatch,
):
    import config.system_config as system_config_module

    opened = []
    fsynced = []
    closed = []
    monkeypatch.setattr(system_config_module, "_IS_WINDOWS", False, raising=False)
    monkeypatch.setattr(
        system_config_module.os,
        "open",
        lambda path, flags, mode: opened.append((Path(path), flags, mode)) or 73,
    )
    monkeypatch.setattr(system_config_module.os, "fsync", fsynced.append)
    monkeypatch.setattr(system_config_module.os, "close", closed.append)

    system_config_module._fsync_directory(tmp_path)

    assert opened == [(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0), 0o700)]
    assert fsynced == [73]
    assert closed == [73]


def test_config_directory_fsync_does_not_open_directory_on_windows(
    tmp_path,
    monkeypatch,
):
    import config.system_config as system_config_module

    monkeypatch.setattr(system_config_module, "_IS_WINDOWS", True, raising=False)

    def fail_open(*_args, **_kwargs):
        raise AssertionError("Windows directory fsync must remain disabled")

    monkeypatch.setattr(system_config_module.os, "open", fail_open)

    system_config_module._fsync_directory(tmp_path)


def test_update_directory_fsync_failure_reports_committed_and_publishes_generation(
    tmp_path,
    monkeypatch,
):
    system_config_module, config_path = _prepare_system_config(monkeypatch, tmp_path)
    system_config_module.update_system_config_section("agent", {"context_max_rounds": 10})
    before_generation = system_config_module.get_system_config_generation()
    fsync_error = OSError("synthetic directory fsync failure")

    def fail_directory_fsync(_path):
        raise fsync_error

    monkeypatch.setattr(system_config_module, "_fsync_directory", fail_directory_fsync)

    with pytest.raises(OSError) as exc_info:
        system_config_module.update_system_config_section("agent", {"context_max_rounds": 11})

    durability_error = exc_info.value
    assert getattr(durability_error, "committed", False) is True
    assert isinstance(durability_error, system_config_module.SystemConfigDurabilityError)
    assert durability_error.__cause__ is fsync_error
    assert json.loads(config_path.read_text(encoding="utf-8"))["agent"]["context_max_rounds"] == 11
    assert system_config_module.get_system_config_generation() == before_generation + 1
    assert system_config_module.load_system_config()["agent"]["context_max_rounds"] == 11


def test_import_directory_fsync_failure_reports_committed_and_publishes_generation(
    tmp_path,
    monkeypatch,
):
    system_config_module, config_path = _prepare_system_config(monkeypatch, tmp_path)
    system_config_module.update_system_config_section("agent", {"context_max_rounds": 10})
    before_generation = system_config_module.get_system_config_generation()
    fsync_error = OSError("synthetic import directory fsync failure")

    def fail_directory_fsync(_path):
        raise fsync_error

    monkeypatch.setattr(system_config_module, "_fsync_directory", fail_directory_fsync)

    with pytest.raises(OSError) as exc_info:
        system_config_module.import_system_config({"agent": {"context_max_rounds": 12}})

    durability_error = exc_info.value
    assert getattr(durability_error, "committed", False) is True
    assert isinstance(durability_error, system_config_module.SystemConfigDurabilityError)
    assert durability_error.__cause__ is fsync_error
    assert json.loads(config_path.read_text(encoding="utf-8"))["agent"]["context_max_rounds"] == 12
    assert system_config_module.get_system_config_generation() == before_generation + 1
    assert system_config_module.load_system_config()["agent"]["context_max_rounds"] == 12


def test_invalidation_failure_does_not_mask_committed_durability_error(
    tmp_path,
    monkeypatch,
):
    system_config_module, _ = _prepare_system_config(monkeypatch, tmp_path)
    system_config_module.update_system_config_section("agent", {"context_max_rounds": 10})
    fsync_error = OSError("synthetic directory fsync failure")
    invalidate_calls = []

    def fail_directory_fsync(_path):
        raise fsync_error

    def fail_invalidate():
        invalidate_calls.append(True)
        raise RuntimeError("synthetic invalidation failure")

    monkeypatch.setattr(system_config_module, "_fsync_directory", fail_directory_fsync)
    monkeypatch.setattr(
        system_config_module,
        "invalidate_system_config_snapshot",
        fail_invalidate,
    )

    with pytest.raises(OSError) as exc_info:
        system_config_module.update_system_config_section("agent", {"context_max_rounds": 11})

    durability_error = exc_info.value
    assert getattr(durability_error, "committed", False) is True
    assert isinstance(durability_error, system_config_module.SystemConfigDurabilityError)
    assert durability_error.__cause__ is fsync_error
    assert invalidate_calls == [True]


def _synthetic_app_config(settings_module, *, marker: str):
    return settings_module.AppConfig(
        llm=settings_module.LLMConfig(
            base_url="https://llm.example.net/v1",
            model=f"main-{marker}",
            api_key=f"secret-{marker}",
            timeout=30,
            max_tokens=1024,
            temperature=0.2,
        ),
        task_llm=settings_module.LLMConfig(
            base_url="https://llm.example.net/v1",
            model=f"task-{marker}",
            api_key=f"task-secret-{marker}",
            timeout=30,
            max_tokens=1024,
            temperature=0.2,
        ),
        chat_storage_dir=f"/runtime/{marker}/chats",
        execution_storage_dir=f"/runtime/{marker}/executions",
        project_root=f"/runtime/{marker}",
        auth_users_file=f"/runtime/{marker}/users.json",
        auth_secret=f"auth-secret-{marker}",
        auth_token_ttl_seconds=3600,
    )


def _prepare_app_config_cache_test(monkeypatch, *, generation):
    import config.settings as settings_module

    monkeypatch.setenv("PERF_CONFIG_SNAPSHOT_ENABLED", "true")
    monkeypatch.setattr(
        settings_module,
        "get_system_config_generation",
        lambda: generation[0],
        raising=False,
    )
    monkeypatch.setattr(settings_module, "_APP_CONFIG_CACHE", None, raising=False)
    return settings_module


def test_app_config_reuses_one_build_per_system_generation(monkeypatch):
    generation = [7]
    settings_module = _prepare_app_config_cache_test(
        monkeypatch,
        generation=generation,
    )
    calls = []

    def build(cls):
        calls.append(generation[0])
        return _synthetic_app_config(settings_module, marker=str(generation[0]))

    monkeypatch.setattr(
        settings_module.AppConfig,
        "_build_from_env",
        classmethod(build),
        raising=False,
    )

    first = settings_module.AppConfig.from_env()
    second = settings_module.AppConfig.from_env()
    assert calls == [7]
    assert first == second
    assert first is not second

    generation[0] = 8
    third = settings_module.AppConfig.from_env()
    fourth = settings_module.AppConfig.from_env()
    assert calls == [7, 8]
    assert third == fourth
    assert third.llm.model == "main-8"


def test_app_config_returns_deep_defensive_copies_of_cached_secrets(monkeypatch):
    generation = [11]
    settings_module = _prepare_app_config_cache_test(
        monkeypatch,
        generation=generation,
    )
    monkeypatch.setattr(
        settings_module.AppConfig,
        "_build_from_env",
        classmethod(lambda cls: _synthetic_app_config(settings_module, marker="stable")),
        raising=False,
    )

    first = settings_module.AppConfig.from_env()
    first.llm.model = "mutated-model"
    first.llm.api_key = synthetic_secret("config_snapshot-credential-02")
    first.task_llm.api_key = synthetic_secret("config_snapshot-credential-03")
    first.auth_secret = synthetic_secret("config_snapshot-credential-01")

    second = settings_module.AppConfig.from_env()
    assert second.llm.model == "main-stable"
    assert second.llm.api_key == "secret-stable"
    assert second.task_llm.api_key == "task-secret-stable"
    assert second.auth_secret == "auth-secret-stable"


def test_app_config_concurrent_miss_builds_once(monkeypatch):
    generation = [13]
    settings_module = _prepare_app_config_cache_test(
        monkeypatch,
        generation=generation,
    )
    build_started = threading.Event()
    release_build = threading.Event()
    calls = []
    results = []
    errors = []

    def build(cls):
        calls.append(True)
        build_started.set()
        if not release_build.wait(timeout=2):
            raise TimeoutError("test did not release AppConfig build")
        return _synthetic_app_config(settings_module, marker="concurrent")

    monkeypatch.setattr(
        settings_module.AppConfig,
        "_build_from_env",
        classmethod(build),
        raising=False,
    )

    def load_config():
        try:
            results.append(settings_module.AppConfig.from_env())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=load_config) for _ in range(6)]
    for thread in threads:
        thread.start()
    assert build_started.wait(timeout=2)
    release_build.set()
    for thread in threads:
        thread.join(timeout=2)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert calls == [True]
    assert len(results) == 6
    assert len({id(config) for config in results}) == 6


def test_app_config_defensive_copy_runs_outside_cache_lock(monkeypatch):
    generation = [17]
    settings_module = _prepare_app_config_cache_test(
        monkeypatch,
        generation=generation,
    )
    monkeypatch.setattr(
        settings_module.AppConfig,
        "_build_from_env",
        classmethod(lambda cls: _synthetic_app_config(settings_module, marker="copy")),
        raising=False,
    )
    settings_module.AppConfig.from_env()
    first_copy_started = threading.Event()
    second_copy_started = threading.Event()
    release_first_copy = threading.Event()
    copy_calls = []
    errors = []

    def controlled_copy(config):
        call_number = len(copy_calls) + 1
        copy_calls.append(call_number)
        if call_number == 1:
            first_copy_started.set()
            if not release_first_copy.wait(timeout=2):
                raise TimeoutError("second copy was blocked by the cache lock")
        else:
            second_copy_started.set()
        return _synthetic_app_config(settings_module, marker="copy")

    monkeypatch.setattr(
        settings_module,
        "_copy_app_config",
        controlled_copy,
        raising=False,
    )

    def load_config():
        try:
            settings_module.AppConfig.from_env()
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=load_config)
    second = threading.Thread(target=load_config)
    first.start()
    assert first_copy_started.wait(timeout=2)
    second.start()
    try:
        assert second_copy_started.wait(timeout=0.5)
    finally:
        release_first_copy.set()
        first.join(timeout=2)
        second.join(timeout=2)

    assert errors == []
    assert copy_calls == [1, 2]


def test_app_config_snapshot_flag_disabled_bypasses_generation_cache(monkeypatch):
    import config.settings as settings_module

    monkeypatch.setenv("PERF_CONFIG_SNAPSHOT_ENABLED", "false")
    monkeypatch.setattr(settings_module, "_APP_CONFIG_CACHE", None, raising=False)

    def fail_generation_lookup():
        raise AssertionError("disabled snapshot path must not read generation")

    monkeypatch.setattr(
        settings_module,
        "get_system_config_generation",
        fail_generation_lookup,
        raising=False,
    )
    calls = []

    def build(cls):
        calls.append(len(calls) + 1)
        return _synthetic_app_config(settings_module, marker=str(calls[-1]))

    monkeypatch.setattr(
        settings_module.AppConfig,
        "_build_from_env",
        classmethod(build),
        raising=False,
    )

    first = settings_module.AppConfig.from_env()
    second = settings_module.AppConfig.from_env()

    assert calls == [1, 2]
    assert first.llm.model == "main-1"
    assert second.llm.model == "main-2"


def test_app_config_storage_accessor_reads_only_storage_section(monkeypatch):
    import config.settings as settings_module

    storage_mapping = {
        "chat_storage_dir": "data/chats",
        "execution_storage_dir": "data/executions",
        "project_root": "/runtime/root",
        "auth_users_file": "users.json",
    }
    calls = []

    def load_storage_section(section):
        calls.append(section)
        return storage_mapping

    monkeypatch.setattr(
        settings_module,
        "load_system_config_section",
        load_storage_section,
        raising=False,
    )

    def fail_full_system_config_load():
        raise AssertionError("storage accessor must not load full system config")

    monkeypatch.setattr(
        settings_module,
        "load_system_config",
        fail_full_system_config_load,
    )

    storage = settings_module.StorageConfig.from_env()

    assert calls == ["storage"]
    assert storage.project_root == "/runtime/root"
    assert storage.chat_storage_dir == "/runtime/root/data/chats"
    assert storage.execution_storage_dir == "/runtime/root/data/executions"
    assert storage.auth_users_file == "/runtime/root/users.json"


def test_system_config_write_flushes_and_atomically_replaces_from_same_directory(
    tmp_path,
    monkeypatch,
):
    system_config_module, config_path = _prepare_system_config(monkeypatch, tmp_path)
    original_fsync = system_config_module.os.fsync
    original_replace = system_config_module.os.replace
    calls: dict[str, Any] = {"fsync": 0, "replace": []}

    def recording_fsync(descriptor):
        calls["fsync"] += 1
        return original_fsync(descriptor)

    def recording_replace(source, target):
        calls["replace"].append((Path(source), Path(target)))
        return original_replace(source, target)

    monkeypatch.setattr(system_config_module.os, "fsync", recording_fsync)
    monkeypatch.setattr(system_config_module.os, "replace", recording_replace)

    system_config_module.update_system_config_section("agent", {"context_max_rounds": 11})

    # Flush both the temporary file and its parent directory after replacement.
    assert calls["fsync"] == 2
    assert len(calls["replace"]) == 1
    temporary_path, target_path = calls["replace"][0]
    assert temporary_path.parent == config_path.parent
    assert target_path == config_path
    assert not temporary_path.exists()


def test_successful_write_publishes_file_and_generation_as_one_transition(
    tmp_path,
    monkeypatch,
):
    system_config_module, _ = _prepare_system_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        system_config_module,
        "CONFIG_SNAPSHOT_CHECK_INTERVAL_SECONDS",
        0.0,
    )
    system_config_module.update_system_config_section("agent", {"context_max_rounds": 10})
    before_generation = system_config_module.get_system_config_generation()
    original_invalidate = system_config_module.invalidate_system_config_snapshot
    invalidation_reached = threading.Event()
    allow_invalidation = threading.Event()
    reader_finished = threading.Event()
    errors: list[BaseException] = []

    def blocking_invalidate():
        invalidation_reached.set()
        if not allow_invalidation.wait(timeout=2):
            raise TimeoutError("test did not release config invalidation")
        return original_invalidate()

    def update_config():
        try:
            system_config_module.update_system_config_section("agent", {"context_max_rounds": 11})
        except BaseException as exc:
            errors.append(exc)

    def read_config():
        try:
            system_config_module.load_system_config()
        except BaseException as exc:
            errors.append(exc)
        finally:
            reader_finished.set()

    monkeypatch.setattr(
        system_config_module,
        "invalidate_system_config_snapshot",
        blocking_invalidate,
    )
    writer = threading.Thread(target=update_config)
    reader = threading.Thread(target=read_config)
    writer.start()
    assert invalidation_reached.wait(timeout=2)
    reader.start()
    try:
        assert not reader_finished.wait(timeout=0.1)
    finally:
        allow_invalidation.set()
        writer.join(timeout=2)
        reader.join(timeout=2)

    assert not writer.is_alive()
    assert not reader.is_alive()
    assert errors == []
    assert system_config_module.get_system_config_generation() == before_generation + 1
