"""CLI helpers for launching isolated local agent simulation runs."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid
from typing import Any


ARTIFACT_ROOT_NAME = "simulations"
DEFAULT_USER_ID = "sim"
DEFAULT_WORKSPACE_ID = "default"
PRIVATE_CONFIG_FILENAME = "system_config.private.json"
PUBLIC_CONFIG_FILENAME = "system_config.public.json"
SPEC_FILENAME = "spec.json"
LAUNCH_FILENAME = "launch.json"
RESULT_FILENAME = "result.json"
EVENTS_FILENAME = "events.jsonl"
WORKER_LOG_FILENAME = "worker.log"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            payload = start_run(args)
        elif args.command == "run":
            payload = run_foreground(args)
        elif args.command == "status":
            payload = read_status(args.run_dir)
        elif args.command == "result":
            payload = read_result(args.run_dir)
        elif args.command == "logs":
            return print_logs(args.run_dir, tail=args.tail)
        elif args.command == "worker":
            return run_worker(args.run_dir)
        else:
            parser.error("unknown command")
            return 2
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent_sim",
        description="Launch and inspect isolated local agent simulation runs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start", help="start a background simulation run")
    add_run_arguments(start_parser)

    run_parser = subparsers.add_parser("run", help="run a simulation in the foreground")
    add_run_arguments(run_parser)

    for name in ("status", "result"):
        command_parser = subparsers.add_parser(name, help=f"read simulation {name}")
        command_parser.add_argument("run_dir", help="simulation artifact directory")

    logs_parser = subparsers.add_parser("logs", help="print worker and agent logs")
    logs_parser.add_argument("run_dir", help="simulation artifact directory")
    logs_parser.add_argument("--tail", type=int, default=0, help="only print the last N lines")

    worker_parser = subparsers.add_parser("worker", help="internal worker entrypoint")
    worker_parser.add_argument("run_dir", help="simulation artifact directory")

    return parser


def add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("question", help="user question/request to send to the agent")
    parser.add_argument("--session-id", help="chat/execution session id; defaults to sim_<run_id>")
    parser.add_argument("--run-id", help="agent run id; defaults to sim_<short uuid>")
    parser.add_argument("--user-id", default=DEFAULT_USER_ID, help="user id for runtime stores")
    parser.add_argument("--workspace-id", default=DEFAULT_WORKSPACE_ID, help="workspace id")
    parser.add_argument("--output-dir", help="directory for simulation artifacts")
    parser.add_argument("--model-profile-id", help="existing saved model profile id")
    parser.add_argument("--agent-mode", choices=("quick", "thinking", "multi_agent"), help="agent mode")
    parser.add_argument("--llm-request-mode", choices=("stream", "sync"), help="upstream LLM request mode")
    parser.add_argument("--selected-ontology-id", help="preselected ontology id")
    parser.add_argument("--context-json", help="JSON object merged into user_context")
    parser.add_argument(
        "--context",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="context key/value pair; may be repeated",
    )
    parser.add_argument("--base-url", help="transient model base URL for this run")
    parser.add_argument("--model", help="transient model name for this run")
    parser.add_argument("--api-key", help="transient model API key for this run")
    parser.add_argument("--api-key-env", help="environment variable containing the transient model API key")
    parser.add_argument("--provider", default="openai_compatible", help="transient model provider")
    parser.add_argument("--temperature", type=float, help="transient model temperature")
    parser.add_argument("--max-tokens", type=int, help="transient model max tokens")
    parser.add_argument("--timeout", type=int, help="transient model timeout seconds")


def start_run(args: argparse.Namespace) -> dict[str, Any]:
    run_dir, spec = prepare_run(args, mode="background")
    worker_log = run_dir / WORKER_LOG_FILENAME
    command = [sys.executable, str(_repo_root() / "tools" / "agent_sim.py"), "worker", str(run_dir)]
    env = dict(os.environ)
    env["PYTHONPATH"] = _pythonpath_with_backend()

    with worker_log.open("ab") as stdout:
        process = subprocess.Popen(
            command,
            cwd=str(_repo_root()),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    launch = {
        "status": "launched",
        "pid": process.pid,
        "launched_at_ms": _now_ms(),
        "command": command,
    }
    _write_json(run_dir / LAUNCH_FILENAME, launch)
    return _start_response(run_dir, spec, launch)


def run_foreground(args: argparse.Namespace) -> dict[str, Any]:
    run_dir, _spec = prepare_run(args, mode="foreground")
    exit_code = run_worker(str(run_dir))
    result = _read_json(run_dir / RESULT_FILENAME) or {}
    result.setdefault("status", "error" if exit_code else "unknown")
    result["artifact_dir"] = str(run_dir)
    return result


def prepare_run(args: argparse.Namespace, *, mode: str) -> tuple[Path, dict[str, Any]]:
    run_id = str(args.run_id or "").strip() or f"sim_{uuid.uuid4().hex[:12]}"
    session_id = str(args.session_id or "").strip() or f"sim_{run_id}"
    run_dir = _resolve_run_dir(args.output_dir, run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)

    private_config, public_config = build_config_snapshots(args)
    private_config_path = run_dir / PRIVATE_CONFIG_FILENAME
    _write_json(private_config_path, private_config)
    try:
        private_config_path.chmod(0o600)
    except OSError:
        pass
    _write_json(run_dir / PUBLIC_CONFIG_FILENAME, public_config)

    context = build_context(args)
    spec = {
        "schema_version": 1,
        "mode": mode,
        "created_at_ms": _now_ms(),
        "repo_root": str(_repo_root()),
        "backend_path": str(_backend_root()),
        "run_id": run_id,
        "session_id": session_id,
        "user_id": str(args.user_id or DEFAULT_USER_ID),
        "workspace_id": str(args.workspace_id or DEFAULT_WORKSPACE_ID),
        "question": args.question,
        "context": context,
        "private_config_path": str(private_config_path),
        "paths": {
            "events": str(run_dir / EVENTS_FILENAME),
            "result": str(run_dir / RESULT_FILENAME),
            "worker_log": str(run_dir / WORKER_LOG_FILENAME),
            "agent_logs": str(run_dir / "logs"),
        },
    }
    _write_json(run_dir / SPEC_FILENAME, _public_spec(spec))
    _write_json(run_dir / "spec.private.json", spec)
    return run_dir, spec


def build_context(args: argparse.Namespace) -> dict[str, Any]:
    context: dict[str, Any] = {}
    if args.context_json:
        raw = json.loads(args.context_json)
        if not isinstance(raw, dict):
            raise ValueError("--context-json must be a JSON object")
        context.update(raw)
    for item in args.context or []:
        if "=" not in item:
            raise ValueError("--context must use KEY=VALUE")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("--context key cannot be empty")
        context[key] = _parse_context_value(value)

    for source, target in (
        (args.model_profile_id, "model_profile_id"),
        (args.agent_mode, "agent_mode"),
        (args.llm_request_mode, "llm_request_mode"),
        (args.selected_ontology_id, "selected_ontology_id"),
    ):
        if source:
            context[target] = source

    return context


def build_config_snapshots(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    from agentic.config.system_config import get_public_system_config, load_system_config

    private_config = json.loads(json.dumps(load_system_config(), ensure_ascii=False))
    public_config = json.loads(json.dumps(get_public_system_config(), ensure_ascii=False))
    _drop_empty_profile_list(private_config)
    transient_profile = build_transient_profile(args)
    if transient_profile:
        profile_id = transient_profile["id"]
        private_config.setdefault("llm_profiles", {})["profiles"] = [
            profile
            for profile in private_config.get("llm_profiles", {}).get("profiles", [])
            if profile.get("id") != profile_id
        ] + [transient_profile]
        private_config.setdefault("llm_profiles", {})["default_profile_id"] = profile_id

        public_profile = dict(transient_profile)
        public_profile.pop("api_key", None)
        public_profile["has_api_key"] = bool(transient_profile.get("api_key"))
        public_profile["is_default"] = True
        public_config.setdefault("llm_profiles", {})["profiles"] = [
            profile
            for profile in public_config.get("llm_profiles", {}).get("profiles", [])
            if profile.get("id") != profile_id
        ] + [public_profile]
        public_config.setdefault("llm_profiles", {})["default_profile_id"] = profile_id
    return private_config, public_config


def _drop_empty_profile_list(config: dict[str, Any]) -> None:
    profiles_section = config.get("llm_profiles")
    if not isinstance(profiles_section, dict):
        return
    profiles = profiles_section.get("profiles")
    if profiles == []:
        profiles_section.pop("profiles", None)
    if not profiles_section.get("default_profile_id"):
        profiles_section.pop("default_profile_id", None)


def build_transient_profile(args: argparse.Namespace) -> dict[str, Any] | None:
    has_direct_model_config = any(
        value is not None
        for value in (
            args.base_url,
            args.model,
            args.api_key,
            args.api_key_env,
            args.temperature,
            args.max_tokens,
            args.timeout,
        )
    )
    if not has_direct_model_config:
        return None
    if args.model_profile_id:
        raise ValueError("--model-profile-id cannot be combined with direct transient model options")
    api_key = args.api_key
    if args.api_key_env:
        api_key = os.getenv(args.api_key_env)
        if not api_key:
            raise ValueError(f"environment variable is empty: {args.api_key_env}")
    if not args.base_url or not args.model:
        raise ValueError("--base-url and --model are required for a transient model profile")
    return {
        "id": "sim_transient",
        "name": "Simulation transient model",
        "provider": args.provider or "openai_compatible",
        "base_url": args.base_url,
        "model": args.model,
        "api_key": api_key or "",
        "timeout": args.timeout if args.timeout is not None else 600,
        "max_tokens": args.max_tokens if args.max_tokens is not None else 4096,
        "temperature": args.temperature if args.temperature is not None else 0.7,
        "enabled": True,
    }


def run_worker(run_dir: str) -> int:
    artifact_dir = Path(run_dir).resolve()
    spec = _read_json(artifact_dir / "spec.private.json")
    if not spec:
        raise ValueError(f"missing private spec: {artifact_dir}")

    result_path = artifact_dir / RESULT_FILENAME
    events_path = artifact_dir / EVENTS_FILENAME
    worker_log_path = artifact_dir / WORKER_LOG_FILENAME
    _append_text(worker_log_path, f"worker.start pid={os.getpid()} run_dir={artifact_dir}\n")
    started_at_ms = _now_ms()

    try:
        _patch_system_config_path(spec["private_config_path"])
        _configure_run_logging(artifact_dir)

        from agentic.app import build_runtime_services
        from agentic.orchestrator import AgenticOrchestrator
        from agentic.workspaces import workspace_scope

        runtime = build_runtime_services()
        orchestrator = AgenticOrchestrator(
            user_store_manager=runtime["user_store_manager"],
            agent_config=runtime["agent_config"],
        )
        event_count = 0
        final_output: Any = None
        status = "running"
        error: str | None = None

        with workspace_scope(spec.get("workspace_id") or DEFAULT_WORKSPACE_ID):
            for event in orchestrator.run(
                user_request=spec["question"],
                user_context=spec.get("context") or {},
                session_id=spec["session_id"],
                user_id=spec.get("user_id") or DEFAULT_USER_ID,
                workspace_id=spec.get("workspace_id") or DEFAULT_WORKSPACE_ID,
                requested_run_id=spec["run_id"],
            ):
                event_count += 1
                _append_jsonl(events_path, event)
                event_data = event.get("data", event) if isinstance(event, dict) else event
                if isinstance(event_data, dict) and event_data.get("type") == "turn.finished":
                    payload = event_data.get("payload") or {}
                    status = str(payload.get("status") or "success")
                    final_output = payload.get("final_output")
                    error = payload.get("error")

        finished_at_ms = _now_ms()
        result = {
            "status": status,
            "run_id": spec["run_id"],
            "session_id": spec["session_id"],
            "user_id": spec.get("user_id"),
            "workspace_id": spec.get("workspace_id"),
            "artifact_dir": str(artifact_dir),
            "started_at_ms": started_at_ms,
            "finished_at_ms": finished_at_ms,
            "duration_ms": _duration_ms(started_at_ms, finished_at_ms),
            "event_count": event_count,
            "final_output": final_output,
            "error": error,
            "paths": spec.get("paths") or {},
        }
        _write_json(result_path, result)
        _append_text(worker_log_path, f"worker.finished status={status} events={event_count}\n")
        return 0 if status == "success" else 1
    except Exception as exc:
        finished_at_ms = _now_ms()
        result = {
            "status": "error",
            "run_id": spec.get("run_id"),
            "session_id": spec.get("session_id"),
            "artifact_dir": str(artifact_dir),
            "started_at_ms": started_at_ms,
            "finished_at_ms": finished_at_ms,
            "duration_ms": _duration_ms(started_at_ms, finished_at_ms),
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "paths": spec.get("paths") or {},
        }
        _write_json(result_path, result)
        _append_text(worker_log_path, result["traceback"])
        return 1


def read_status(run_dir: str) -> dict[str, Any]:
    artifact_dir = Path(run_dir).resolve()
    result = _read_json(artifact_dir / RESULT_FILENAME)
    launch = _read_json(artifact_dir / LAUNCH_FILENAME) or {}
    if result:
        return result
    pid = launch.get("pid")
    running = _pid_is_running(pid) if isinstance(pid, int) else False
    return {
        "status": "running" if running else "unknown",
        "pid": pid,
        "artifact_dir": str(artifact_dir),
        "launched_at_ms": launch.get("launched_at_ms"),
        "result_path": str(artifact_dir / RESULT_FILENAME),
    }


def read_result(run_dir: str) -> dict[str, Any]:
    artifact_dir = Path(run_dir).resolve()
    result = _read_json(artifact_dir / RESULT_FILENAME)
    if not result:
        raise ValueError(f"result not ready: {artifact_dir / RESULT_FILENAME}")
    return result


def print_logs(run_dir: str, *, tail: int = 0) -> int:
    artifact_dir = Path(run_dir).resolve()
    paths = [artifact_dir / WORKER_LOG_FILENAME]
    log_dir = artifact_dir / "logs"
    if log_dir.exists():
        paths.extend(sorted(log_dir.glob("*.log*")))
    lines: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        lines.append(f"===== {path} =====\n")
        try:
            lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines(True))
        except OSError as exc:
            lines.append(f"<failed to read: {exc}>\n")
    if tail and tail > 0:
        lines = lines[-tail:]
    sys.stdout.write("".join(lines))
    return 0


def _patch_system_config_path(path: str) -> None:
    import agentic.config.system_config as system_config

    system_config.CONFIG_PATH = Path(path).resolve()


def _configure_run_logging(run_dir: Path) -> None:
    from agentic.utils.logging import configure_logging

    configure_logging(log_dir=str(run_dir / "logs"), log_file="agent.log", console=False, force=True)


def _resolve_run_dir(output_dir: str | None, run_id: str) -> Path:
    if output_dir:
        path = Path(output_dir).expanduser()
        return path.resolve() if path.name == run_id else (path / run_id).resolve()
    from agentic.config.system_config import load_system_config

    project_root = Path(load_system_config()["storage"]["project_root"])
    return (project_root / ARTIFACT_ROOT_NAME / run_id).resolve()


def _start_response(run_dir: Path, spec: dict[str, Any], launch: dict[str, Any]) -> dict[str, Any]:
    wrapper = _repo_root() / "tools" / "agent_sim.py"
    return {
        "status": "launched",
        "pid": launch["pid"],
        "run_id": spec["run_id"],
        "session_id": spec["session_id"],
        "artifact_dir": str(run_dir),
        "result_path": str(run_dir / RESULT_FILENAME),
        "events_path": str(run_dir / EVENTS_FILENAME),
        "logs_command": f"{sys.executable} {wrapper} logs {run_dir}",
        "status_command": f"{sys.executable} {wrapper} status {run_dir}",
        "result_command": f"{sys.executable} {wrapper} result {run_dir}",
    }


def _public_spec(spec: dict[str, Any]) -> dict[str, Any]:
    public = dict(spec)
    public.pop("private_config_path", None)
    public["paths"] = dict(spec.get("paths") or {})
    return public


def _parse_context_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _backend_root() -> Path:
    return _repo_root() / "backend"


def _pythonpath_with_backend() -> str:
    backend = str(_backend_root())
    current = os.environ.get("PYTHONPATH")
    return backend if not current else os.pathsep.join([backend, current])


def _now_ms() -> int:
    return int(time.time() * 1000)


def _duration_ms(started_at_ms: int, finished_at_ms: int) -> int:
    return max(0, int(finished_at_ms) - int(started_at_ms))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _append_jsonl(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")


def _append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _pid_is_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


if __name__ == "__main__":
    raise SystemExit(main())
