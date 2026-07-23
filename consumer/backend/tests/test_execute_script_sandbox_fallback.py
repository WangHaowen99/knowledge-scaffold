"""Sandbox selection and explicit local-fallback tests for execute_script."""

from __future__ import annotations

import logging
import subprocess
from types import SimpleNamespace

import pytest

import agent.tools.execute_script as execute_script_module
from agent.tools.business_data.script_env import SCRIPT_BUSINESS_DATA_CONNECTION_ENV
from integration.sandbox.errors import SandboxExecError, SandboxUnavailableError


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
    ontology_dir = tmp_path / "ontology"
    scripts_dir = ontology_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    script = _write_script(scripts_dir)
    _set_sandbox_config(monkeypatch, fallback_local=False)
    monkeypatch.setattr(execute_script_module.sys, "platform", "linux")
    sandbox_calls = []

    class FakeSandboxExecutor:
        def run_script(self, **kwargs):
            sandbox_calls.append(kwargs)
            return _completed("sandbox-output")

    monkeypatch.setattr(execute_script_module, "SandboxScriptExecutor", FakeSandboxExecutor)

    result = execute_script_module.handle_execute_script_path(
        str(script.relative_to(tmp_path)),
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
    local_mode_events = []
    monkeypatch.setattr(
        execute_script_module,
        "log_sandbox_security_fallback",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("explicit local mode is not a sandbox fallback")
        ),
    )
    monkeypatch.setattr(
        execute_script_module,
        "log_sandbox_local_mode",
        lambda **kwargs: local_mode_events.append(kwargs),
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
    assert local_mode_events[0]["reason"] == "sandbox_disabled"


def test_disabled_sandbox_local_execution_uses_minimal_env(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=False, enabled=False)
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", "must-not-leak")
    monkeypatch.setenv("AUTH_SERVER_ADMIN_TOKEN", "must-not-leak")
    captured = {}
    local_mode_events = []

    monkeypatch.setattr(
        execute_script_module,
        "log_sandbox_local_mode",
        lambda **kwargs: local_mode_events.append(kwargs),
    )

    def _fake_run(*args, **kwargs):
        captured["env"] = kwargs["env"]
        return _completed("normal-local-output")

    monkeypatch.setattr(execute_script_module.subprocess, "run", _fake_run)

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
        env_overrides={
            SCRIPT_BUSINESS_DATA_CONNECTION_ENV: '{"provider":"demo"}',
            "ONTO_X": "must-not-pass",
        },
    )

    assert result == "normal-local-output"
    assert local_mode_events[0]["reason"] == "sandbox_disabled"
    assert captured["env"][SCRIPT_BUSINESS_DATA_CONNECTION_ENV] == '{"provider":"demo"}'
    assert "ONTO_X" not in captured["env"]
    assert "SECRET_ENCRYPTION_KEY" not in captured["env"]
    assert "AUTH_SERVER_ADMIN_TOKEN" not in captured["env"]
    assert "PYTHONPATH" in captured["env"]


def test_disabled_sandbox_local_execution_keeps_domain_table_prefix(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=False, enabled=False)
    monkeypatch.setenv("DOMAIN_TABLE_PREFIX", "custom.example_db")
    captured = {}

    monkeypatch.setattr(
        execute_script_module.subprocess,
        "run",
        lambda *args, **kwargs: (
            captured.setdefault("env", kwargs["env"]),
            _completed("normal-local-output"),
        )[1],
    )

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
    )

    assert result == "normal-local-output"
    assert captured["env"]["DOMAIN_TABLE_PREFIX"] == "custom.example_db"


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
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", "must-not-leak")
    captured = {}

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
        lambda *args, **kwargs: (
            captured.setdefault("env", kwargs["env"]),
            _completed("local-after-error"),
        )[1],
    )

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
        run_id="run-error",
    )

    assert result == "local-after-error"
    assert fallback_events[0]["reason"] == "sandbox_unavailable"
    assert "SECRET_ENCRYPTION_KEY" not in captured["env"]


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

    assert result == "Error: Sandbox unavailable."


@pytest.mark.parametrize(
    "sandbox_error",
    [
        SandboxExecError("ACCESS_TOKEN=sandbox-exec-secret"),
        RuntimeError("ACCESS_TOKEN=unexpected-sandbox-secret"),
    ],
)
def test_sandbox_execution_errors_are_redacted_when_fallback_disabled(
    tmp_path,
    monkeypatch,
    sandbox_error,
):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=False)
    monkeypatch.setattr(execute_script_module.sys, "platform", "linux")

    class FailingSandboxExecutor:
        def run_script(self, **kwargs):
            raise sandbox_error

    monkeypatch.setattr(execute_script_module, "SandboxScriptExecutor", FailingSandboxExecutor)

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
        run_id="run-error",
    )

    assert result == "Error: Sandbox execution failed."
    assert "secret" not in result


def test_request_log_redacts_cli_argument_values(tmp_path, monkeypatch, caplog):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=False, enabled=False)
    monkeypatch.setattr(
        execute_script_module.subprocess,
        "run",
        lambda *args, **kwargs: _completed("ok"),
    )

    with caplog.at_level(logging.INFO):
        result = execute_script_module.handle_execute_script_path(
            script.name,
            base_path=tmp_path,
            args=["--access-token", "cli-secret-value"],
        )

    assert result == "ok"
    assert "cli-secret-value" not in caplog.text
    assert "arg_count=2" in caplog.text


def test_live_log_redacts_script_output_values(tmp_path, monkeypatch, caplog):
    script = tmp_path / "live_output.py"
    script.write_text("print('ACCESS_TOKEN=live-secret-value')\n", encoding="utf-8")
    _set_sandbox_config(monkeypatch, fallback_local=False, enabled=False)
    monkeypatch.setattr(execute_script_module, "SCRIPT_LIVE_LOG_ENABLED", True)

    with caplog.at_level(logging.INFO):
        result = execute_script_module.handle_execute_script_path(
            script.name,
            base_path=tmp_path,
        )

    assert "live-secret-value" in result
    assert "live-secret-value" not in caplog.text
    assert "line_chars=" in caplog.text


def test_unexpected_execution_error_is_redacted_from_result_and_logs(
    tmp_path,
    monkeypatch,
    caplog,
):
    script = _write_script(tmp_path)
    monkeypatch.setattr(
        execute_script_module,
        "load_sandbox_config",
        lambda: (_ for _ in ()).throw(
            RuntimeError("ACCESS_TOKEN=unexpected-secret-value")
        ),
    )

    with caplog.at_level(logging.ERROR):
        result = execute_script_module.handle_execute_script_path(
            script.name,
            base_path=tmp_path,
        )

    assert result == "Error: Script execution failed due to an internal error."
    assert "unexpected-secret-value" not in result
    assert "unexpected-secret-value" not in caplog.text
    assert "error_type=RuntimeError" in caplog.text


def test_nonzero_exit_redacts_process_output_from_result_and_logs(
    tmp_path,
    monkeypatch,
    caplog,
):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=False, enabled=False)
    monkeypatch.setattr(
        execute_script_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=["python"],
            returncode=7,
            stdout="ACCESS_TOKEN=stdout-secret",
            stderr="DATABASE_URL=stderr-secret",
        ),
    )

    with caplog.at_level(logging.WARNING):
        result = execute_script_module.handle_execute_script_path(
            script.name,
            base_path=tmp_path,
        )

    assert result == "Error: Script exited with code 7."
    assert "stdout-secret" not in caplog.text
    assert "stderr-secret" not in caplog.text


def test_nonzero_exit_returns_explicit_public_business_error(
    tmp_path,
    monkeypatch,
    caplog,
):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=False, enabled=False)
    monkeypatch.setattr(
        execute_script_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=["python"],
            returncode=7,
            stdout="ACCESS_TOKEN=stdout-secret",
            stderr=(
                'ONTO_PUBLIC_ERROR: {"code":"MATERIAL_NOT_FOUND",'
                '"message":"Material A001 was not found."}\n'
                "DATABASE_URL=stderr-secret"
            ),
        ),
    )

    with caplog.at_level(logging.WARNING):
        result = execute_script_module.handle_execute_script_path(
            script.name,
            base_path=tmp_path,
        )

    assert result == (
        "Error: Script exited with code 7. "
        "[MATERIAL_NOT_FOUND] Material A001 was not found."
    )
    assert "stdout-secret" not in result
    assert "stderr-secret" not in result
    assert "stdout-secret" not in caplog.text
    assert "stderr-secret" not in caplog.text


def test_nonzero_exit_rejects_malformed_public_error_marker(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=False, enabled=False)
    monkeypatch.setattr(
        execute_script_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=["python"],
            returncode=9,
            stdout="",
            stderr=(
                'ONTO_PUBLIC_ERROR: {"code":"bad-code",'
                '"message":"DATABASE_URL=must-stay-redacted"}'
            ),
        ),
    )

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
    )

    assert result == "Error: Script exited with code 9."
    assert "must-stay-redacted" not in result


def test_timeout_redacts_process_output_from_logs(tmp_path, monkeypatch, caplog):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=False, enabled=False)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["python"],
            timeout=1,
            output="ACCESS_TOKEN=timeout-stdout-secret",
            stderr="DATABASE_URL=timeout-stderr-secret",
        )

    monkeypatch.setattr(execute_script_module.subprocess, "run", timeout)

    with caplog.at_level(logging.WARNING):
        result = execute_script_module.handle_execute_script_path(
            script.name,
            base_path=tmp_path,
            timeout_seconds=1,
        )

    assert result == "Error: Script execution timed out after 1 seconds."
    assert "timeout-stdout-secret" not in caplog.text
    assert "timeout-stderr-secret" not in caplog.text


def test_execution_boundary_rejects_path_outside_current_ontology_scripts(
    tmp_path,
    monkeypatch,
):
    project_root = tmp_path / "runtime"
    ontology_dir = project_root / "workspaces" / "workspace-a" / "ontology"
    (ontology_dir / "scripts").mkdir(parents=True)
    outside_script = project_root / "workspaces" / "workspace-b" / "outside.py"
    outside_script.parent.mkdir(parents=True)
    outside_script.write_text("print('outside')\n", encoding="utf-8")
    _set_sandbox_config(monkeypatch, fallback_local=False, enabled=False)
    executed = []
    monkeypatch.setattr(
        execute_script_module.subprocess,
        "run",
        lambda *args, **kwargs: executed.append((args, kwargs)) or _completed("outside"),
    )

    result = execute_script_module.handle_execute_script_path(
        str(outside_script.relative_to(project_root)),
        base_path=project_root,
        ontology_dir=ontology_dir,
    )

    assert result == "Error: Access denied - script must be within current ontology scripts directory"
    assert executed == []
