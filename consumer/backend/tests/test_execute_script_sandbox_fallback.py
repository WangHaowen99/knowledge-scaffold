"""Sandbox selection and explicit local-fallback tests for execute_script."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import agent.tools.execute_script as execute_script_module
from integration.sandbox.errors import SandboxUnavailableError


def _write_script(tmp_path):
    script = tmp_path / "operation.py"
    script.write_text("print('real script should not run in this test')\n", encoding="utf-8")
    return script


def _set_sandbox_config(
    monkeypatch,
    *,
    fallback_local: bool,
    enabled: bool = True,
) -> None:
    monkeypatch.setattr(
        execute_script_module,
        "load_sandbox_config",
        lambda: SimpleNamespace(enabled=enabled, fallback_local=fallback_local),
    )


def _completed(output: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["python"],
        returncode=0,
        stdout=output,
        stderr="",
    )


def test_live_log_does_not_bypass_sandbox(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    extra_package_dir = tmp_path / "extra-package"
    extra_package_dir.mkdir()
    _set_sandbox_config(monkeypatch, fallback_local=False)
    monkeypatch.setattr(execute_script_module.sys, "platform", "linux")
    monkeypatch.setattr(execute_script_module, "SCRIPT_LIVE_LOG_ENABLED", True)
    sandbox_calls = []

    class FakeSandboxExecutor:
        def run_script(self, **kwargs):
            sandbox_calls.append(kwargs)
            return _completed("sandbox-output")

    monkeypatch.setattr(execute_script_module, "SandboxScriptExecutor", FakeSandboxExecutor)
    monkeypatch.setattr(
        execute_script_module,
        "_run_with_live_logging",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("live logging must not force local execution")
        ),
    )

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
        run_id="run-live",
        extra_pythonpath_dirs=[extra_package_dir],
        env_overrides={"ONTO_X": "must-not-be-forwarded-to-sandbox-executor"},
    )

    assert result == "sandbox-output"
    assert len(sandbox_calls) == 1
    assert "env_overrides" not in sandbox_calls[0]
    assert sandbox_calls[0]["ontology_dir"] is None


def test_explicit_ontology_dir_is_forwarded_to_sandbox(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    ontology_dir = tmp_path / "ontology"
    ontology_dir.mkdir()
    _set_sandbox_config(monkeypatch, fallback_local=False)
    monkeypatch.setattr(execute_script_module.sys, "platform", "linux")
    sandbox_calls = []

    class FakeSandboxExecutor:
        def run_script(self, **kwargs):
            sandbox_calls.append(kwargs)
            return _completed("sandbox-output")

    monkeypatch.setattr(execute_script_module, "SandboxScriptExecutor", FakeSandboxExecutor)

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
        run_id="run-explicit-ontology",
        ontology_dir=ontology_dir,
    )

    assert result == "sandbox-output"
    assert sandbox_calls[0]["ontology_dir"] == ontology_dir


def test_sandbox_none_result_returns_public_internal_error(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=False)
    monkeypatch.setattr(execute_script_module.sys, "platform", "linux")

    class EmptySandboxExecutor:
        def run_script(self, **kwargs):
            return None

    monkeypatch.setattr(execute_script_module, "SandboxScriptExecutor", EmptySandboxExecutor)

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
        run_id="run-empty-sandbox-result",
    )

    assert result == "Error: Script execution failed due to an internal error."


def test_disabled_sandbox_uses_normal_local_execution(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=False, enabled=False)
    monkeypatch.setattr(
        execute_script_module,
        "log_sandbox_security_fallback",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("explicit local mode is not a sandbox fallback")
        ),
    )
    monkeypatch.setattr(
        execute_script_module.subprocess,
        "run",
        lambda *args, **kwargs: _completed("normal-local-output"),
    )

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
        run_id=None,
    )

    assert result == "normal-local-output"


def test_local_none_result_returns_public_internal_error(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=False, enabled=False)
    monkeypatch.setattr(execute_script_module.subprocess, "run", lambda *args, **kwargs: None)

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
    )

    assert result == "Error: Script execution failed due to an internal error."


def test_oversize_args_fail_closed_when_fallback_disabled(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=False)
    monkeypatch.setattr(execute_script_module.sys, "platform", "linux")
    monkeypatch.setattr(execute_script_module, "_MAX_CLI_ARGS_SIZE", 1)
    monkeypatch.setattr(
        execute_script_module,
        "_spawn_with_args_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("local args-file execution must remain blocked")
        ),
    )

    result = execute_script_module.handle_execute_script_path(
        script.name,
        args=["oversize"],
        base_path=tmp_path,
        run_id="run-oversize",
    )

    assert result == (
        "Error: Sandbox unavailable: oversized script arguments are not "
        "supported by sandbox execution"
    )


def test_oversize_args_use_explicit_fallback_when_enabled(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=True)
    monkeypatch.setattr(execute_script_module.sys, "platform", "linux")
    monkeypatch.setattr(execute_script_module, "_MAX_CLI_ARGS_SIZE", 1)
    fallback_events = []
    monkeypatch.setattr(
        execute_script_module,
        "log_sandbox_security_fallback",
        lambda **kwargs: fallback_events.append(kwargs),
    )
    monkeypatch.setattr(
        execute_script_module,
        "_spawn_with_args_file",
        lambda *args, **kwargs: _completed("local-output"),
    )

    result = execute_script_module.handle_execute_script_path(
        script.name,
        args=["oversize"],
        base_path=tmp_path,
        run_id="run-oversize",
    )

    assert result == "local-output"
    assert fallback_events[0]["reason"] == "oversize_args"


def test_missing_run_id_uses_fallback_gate(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=False)
    monkeypatch.setattr(execute_script_module.sys, "platform", "linux")

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
        run_id=None,
    )

    assert result == (
        "Error: Sandbox unavailable: run_id is required for sandbox execution"
    )


def test_unsupported_platform_uses_fallback_gate(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=False)
    monkeypatch.setattr(execute_script_module.sys, "platform", "win32")

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
        run_id="run-windows",
    )

    assert result == (
        "Error: Sandbox unavailable: sandbox execution requires Linux "
        "(current platform: win32)"
    )


def test_sandbox_exception_uses_same_fallback_gate(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=True)
    monkeypatch.setattr(execute_script_module.sys, "platform", "linux")
    fallback_events = []

    class FailingSandboxExecutor:
        def run_script(self, **kwargs):
            raise SandboxUnavailableError("jiuwenbox is down")

    monkeypatch.setattr(execute_script_module, "SandboxScriptExecutor", FailingSandboxExecutor)
    monkeypatch.setattr(
        execute_script_module,
        "log_sandbox_security_fallback",
        lambda **kwargs: fallback_events.append(kwargs),
    )
    monkeypatch.setattr(
        execute_script_module.subprocess,
        "run",
        lambda *args, **kwargs: _completed("local-after-error"),
    )

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
        run_id="run-error",
    )

    assert result == "local-after-error"
    assert fallback_events[0]["reason"] == "sandbox_unavailable"


def test_sandbox_exception_fails_closed_when_fallback_disabled(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=False)
    monkeypatch.setattr(execute_script_module.sys, "platform", "linux")

    class FailingSandboxExecutor:
        def run_script(self, **kwargs):
            raise SandboxUnavailableError("jiuwenbox is down")

    monkeypatch.setattr(execute_script_module, "SandboxScriptExecutor", FailingSandboxExecutor)
    monkeypatch.setattr(
        execute_script_module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("sandbox failure must not execute locally")
        ),
    )

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
        run_id="run-error",
    )

    assert result == "Error: Sandbox unavailable: jiuwenbox is down"

