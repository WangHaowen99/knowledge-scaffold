"""state_helper 执行锁的持有者绑定到引擎实例（B5 前置，B10 加 init 的前提）。

后端每次连接引擎时写 .onto/run/engine.json；state_helper 由后端子孙进程调用时，持有者是
这个引擎实例，与后端是不是 1 号进程、引擎是 CLI 子进程还是进程内运行无关。
这里用测试进程充当后端：子进程跑 state_helper，测试进程就是它的祖先。
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import config
from tests.proc import popen, run as run_cmd

HELPER = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts" / "state_helper.py"


def run_helper(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    return run_cmd(
        [sys.executable, str(HELPER), *args],
        cwd=workspace, capture_output=True, text=True, timeout=30,
    )


def write_marker(workspace: Path, engine_id: str, host_pid: int | None = None) -> None:
    path = workspace / ".onto" / "run" / "engine.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "engine_id": engine_id,
        "host_pid": os.getpid() if host_pid is None else host_pid,
        "engine": "jiuwen",
    }), encoding="utf-8")


def read_lock(workspace: Path, command: str = "extract-object") -> dict:
    return json.loads((workspace / ".ontology-state" / f"lock-{command}.json").read_text(encoding="utf-8"))


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "ontology" / "object").mkdir(parents=True)
    return tmp_path


def test_engine_owned_lock_is_shared_across_calls(workspace):
    write_marker(workspace, "engine_a")

    assert run_helper(workspace, "acquire", "extract-object", "source").stdout.strip() == "ok"
    lock = read_lock(workspace)
    assert lock["owner"] == "engine:engine_a" and "owner_pid" not in lock

    # 同一引擎实例的另一次 Bash 调用（进程不同）可续写阶段
    result = run_helper(workspace, "acquire", "extract-object", "objects")
    assert result.returncode == 0 and result.stdout.strip() == "ok"
    assert read_lock(workspace)["phase"] == "objects"
    # 本会话自己拿的锁：查到的是 own（上下文压缩后再查，不能当成残留清掉，F 第 5 条）
    assert run_helper(workspace, "check-lock", "extract-object").stdout.startswith("own:")


def test_other_caller_still_sees_engine_lock_as_locked(workspace):
    write_marker(workspace, "engine_a")
    run_helper(workspace, "acquire", "extract-object", "objects")
    bystander = popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        write_marker(workspace, "engine_a", host_pid=bystander.pid)
        assert run_helper(workspace, "check-lock", "extract-object").stdout.strip().startswith("locked:")
    finally:
        bystander.kill()


def test_new_engine_instance_makes_old_lock_stale(workspace):
    write_marker(workspace, "engine_a")
    run_helper(workspace, "acquire", "extract-object", "source")

    write_marker(workspace, "engine_b")  # 回收、重启、改配置后重连
    assert run_helper(workspace, "check-lock", "extract-object").stdout.startswith("stale:")
    assert run_helper(workspace, "acquire", "extract-object", "start").returncode == 0
    assert read_lock(workspace)["owner"] == "engine:engine_b"


def test_process_outside_backend_cannot_borrow_engine_lock(workspace):
    write_marker(workspace, "engine_a")
    run_helper(workspace, "acquire", "extract-object", "source")

    # 同一个在线引擎，但调用者不是它的后端的子孙（例如在终端里人工跑脚本）
    bystander = popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        write_marker(workspace, "engine_a", host_pid=bystander.pid)
        result = run_helper(workspace, "acquire", "extract-object", "start")
    finally:
        bystander.kill()
        bystander.wait()
    assert result.returncode == 2
    assert result.stdout.startswith("conflict:")
    assert "engine:engine_a" in result.stderr


def test_without_marker_falls_back_to_process_owner(workspace):
    assert run_helper(workspace, "acquire", "extract-object", "start").returncode == 0
    lock = read_lock(workspace)
    assert "owner" not in lock and isinstance(lock["owner_pid"], int) and lock["owner_pid"] > 1


@pytest.mark.parametrize("owner_pid", [1, 0])
def test_lock_owned_by_pid_one_is_stale(workspace, owner_pid):
    """没加 init 的容器里后端是 1 号进程，那时写下的锁持有者是 1；加 init 后 1 号是 tini。"""
    state = workspace / ".ontology-state"
    state.mkdir()
    (state / "lock-extract-object.json").write_text(json.dumps({
        "command": "extract-object", "phase": "objects",
        "started_at": "2026-11-01 08:00:00", "owner_pid": owner_pid,
    }), encoding="utf-8")
    write_marker(workspace, "engine_a")

    assert run_helper(workspace, "check-lock", "extract-object").stdout.startswith("stale:")
    assert run_helper(workspace, "acquire", "extract-object", "start").returncode == 0


def test_ancestor_walk_starts_from_parent():
    spec = importlib.util.spec_from_file_location("state_helper_owner_probe", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ancestors = module._ancestor_pids()
    assert ancestors and ancestors[0] == os.getppid()
    assert os.getpid() not in ancestors
