"""Jiuwen 资产装载层测试：命令展开、system prompt 组装、守卫 hooks 真实执行。

守卫测试直接跑 assets 里的 shell 脚本本体（临时 git 工作区），保证与
Claude 引擎的 hook 语义逐字一致。
"""

import json
import subprocess
from pathlib import Path

from app import config
from app.services.engines.jiuwen_assets import JiuwenAssetLoader, run_hooks_sync

ASSETS_DIR = Path(config.ASSETS_DIR)


_ASSEMBLED: dict[str, Path] = {}


def make_loader() -> JiuwenAssetLoader:
    """读真实的装配目录（core 包 + 内核守卫），与项目工作区 .claude 指向的同构。"""
    import tempfile

    from tests.pack_helpers import assemble_real

    if "dir" not in _ASSEMBLED:
        _ASSEMBLED["dir"] = assemble_real(Path(tempfile.mkdtemp(prefix="onto-jiuwen-assets-")))
    return JiuwenAssetLoader(dotclaude=_ASSEMBLED["dir"])


def test_expand_command_substitutes_arguments():
    loader = make_loader()
    expanded = loader.expand_command("/ontology-research 外卖平台")
    assert "外卖平台" in expanded
    assert "$ARGUMENTS" not in expanded
    assert "state_helper.py" in expanded  # 展开的是命令文档全文
    # 非命令与未知命令原样透传
    assert loader.expand_command("你好") == "你好"
    assert loader.expand_command("/no-such-command x") == "/no-such-command x"


def test_system_prompt_contains_rules_and_command_index():
    loader = make_loader()
    prompt = loader.system_prompt()
    assert "Wiki" in prompt or "wiki" in prompt  # workspace-rules 的链接规则
    assert "/ontology-research" in prompt  # 命令索引
    assert "task_tool" in prompt  # 运行时适配注记
    assert len(loader.command_index()) >= 15


def _git_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    # 守卫脚本经 .claude 相对路径自定位（与真实工作区同构：符号链接到装配目录）
    (ws / ".claude").symlink_to(make_loader().toolkit)
    (ws / "resource").mkdir()
    (ws / "resource" / "材料.md").write_text("源材料", encoding="utf-8")
    return ws


def test_hooks_deny_resource_write_and_lock_edit(tmp_path):
    loader = make_loader()
    entries = loader.pre_tool_use_hooks()
    assert entries, "settings.json 应含 PreToolUse 守卫"
    ws = _git_workspace(tmp_path)

    # 写 resource/ → 拒绝（file_path 路径）
    reason = run_hooks_sync(
        entries, "write_file", {"file_path": "resource/材料.md", "content": "x"}, ws
    )
    assert reason and "resource" in reason

    # bash 重定向写 resource/ → 拒绝（命令写目标提取）
    reason = run_hooks_sync(
        entries, "bash", {"command": "echo hi > resource/新文件.md"}, ws
    )
    assert reason and "resource" in reason

    # 手编锁文件 → 拒绝
    reason = run_hooks_sync(
        entries,
        "edit_file",
        {"file_path": ".ontology-state/lock-research.json", "content": "{}"},
        ws,
    )
    assert reason and "锁文件" in reason


def test_hooks_allow_normal_operations(tmp_path):
    loader = make_loader()
    entries = loader.pre_tool_use_hooks()
    ws = _git_workspace(tmp_path)

    assert run_hooks_sync(
        entries, "write_file", {"file_path": "ontology/object/订单.md", "content": "# 订单"}, ws
    ) is None
    assert run_hooks_sync(entries, "bash", {"command": "ls ontology"}, ws) is None
    assert run_hooks_sync(entries, "read_file", {"file_path": "resource/材料.md"}, ws) is None


def test_hooks_deny_toolset_write(tmp_path):
    loader = make_loader()
    entries = loader.pre_tool_use_hooks()
    ws = _git_workspace(tmp_path)
    reason = run_hooks_sync(
        entries,
        "write_file",
        {"file_path": str(ws / ".claude" / "scripts" / "state_helper.py"), "content": "x"},
        ws,
    )
    assert reason is not None  # 工具集资产禁写


def test_hooks_deny_self_invented_ontology_dir(tmp_path):
    """ontology/ 下自创目录任何时候都拒绝——内容进不了知识包。"""
    loader = make_loader()
    entries = loader.pre_tool_use_hooks()
    ws = _git_workspace(tmp_path)

    reason = run_hooks_sync(
        entries, "write_file", {"file_path": "ontology/cases/案例.md", "content": "x"}, ws
    )
    assert reason and "自创目录" in reason

    # 白名单内但不打包的目录，非演进期不拦（建模代理的上下文沉淀区）
    assert run_hooks_sync(
        entries, "write_file", {"file_path": "ontology/knowledge/条目.md", "content": "x"}, ws
    ) is None


def test_hooks_restrict_evolution_writes_to_packaged_dirs(tmp_path):
    """反馈演进期：写入必须落在会进知识包的目录，否则 consumer 看不到。"""
    loader = make_loader()
    entries = loader.pre_tool_use_hooks()
    ws = _git_workspace(tmp_path)
    # 反馈演进节点声明的写入范围（state_helper acquire evolution 写进 .onto/run/active.json，方案 D11）
    active = ws / ".onto" / "run" / "active.json"
    active.parent.mkdir(parents=True)
    active.write_text(json.dumps({"commands": {"evolution": {"node": "evo.evolution", "allow": [
        "ontology/object/*", "ontology/operation/*", "ontology/logic/*", "ontology/rule/*", "ontology/metric/*",
        "ontology/scripts/*", "ontology/testcase/*", "ontology/data-mock/*"]}}}), encoding="utf-8")
    # 守卫只认锁还在的命令（审查第 9 条）：这把锁不带持有者信息，按还在跑
    (ws / ".ontology-state").mkdir(exist_ok=True)
    (ws / ".ontology-state" / "lock-evolution.json").write_text(json.dumps({"command": "evolution"}), encoding="utf-8")

    for rel in ("ontology/knowledge/案例.md", "ontology/action/动作.md"):
        reason = run_hooks_sync(entries, "write_file", {"file_path": rel, "content": "x"}, ws)
        assert reason and ("只能写它声明的范围" in reason or "不允许自创目录" in reason), rel

    for rel in ("ontology/rule/场景规则.md", "ontology/object/订单.md", "ontology/testcase/用例.md"):
        assert run_hooks_sync(
            entries, "write_file", {"file_path": rel, "content": "x"}, ws
        ) is None, rel
