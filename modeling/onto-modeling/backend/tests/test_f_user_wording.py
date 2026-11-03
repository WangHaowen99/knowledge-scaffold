"""F 第一批第 4 条：助手对用户说内部步骤和旧版话术。

真实模型（V 阶段）会说「已保存版本 7，如需回退回复"回退"」「回复 **确认生效** → …」「Step 2.5：固化只读来源快照」
「锁检查通过」。来源是工作区规则第 10 条、用户词汇表的动作语句模板、提交后钩子的输出，以及命令文档收尾的「下一步建议」菜单。
现在提交只是记进这一轮的修改，确认生效、撤回、回退都在界面上做。
"""
from __future__ import annotations

import re
from pathlib import Path

from app import config
from tests.test_artifact_verification import git_workspace, hook, put  # noqa: F401 - 夹具

ASSETS = Path(config.ASSETS_DIR)
DOCS = sorted([ASSETS / "workspace-rules.md", *(ASSETS / "workspace-toolkit").glob("*.md"),
               *(ASSETS / "workspace-toolkit").glob("commands/*.md"), *(ASSETS / "workspace-toolkit").glob("agents/*.md"),
               *(ASSETS / "packs").glob("*/commands/*.md"), *(ASSETS / "packs").glob("*/agents/*.md"),
               *(ASSETS / "packs").glob("*/*.md")])
# 让助手照着说的模板（规则里「不说已保存版本 N」这类否定句不算）
OLD_WORDING = re.compile(r"已保存版本\s*[{$]|如需回退到上一版本")


def test_post_commit_hook_does_not_announce_versions(git_workspace):  # noqa: F811
    root, git = git_workspace
    put(root, "ontology/object/EntityA.md", "# 对象描述\n合成。\n")
    git("add", "ontology/object")
    git("commit", "-qm", "extract: synthetic objects")
    out = hook(root, "post_commit_hook.sh").stdout
    assert "这一轮的修改" in out
    assert "已保存版本" not in out and "回退" not in out.replace("不要让用户回复「回退」", "") and "建议运行" not in out


def test_post_commit_hook_has_no_version_or_next_command_wording():
    script = (Path(config.BASE_DIR) / "app" / "kernel_guards" / "post_commit_hook.sh").read_text(encoding="utf-8")
    assert "已保存版本" not in script and "如需回退" not in script and "建议运行 /" not in script


def test_docs_do_not_tell_assistant_to_offer_rollback_by_reply():
    found = [f"{path.relative_to(ASSETS)}:{n}" for path in DOCS
             for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if OLD_WORDING.search(line)]
    assert found == []


def test_rules_tell_assistant_how_to_talk_to_user():
    rules = (ASSETS / "workspace-rules.md").read_text(encoding="utf-8")
    section = rules.split("## 对用户说话", 1)[1]
    for phrase in ("步骤编号", "命令锁", "已保存版本 N", "下一步建议", "建议条"):
        assert phrase in section
