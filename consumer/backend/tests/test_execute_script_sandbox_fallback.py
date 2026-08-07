"""Sandbox selection and explicit local-fallback tests for execute_script."""

from __future__ import annotations

import logging
import subprocess
from types import SimpleNamespace

import pytest

import agent.tools.execute_script as execute_script_module
from integration.sandbox.alerts import log_sandbox_security_fallback
from integration.sandbox.errors import (
    SandboxCreateError,
    SandboxExecError,
    SandboxUnavailableError,
    SandboxUnsupportedProviderError,
)


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


def _completed(
    output: str,
    *,
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["python"],
        returncode=returncode,
        stdout=output,
        stderr=stderr,
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


def test_legacy_mode_routes_directly_to_original_local_path(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    monkeypatch.setattr(
        execute_script_module,
        "load_sandbox_config",
        lambda: SimpleNamespace(
            enabled=False,
            fallback_local=True,
            execution_mode="legacy",
        ),
    )
    monkeypatch.setattr(
        execute_script_module,
        "SandboxScriptExecutor",
        lambda: (_ for _ in ()).throw(
            AssertionError("legacy mode must not initialize the sandbox executor")
        ),
    )
    monkeypatch.setattr(
        execute_script_module.subprocess,
        "run",
        lambda *args, **kwargs: _completed("legacy-output"),
    )

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
    )

    assert result == "legacy-output"


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
    assert fallback_events[0]["error"] == "SandboxUnavailableError"
    assert fallback_events[0]["mode"] == "prefer"


def test_unsupported_provider_uses_local_execution_with_audit(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=True)
    monkeypatch.setattr(execute_script_module.sys, "platform", "linux")
    fallback_events = []

    class UnsupportedProviderSandboxExecutor:
        def run_script(self, **kwargs):
            raise SandboxUnsupportedProviderError("neo4j")

    monkeypatch.setattr(
        execute_script_module,
        "SandboxScriptExecutor",
        UnsupportedProviderSandboxExecutor,
    )
    monkeypatch.setattr(
        execute_script_module,
        "log_sandbox_security_fallback",
        lambda **kwargs: fallback_events.append(kwargs),
    )
    monkeypatch.setattr(
        execute_script_module.subprocess,
        "run",
        lambda *args, **kwargs: _completed("local-neo4j-output"),
    )

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
        run_id="run-neo4j",
    )

    assert result == "local-neo4j-output"
    assert fallback_events == [
        {
            "reason": "unsupported_provider",
            "call_context": "run_id=run-neo4j",
            "script_path": script.name,
            "run_id": "run-neo4j",
            "error": "SandboxUnsupportedProviderError",
            "mode": "prefer",
            "provider": "neo4j",
            "workspace_id": None,
            "ontology_id": None,
        }
    ]


@pytest.mark.parametrize(
    ("returncode", "stderr", "expected_reason"),
    [
        (
            1,
            "python3: can't open file '/workspace/scripts/tool.py': "
            "[Errno 2] No such file or directory",
            "sandbox_tool_missing",
        ),
        (
            1,
            "ModuleNotFoundError: No module named 'legacy_tool_runtime'",
            "sandbox_tool_missing",
        ),
        (127, "legacy-tool: command not found", "sandbox_tool_missing"),
        (
            1,
            "exec denied by policy: permission denied",
            "sandbox_policy_unavailable",
        ),
        (126, "python3: permission denied", "sandbox_policy_unavailable"),
    ],
)
def test_sandbox_tool_or_policy_failure_reuses_local_execution(
    tmp_path,
    monkeypatch,
    returncode,
    stderr,
    expected_reason,
):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=True)
    monkeypatch.setattr(execute_script_module.sys, "platform", "linux")
    fallback_events = []

    class MissingToolSandboxExecutor:
        def run_script(self, **kwargs):
            return _completed("", returncode=returncode, stderr=stderr)

    monkeypatch.setattr(
        execute_script_module,
        "SandboxScriptExecutor",
        MissingToolSandboxExecutor,
    )
    monkeypatch.setattr(
        execute_script_module,
        "log_sandbox_security_fallback",
        lambda **kwargs: fallback_events.append(kwargs),
    )
    monkeypatch.setattr(
        execute_script_module.subprocess,
        "run",
        lambda *args, **kwargs: _completed("local-default-output"),
    )

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
        run_id="run-sandbox-recovery",
    )

    assert result == "local-default-output"
    assert fallback_events[0]["reason"] == expected_reason


@pytest.mark.parametrize(
    ("sandbox_error", "expected_reason"),
    [
        (SandboxExecError("sandbox command not found"), "sandbox_tool_missing"),
        (SandboxCreateError("sandbox policy rejected command"), "sandbox_policy_unavailable"),
    ],
)
def test_sandbox_tool_or_policy_exception_reuses_local_execution(
    tmp_path,
    monkeypatch,
    sandbox_error,
    expected_reason,
):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=True)
    monkeypatch.setattr(execute_script_module.sys, "platform", "linux")
    fallback_events = []

    class FailingSandboxExecutor:
        def run_script(self, **kwargs):
            raise sandbox_error

    monkeypatch.setattr(execute_script_module, "SandboxScriptExecutor", FailingSandboxExecutor)
    monkeypatch.setattr(
        execute_script_module,
        "log_sandbox_security_fallback",
        lambda **kwargs: fallback_events.append(kwargs),
    )
    monkeypatch.setattr(
        execute_script_module.subprocess,
        "run",
        lambda *args, **kwargs: _completed("local-exception-recovery"),
    )

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
        run_id="run-sandbox-exception-recovery",
    )

    assert result == "local-exception-recovery"
    assert fallback_events[0]["reason"] == expected_reason


def test_sandbox_recovery_reuses_original_local_command_and_context(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=True)
    monkeypatch.setattr(execute_script_module.sys, "platform", "linux")
    local_calls = []

    class MissingToolSandboxExecutor:
        def run_script(self, **kwargs):
            return _completed(
                "",
                returncode=1,
                stderr="python: can't open file '/workspace/tool.py': No such file or directory",
            )

    monkeypatch.setattr(execute_script_module, "SandboxScriptExecutor", MissingToolSandboxExecutor)

    def local_run(*args, **kwargs):
        local_calls.append((args, kwargs))
        return _completed("local-original-path")

    monkeypatch.setattr(execute_script_module.subprocess, "run", local_run)

    result = execute_script_module.handle_execute_script_path(
        script.name,
        args=["--mode", "compat"],
        base_path=tmp_path,
        run_id="run-original-context",
        env_overrides={"TOOL_COMPAT_FLAG": "enabled"},
    )

    assert result == "local-original-path"
    assert len(local_calls) == 1
    command, options = local_calls[0]
    assert command == (
        [
            execute_script_module.sys.executable,
            str(script.resolve()),
            "--mode",
            "compat",
        ],
    )
    assert options["cwd"] == str(tmp_path.resolve())
    assert options["env"]["TOOL_COMPAT_FLAG"] == "enabled"


def test_sandbox_timeout_is_not_replayed_locally(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=True)
    monkeypatch.setattr(execute_script_module.sys, "platform", "linux")

    class TimedOutSandboxExecutor:
        def run_script(self, **kwargs):
            raise SandboxExecError("exec timed out in sandbox")

    monkeypatch.setattr(execute_script_module, "SandboxScriptExecutor", TimedOutSandboxExecutor)
    monkeypatch.setattr(
        execute_script_module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a sandbox timeout must not replay locally")
        ),
    )

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
        run_id="run-sandbox-timeout",
        timeout_seconds=7,
    )

    assert result == "Error: Script execution timed out after 7 seconds."


@pytest.mark.parametrize(
    ("returncode", "stderr"),
    [
        (2, "business validation failed"),
        (2, "lookup failed: file not found"),
        (1, "PermissionError: [Errno 13] Permission denied: '/restricted'"),
        (1, "OSError: [Errno 30] Read-only file system: '/workspace/output'"),
    ],
)
def test_normal_sandbox_script_failure_is_not_replayed_locally(
    tmp_path,
    monkeypatch,
    returncode,
    stderr,
):
    script = _write_script(tmp_path)
    _set_sandbox_config(monkeypatch, fallback_local=True)
    monkeypatch.setattr(execute_script_module.sys, "platform", "linux")

    class FailedSandboxExecutor:
        def run_script(self, **kwargs):
            return _completed(
                "",
                returncode=returncode,
                stderr=stderr,
            )

    monkeypatch.setattr(
        execute_script_module,
        "SandboxScriptExecutor",
        FailedSandboxExecutor,
    )
    monkeypatch.setattr(
        execute_script_module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("normal sandbox script failures must not replay locally")
        ),
    )

    result = execute_script_module.handle_execute_script_path(
        script.name,
        base_path=tmp_path,
        run_id="run-sandbox-failure",
    )

    assert result == "Error: Script exited with code {}.".format(returncode)


def test_sandbox_fallback_log_contains_searchable_audit_fields(caplog):
    with caplog.at_level(logging.ERROR, logger="sandbox.alerts"):
        log_sandbox_security_fallback(
            reason="unsupported_provider",
            call_context="run_id=run-1",
            script_path="scripts/demo.py",
            run_id="run-1",
            error="SandboxUnsupportedProviderError",
            mode="prefer",
            provider="neo4j",
            workspace_id="workspace-a",
            ontology_id="ontology-a",
        )

    record = caplog.records[-1]
    assert record.security_event == "sandbox_bypass"
    assert record.execution_mode == "prefer"
    assert record.execution_decision == "local_fallback"
    assert record.fallback_reason == "unsupported_provider"
    assert record.provider == "neo4j"
    assert record.workspace_id == "workspace-a"
    assert record.ontology_id == "ontology-a"
    assert record.error_type == "SandboxUnsupportedProviderError"


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
