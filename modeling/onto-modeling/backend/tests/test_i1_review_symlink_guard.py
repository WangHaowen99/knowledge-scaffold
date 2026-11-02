"""I1 审查修复第 1 条（多一层防护）：助手建不了指向工作区外的符号链接。

- 守卫：`ln -s` 与内联代码里的 `os.symlink`、`symlink_to`、`symlinkSync`，目标按链接所在目录解析后不在项目根（或副本根）内就拒；
- 每轮核对：一轮开始、一轮结束时把副本与主线工作区里白名单外、指向根外的链接删掉（守卫拦不住的写法兜底）；
- 提交检查：助手的提交里不能有符号链接。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.kernel.changesets import service, worktree
from app.kernel_guards import _guard_common as guards
from tests.test_agent_commits import agent_shell, project as agent_project  # noqa: F401  （夹具按名字注入）
from tests.test_changesets import project  # noqa: F401  （夹具按名字注入）


def guard_says(cwd: Path, command: str) -> str | None:
    os.environ["CLAUDE_PROJECT_DIR"] = str(cwd)
    try:
        return guards.check("toolset", {"cwd": str(cwd), "tool_name": "Bash", "tool_input": {"command": command}})
    finally:
        os.environ.pop("CLAUDE_PROJECT_DIR", None)


# ---- 守卫 ----


@pytest.mark.parametrize("command", [
    "ln -s /etc/hosts reports/x.md",
    "ln -sf /proc/self/environ ontology/object/a.md",
    "ln --symbolic /tmp reports/out",
    "mkdir -p tmp && ln -s ../../.. tmp/up",
    "cd reports && ln -s ../../outside.md leak.md",
    "ln -s -t reports /etc/hosts",
    "ln -sfn /var/data ontology/knowledge",
    "ln -s $HOME/secret reports/y.md",
    "python3 -c \"import os; os.symlink('/proc/self/environ', 'reports/x.md')\"",
    "python3 -c \"from pathlib import Path; Path('reports/x.md').symlink_to('/etc/hosts')\"",
    "node -e \"require('fs').symlinkSync('/etc/hosts', 'reports/x.md')\"",
    "python3 - <<'EOF'\nimport os\nos.symlink(target, 'reports/x.md')\nEOF",
])
def test_guard_denies_links_leaving_the_workspace(project, command):  # noqa: F811
    _workspaces, _project_id, root = project
    reason = guard_says(root, command)
    assert reason and "符号链接" in reason, command


@pytest.mark.parametrize("command", [
    "ln -s ../ontology/object tmp/objects",
    "ln -s object/门店.md ontology/门店-link.md",
    "cd ontology && ln -s object/门店.md 链接.md",
    "ln existing.md hard.md",
    "ls -l reports",
    "python3 -c \"print('symlink 只是一个词')\"",
])
def test_guard_allows_links_inside_and_other_commands(project, command):  # noqa: F811
    _workspaces, _project_id, root = project
    assert guard_says(root, command) is None, command


def test_guard_in_copy_uses_copy_root(project):  # noqa: F811
    workspaces, project_id, root = project
    copy = service.copy_for_turn(workspaces, project_id, root)
    assert guard_says(copy, f"ln -s {root}/ontology reports/main") is not None
    assert guard_says(copy, "ln -s ../ontology/object reports/objects") is None


# ---- 每轮核对 ----


def _plant(base: Path, outside: Path) -> None:
    (base / "reports").mkdir(exist_ok=True)
    (base / "ontology" / "object").mkdir(parents=True, exist_ok=True)
    os.symlink(str(outside / "secret.key"), base / "reports" / "leak.md")
    os.symlink(str(outside), base / "ontology" / "knowledge-out")
    os.symlink("../ontology/object", base / "reports" / "objects")  # 指向根内：留着


def test_sweep_removes_escaping_links_in_copy_and_mainline(project, tmp_path):  # noqa: F811
    workspaces, project_id, root = project
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.key").write_text("top-secret-value\n", encoding="utf-8")
    copy = service.copy_for_turn(workspaces, project_id, root)
    _plant(copy, outside)
    _plant(root, outside)
    service.observe(workspaces, project_id, running=False, conversation_id="default")
    assert not os.path.lexists(copy / "reports" / "leak.md")
    assert not os.path.lexists(copy / "ontology" / "knowledge-out")
    assert (copy / "reports" / "objects").is_symlink()
    # 合法的链接都在
    for name in (".claude", "resource", "CLAUDE.md"):
        assert (copy / name).is_symlink(), name
    # 主线工作区：下一轮开始时清
    service.copy_for_turn(workspaces, project_id, root)
    assert not os.path.lexists(root / "reports" / "leak.md")
    assert not os.path.lexists(root / "ontology" / "knowledge-out")
    assert (root / "reports" / "objects").is_symlink() and (root / ".claude").is_symlink()
    assert (outside / "secret.key").read_text(encoding="utf-8") == "top-secret-value\n"


def test_sweep_keeps_tree_and_skips_git(tmp_path):
    base = tmp_path / "ws"
    (base / ".git").mkdir(parents=True)
    os.symlink("/etc/hosts", base / ".git" / "weird")  # .git 里不管
    (base / "a" / "b").mkdir(parents=True)
    os.symlink("/etc", base / "a" / "b" / "etc")
    os.symlink("/nonexistent/far/away", base / "a" / "dangling")
    removed = worktree.drop_escaping_links(base)
    assert sorted(removed) == ["a/b/etc", "a/dangling"]
    assert (base / ".git" / "weird").is_symlink() and (base / "a" / "b").is_dir()


# ---- 提交检查 ----


@pytest.mark.skipif(not shutil.which("git"), reason="需要 git")
def test_agent_commit_with_symlink_is_blocked(agent_project):  # noqa: F811
    _manager, _project_id, root = agent_project
    (root / "ontology" / "object").mkdir(parents=True, exist_ok=True)
    os.symlink("门店.md", root / "ontology" / "object" / "链接.md")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout
    proc = agent_shell(root, "git add ontology/object/链接.md && git commit -qm 'agent: 链接'")
    assert proc.returncode != 0 and "符号链接" in proc.stderr, proc.stderr
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout == head
