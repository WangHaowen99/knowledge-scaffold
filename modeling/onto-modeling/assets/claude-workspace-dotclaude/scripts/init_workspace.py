"""初始化一个建模工作目录。被两条路径共用：

1. init_task.sh —— 用户级建模任务（workspace/<任务名>/）
2. eval/tooling/paths.py —— 评测 run 的 builder/cwd/

初始化内容：
- 软链 .claude / CLAUDE.md / README.md 到工具集根（相对路径，按嵌套层数自动算）
- 复制 resource/ 和 .obsidian/ 到任务目录（每任务独立）
- 写 .gitignore（来自 .claude/templates/task-gitignore.template）
- git init -b main + 首次提交（记录工具集快照 SHA）

CLI 用法：
    python3 .claude/scripts/init_workspace.py <target_dir> <toolset_root> <task_label>
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


SYMLINK_NAMES = (".claude", "CLAUDE.md", "README.md")
COPY_NAMES = ("resource", ".obsidian")
GITIGNORE_TEMPLATE_REL = ".claude/templates/task-gitignore.template"
DEFAULT_BRANCH = "main"


def _toolset_snapshot_sha(toolset_root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=toolset_root, check=True, capture_output=True, text=True,
        )
        return out.stdout.strip() or "unknown"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def init_modeling_workspace(
    target_dir: Path,
    toolset_root: Path,
    task_label: str,
) -> None:
    """在 target_dir 下初始化一个独立的建模工作区。

    target_dir 必须是空目录或不存在（不存在会被创建）。toolset_root 是工具集
    仓根，task_label 写进首次提交 message 用以人类辨识。

    中途失败时会把 target_dir 整个 rmtree 回滚，便于重试。
    """
    target_dir = Path(target_dir).resolve()
    toolset_root = Path(toolset_root).resolve()

    if target_dir.exists() and not target_dir.is_dir():
        raise FileExistsError(f"target_dir 存在但不是目录：{target_dir}")
    if target_dir.is_dir() and any(target_dir.iterdir()):
        raise FileExistsError(f"target_dir 非空：{target_dir}")

    preexisted = target_dir.exists()
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        _populate(target_dir, toolset_root, task_label)
    except BaseException:
        # 任何异常都回滚到调用前状态——空目录或不存在
        shutil.rmtree(target_dir, ignore_errors=True)
        if preexisted:
            target_dir.mkdir(parents=True, exist_ok=True)
        raise


def _populate(target_dir: Path, toolset_root: Path, task_label: str) -> None:
    rel_to_toolset = Path(os.path.relpath(toolset_root, target_dir))

    for name in SYMLINK_NAMES:
        link = target_dir / name
        link.symlink_to(rel_to_toolset / name)

    # 必需的起点资源——缺一个就让 copytree 抛 FileNotFoundError，
    # 因为这种缺失意味着工具集 checkout 残缺，应在 init 时就报出来，
    # 而不是让 Builder 后面跑 /ontology-research 时见到空 resource/ 才挂
    for name in COPY_NAMES:
        shutil.copytree(toolset_root / name, target_dir / name)

    template = toolset_root / GITIGNORE_TEMPLATE_REL
    shutil.copy(template, target_dir / ".gitignore")

    snapshot = _toolset_snapshot_sha(toolset_root)
    # commit 强制关 GPG 签名——否则用户开了全局 gpgsign 会卡 passphrase
    env_args = [
        "-c", "user.email=ontology-toolset@local",
        "-c", "user.name=ontology-toolset",
        "-c", "commit.gpgsign=false",
    ]
    subprocess.run(
        ["git", "init", "-q", "-b", DEFAULT_BRANCH],
        cwd=target_dir, check=True,
    )
    subprocess.run(["git", "add", ".gitignore"], cwd=target_dir, check=True)
    commit_msg = (
        f"init: 建模任务起点\n\n"
        f"任务名：{task_label}\n"
        f"工具集快照：{snapshot}\n"
    )
    subprocess.run(
        ["git", *env_args, "commit", "-q", "-m", commit_msg],
        cwd=target_dir, check=True,
    )


def _main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "用法: python3 .claude/scripts/init_workspace.py "
            "<target_dir> <toolset_root> <task_label>",
            file=sys.stderr,
        )
        return 2
    target_dir, toolset_root, task_label = argv[1], argv[2], argv[3]
    init_modeling_workspace(Path(target_dir), Path(toolset_root), task_label)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
