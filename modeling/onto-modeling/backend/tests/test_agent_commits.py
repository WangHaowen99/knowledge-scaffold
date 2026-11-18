"""agent 提交收口（方案 C11；详设 4.3、P2 验收「写入都带来源 trailer」）：

- 各项目的 git 指向内核发布的提交钩子；agent 的提交（带会话身份 ONTO_RUN_ENGINE）在提交那一刻
  跑提交检查——`git add -A && git commit` 写在一行时也看得到刚暂存的内容；
- agent 的提交都带来源 trailer，agent 自己写的 Onto-* 行以宿主记下的为准；
- 后端自己的 git 命令、人工提交不经钩子；
- PreToolUse 拦跳过提交检查的写法；钩子就绪时把检查交给 git 钩子；
- MCP 写入不再因为 agent 有暂存改动就回「项目忙」（方案 C10）。

需要 bash、git、python3；测试数据全部为合成内容。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app import config
from app.kernel.writepath import agent_commits
from app.services.workspace import WorkspaceManager
from app.services.workspace.gitops import _run_git
from tests.proc import run as run_cmd

pytestmark = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("git") and shutil.which("python3")),
    reason="需要 bash、git、python3",
)
GUARD = Path(__file__).resolve().parents[1] / "app" / "kernel_guards" / "pre_commit_check.sh"


@pytest.fixture()
def project(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    meta = manager.create_project("提交收口", owner="employee_0001")
    root = manager.project_root(meta["id"])
    agent_commits.install(root)
    marker = root / ".onto" / "run" / "engine.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"engine_id": "engine-1", "conversation_id": "default", "owner": "employee_0001"}),
                      encoding="utf-8")
    return manager, meta["id"], root


def agent_shell(root: Path, command: str, *, agent: bool = True, extra: dict | None = None) -> subprocess.CompletedProcess:
    """模拟 agent 在工作区里执行一条 bash（带不带会话身份）。"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("ONTO_RUN_")}
    env.update(extra or {})
    env.update({"GIT_AUTHOR_NAME": "agent", "GIT_AUTHOR_EMAIL": "agent@example.invalid",
                "GIT_COMMITTER_NAME": "agent", "GIT_COMMITTER_EMAIL": "agent@example.invalid"})
    if agent:
        env.update({"ONTO_RUN_ENGINE": "engine-1", "ONTO_RUN_PROJECT": root.name, "ONTO_RUN_CONVERSATION": "default"})
    return run_cmd(["bash", "-c", command], cwd=root, env=env, capture_output=True, text=True, timeout=120)


def last_message(root: Path) -> str:
    return run_cmd(["git", "log", "-1", "--format=%B"], cwd=root, capture_output=True, text=True).stdout


def pre_tool_use(root: Path, command: str, *, agent: bool = True) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("ONTO_RUN_")}
    if agent:
        env["ONTO_RUN_ENGINE"] = "engine-1"
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(root)})
    proc = run_cmd(["bash", str(GUARD)], cwd=root, input=payload, env=env, capture_output=True, text=True,
                   timeout=120)
    return proc.stdout.strip()


def test_hooks_published_and_installed(project):
    _, _, root = project
    hooks = agent_commits.hooks_dir()
    assert all(os.access(hooks / name, os.X_OK) for name in agent_commits.HOOK_NAMES)
    assert agent_commits.installed(root)
    assert agent_commits.install(root) is False  # 已经指向就不动


def test_repository_shared_with_agent_group_only_when_switching(project, monkeypatch):
    """agent 以独立用户运行时仓库按组共享、写不进的目录交给 share_git_dir 修；不切用户时什么都不动。"""
    from app.governance import agent_identity

    _, _, root = project

    def shared() -> str:
        return _run_git(root, ("config", "--local", "--get", "core.sharedRepository")).stdout.strip()

    _run_git(root, ("config", "--local", "--unset", "core.sharedRepository"))  # 以 root + agent 用户跑时夹具已设过
    monkeypatch.setattr(agent_identity, "switching", lambda: False)
    agent_commits.install(root)
    assert shared() == ""
    repaired = []
    monkeypatch.setattr(agent_identity, "switching", lambda: True)
    monkeypatch.setattr(agent_identity, "share_git_dir", lambda git_dir: repaired.append(git_dir) or 0)
    assert agent_commits.install(root) is True
    assert shared() == "group" and repaired == [root / ".git"]
    assert agent_commits.install(root) is False  # 已经共享、没有要修的
    monkeypatch.setattr(agent_identity, "share_git_dir", lambda git_dir: 3)
    assert agent_commits.install(root) is True  # 修了几处也算改动


def test_agent_commit_carries_source_trailers_and_drops_forged_ones(project):
    _, _, root = project
    (root / ".onto" / "run" / "exec.json").write_text(
        json.dumps({"exec_id": "ex-1", "node": "core.object", "node_version": "core@1.4.2",
                    "conversation_id": "default"}), encoding="utf-8")
    (root / "notes").mkdir()
    (root / "notes" / "草稿.txt").write_text("合成笔记\n", encoding="utf-8")
    message = "extract: 订单对象\n\nOnto-Source: web\nOnto-Author: someone_else"
    proc = agent_shell(root, 'git add -A && git commit -q -m "$MSG"', extra={"MSG": message})
    assert proc.returncode == 0, proc.stderr
    body = last_message(root)
    trailers = dict(line.split(": ", 1) for line in body.splitlines() if line.startswith("Onto-"))
    assert trailers["Onto-Source"] == "agent" and trailers["Onto-Author"] == "employee_0001"
    assert trailers["Onto-Node"] == "core.object@1.4.2"
    assert trailers["Onto-Packs"].startswith("core@")
    assert body.count("Onto-Source") == 1 and "someone_else" not in body
    # 版本列表把它记成助手（契约 VersionSource：agent → assistant）
    from app.services.workspace import versions

    assert versions._TRAILER_SOURCES["agent"] == "assistant"


def test_add_and_commit_in_one_line_is_checked_on_real_index(project):
    _, _, root = project
    (root / "resource").mkdir(exist_ok=True)
    (root / "resource" / "资料.txt").write_text("合成资料\n", encoding="utf-8")
    # PreToolUse 在命令执行之前：暂存区还是空的，它放行（检查交给 git 钩子）
    assert pre_tool_use(root, "git add -A && git commit -m 'research: 资料'") == ""
    proc = agent_shell(root, "git add -A && git commit -q -m 'research: 资料'")
    assert proc.returncode != 0
    assert "resource/" in proc.stderr and "resource/资料.txt" in proc.stderr
    assert "research: 资料" not in last_message(root)


def test_human_and_backend_commits_skip_agent_hooks(project):
    _, _, root = project
    (root / "resource").mkdir(exist_ok=True)
    (root / "resource" / "人工.txt").write_text("合成资料\n", encoding="utf-8")
    proc = agent_shell(root, "git add -A && git commit -q -m 'manual: 人工整理'", agent=False)
    assert proc.returncode == 0, proc.stderr
    assert "Onto-Source" not in last_message(root)
    # 后端的 git 命令一律带 -c core.hooksPath=/dev/null：即使环境里有会话身份也不经钩子
    (root / "resource" / "后端.txt").write_text("合成资料\n", encoding="utf-8")
    _run_git(root, ("add", "-A"))
    result = _run_git(root, ("commit", "-q", "-m", "import: 后端提交"),
                      env={**os.environ, "ONTO_RUN_ENGINE": "engine-1"})
    assert result.returncode == 0, result.stderr
    assert "Onto-Source" not in last_message(root)


@pytest.mark.parametrize("command", [
    "git commit --no-verify -m x",
    "git commit -nm x",
    "git -c core.hooksPath=/tmp/hooks commit -m x",
    "git config core.hooksPath /tmp/hooks",
    "git config --unset core.hooksPath",
])
def test_pre_tool_use_blocks_bypassing_commit_checks(project, command):
    _, _, root = project
    decision = json.loads(pre_tool_use(root, command))
    assert decision["decision"] == "block" and "提交检查" in decision["reason"]


def test_pre_tool_use_falls_back_when_hooks_not_ready(project):
    _, _, root = project
    (root / "resource").mkdir(exist_ok=True)
    (root / "resource" / "资料.txt").write_text("合成资料\n", encoding="utf-8")
    _run_git(root, ("add", "-A"))
    # 没有会话身份（钩子不会管这次提交）：在 PreToolUse 里照旧检查已暂存的内容
    decision = json.loads(pre_tool_use(root, "git commit -m x", agent=False))
    assert decision["decision"] == "block" and "resource/" in decision["reason"]


def test_mcp_write_no_longer_busy_when_agent_has_staged_changes(project, monkeypatch):
    manager, project_id, root = project
    from app.services.onto_mcp import workspace as mcp

    monkeypatch.setattr(mcp, "editing_issue", lambda *args: None)
    (root / "ontology" / "object").mkdir(parents=True, exist_ok=True)
    (root / "ontology" / "object" / "订单.md").write_text(
        "# 对象描述\n\n合成订单\n\n# 对象属性清单\n\n"
        "| 中文名称 | 英文名称 | 类型 | 描述 | 主键 | 属性别名 | 属性规则 |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| 订单号 | order_no | string | 合成 | 是 | 无 | 无 |\n\n# 关联对象\n\n无\n", encoding="utf-8")
    _run_git(root, ("add", "-A"))
    _run_git(root, ("commit", "-q", "-m", "test: 合成对象"))
    # agent 暂存了一份别的改动、还没提交
    (root / "notes").mkdir(exist_ok=True)
    (root / "notes" / "agent.txt").write_text("合成\n", encoding="utf-8")
    _run_git(root, ("add", "notes/agent.txt"))
    revision = mcp.revision(root)
    result = mcp.execute(manager, project_id, "update_ontology_object",
                         {"object_id": "object:订单", "expected_revision": revision,
                          "metadata": {"description": "合成订单（改）"}}, "employee_0001")
    assert result["object"]["description"] == "合成订单（改）", result
    staged = _run_git(root, ("diff", "--cached", "--name-only")).stdout.split()
    assert staged == ["notes/agent.txt"]  # agent 的暂存原样留着，没被这次提交带走
    assert "notes/agent.txt" not in _run_git(root, ("show", "--name-only", "--format=", "HEAD")).stdout
