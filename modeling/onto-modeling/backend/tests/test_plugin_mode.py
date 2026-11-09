"""工作台插件开发模式（ADR-0087，契约 F17）：插件对话、插件开发装配目录、守卫、宿主分流、草稿接口 PD-01～PD-05。

用户名、包名、内容均为合成数据；假引擎不起真实 CLI。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.kernel.changesets import service as changesets
from app.kernel.changesets import worktree
from app.kernel_guards import _guard_common as guards
from app.main import app
from app.routers import sessions as session_routes
from app.services import agent_sessions, auth, conversation_store, streams
from app.services.agent_sessions import SessionManager
from app.services.engines.jiuwen_assets import JiuwenAssetLoader
from app.services.packs import builtin, lockfile
from app.services.plugin_mode import assembly, drafts
from app.services.session_host import exec_records, fingerprint, injection
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import make_node_pack, make_pack
from tests.test_event_streams import buffered
from tests.test_private_packs import AS_OTHER, AS_OWNER, MINE, OWNER, TOKENS, post_zip, project, zipped
from tests.test_session_host import FakeEngine, result, settle

REASON = "插件开发模式只能改 plugins/ 下的草稿"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def put_draft(root: Path, source: Path, name: str | None = None, message: str = "web: 合成草稿") -> Path:
    """把包目录作为主线上的草稿 plugins/<name>/ 提交（模拟一轮合入）。"""
    target = root / "plugins" / (name or source.name.rsplit("-", 1)[0])
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    git(root, "add", "-A", "--", "plugins")
    git(root, "commit", "-qm", message)
    return target


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    """镜像里的真实资产（开发 skill、插件助手规则），鉴权打开。"""
    builtin.clear_cache()
    assembly.clear_cache()
    drafts.clear_cache()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: TOKENS.get(token))
    monkeypatch.setattr(changesets, "_activity_provider", None)
    streams.reset_all()
    yield TestClient(app), manager, tmp_path
    streams.reset_all()
    builtin.clear_cache()
    assembly.clear_cache()
    drafts.clear_cache()


# ---------------------------------------------------------------------------
# 守卫
# ---------------------------------------------------------------------------


@pytest.fixture()
def guarded(tmp_path: Path, monkeypatch):
    data = tmp_path / "data"
    copy = data / "changesets" / "p-mine" / "assistant"
    for rel in ("plugins/demo", "ontology/object", "reports"):
        (copy / rel).mkdir(parents=True)
    (data / "workspaces" / "p-mine" / "plugins").mkdir(parents=True)
    (data / "workspaces" / "p-other" / "ontology").mkdir(parents=True)
    os.symlink(data / "workspaces" / "p-mine" / "resource", copy / "resource")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(copy))

    def check(tool: str, **tool_input) -> str | None:
        return guards.check("plugin_scope", {"tool_name": tool, "tool_input": tool_input, "cwd": str(copy)})

    return check, copy, data


def test_guard_allows_only_drafts_under_plugins(guarded):
    check, copy, data = guarded
    assert check("Write", file_path="plugins/demo/manifest.yaml") is None
    assert check("Edit", file_path=str(copy / "plugins/demo/commands/demo-run.md")) is None
    assert check("Bash", command="mkdir -p plugins/demo/commands && cat > plugins/demo/a.md <<'EOF'\n合成\nEOF") is None
    assert check("Bash", command="python3 .claude/plugin-dev/capability/scripts/pack_tool.py init demo --out plugins") is None
    assert check("Bash", command="python3 .claude/plugin-dev/capability/scripts/pack_tool.py check plugins/demo") is None
    assert check("Bash", command="cat ontology/object/订单.md && echo 合成 > /tmp/onto-scratch.txt") is None
    assert check("Bash", command="python3 -c \"open('plugins/demo/ontology/x.md','w').write('1')\"") is None
    # 文件描述符重定向不是写入路径（实测助手常写 `2>/dev/null`、`2>&1`）
    assert check("Bash", command="rm -f plugins/demo/a.py && rmdir plugins/demo/scripts 2>/dev/null; ls -R plugins/demo") is None
    assert check("Bash", command="rmdir plugins/demo/scripts 2>&1; ls plugins/demo >&2") is None
    assert check("Bash", command="pack_tool check plugins/demo > plugins/demo/out.txt 2>&1") is None


@pytest.mark.parametrize("tool, tool_input", [
    ("Write", {"file_path": "ontology/object/门店.md"}),
    ("Edit", {"file_path": "reports/合成报告.md"}),
    ("Write", {"file_path": "resource/others/资料.md"}),
    ("Write", {"file_path": ".claude/settings.json"}),
    ("Write", {"file_path": "CLAUDE.md"}),
    ("Write", {"file_path": "plugins"}),
    ("Bash", {"command": "rm -rf ."}),
    ("Bash", {"command": "echo 合成 > ontology/object/门店.md"}),
    ("Bash", {"command": "cd plugins/demo && sed -i 's/a/b/' ../../ontology/object/订单.md"}),
    ("Bash", {"command": "python3 -c \"open('ontology/object/a.md','w').write('x')\""}),
    ("Bash", {"command": "python3 - <<'EOF'\nfrom pathlib import Path\nPath('reports/x.md').write_text('1')\nEOF"}),
    ("Bash", {"command": "echo 合成 | tee ontology/object/a.md"}),
    ("Bash", {"command": "cat plugins/demo/a.md > \"$DEST\" && ls ontology"}),
    ("Bash", {"command": "ln -s /etc plugins/demo/etc"}),
    ("Bash", {"command": "ls plugins 2>&1 >& reports/x.md"}),
    ("Bash", {"command": "ls plugins 2> reports/err.txt"}),
])
def test_guard_denies_ontology_materials_and_project_files(guarded, tool, tool_input):
    check, _copy, _data = guarded
    reason = check(tool, **tool_input)
    assert reason and reason.startswith(REASON), reason


def test_guard_denies_other_projects_and_the_mainline_workspace(guarded):
    check, _copy, data = guarded
    other = data / "workspaces" / "p-other" / "ontology" / "a.md"
    assert check("Write", file_path=str(other)).startswith(REASON)
    # 主线工作区也不行：只能在副本里改，每轮由平台合入
    assert check("Write", file_path=str(data / "workspaces" / "p-mine" / "plugins" / "demo" / "a.md")).startswith(REASON)
    assert check("Bash", command=f"python3 -c \"open('{other}','w').write('x')\"").startswith(REASON)
    assert check("Write", file_path=str(data / "packs" / "demo" / "0.1.0" / "manifest.yaml")).startswith(REASON)


def test_guard_script_denies_through_the_hook_protocol(guarded):
    _check, copy, _data = guarded
    script = Path(guards.__file__).parent / "plugin_scope_guard.sh"
    payload = {"tool_name": "Write", "tool_input": {"file_path": "ontology/object/a.md"}, "cwd": str(copy)}
    done = subprocess.run(["bash", str(script)], input=json.dumps(payload), capture_output=True, text=True,
                          env={**os.environ, "CLAUDE_PROJECT_DIR": str(copy)}, timeout=30)
    decision = json.loads(done.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny" and REASON in decision["permissionDecisionReason"]
    payload["tool_input"]["file_path"] = "plugins/demo/a.md"
    done = subprocess.run(["bash", str(script)], input=json.dumps(payload), capture_output=True, text=True,
                          env={**os.environ, "CLAUDE_PROJECT_DIR": str(copy)}, timeout=30)
    assert done.stdout.strip() == ""


def test_modeling_settings_do_not_reference_the_plugin_guard():
    from app import kernel_guards

    assert "plugin_scope_guard" not in json.dumps(kernel_guards.settings_base())


# ---------------------------------------------------------------------------
# 插件开发装配目录
# ---------------------------------------------------------------------------


def test_plugin_dev_assembly_contents_and_stable_hash(env):
    _client, manager, tmp_path = env
    directory = assembly.ensure(manager)
    assert directory.parent == tmp_path / "data" / "plugin-dev" and directory.name == assembly.digest(manager)
    assert (directory / "CLAUDE.md").read_text(encoding="utf-8") == (
        Path(config.ASSETS_DIR) / "plugin-mode" / "CLAUDE.md").read_text(encoding="utf-8")
    for category in ("capability", "industry", "system"):
        skill = directory / "plugin-dev" / category
        for rel in ("SKILL.md", "platform.json", "references/protocol.md", f"references/{category}.md",
                    "scripts/pack_tool.py", "scripts/checker/services/packs/checks.py"):
            assert (skill / rel).is_file(), rel
        assert any((skill / "references" / "examples").iterdir())
    assert not (directory / "plugin-dev" / "provider").exists()  # 不支持连接器包
    assert sorted(path.name for path in (directory / "scripts").iterdir()) == [
        "_guard_common.py", "plugin_scope_guard.sh", "resource_read_guard.sh"]
    settings = json.loads((directory / "settings.json").read_text(encoding="utf-8"))
    hooks = {item["matcher"]: [hook["command"] for hook in item["hooks"]] for item in settings["hooks"]["PreToolUse"]}
    assert ["plugin_scope" in cmd for cmd in hooks["Bash"]] == [True, False] and "resource_read" in hooks["Bash"][1]
    assert "resource_read" in hooks["Read|NotebookRead"][0]
    assert "plugin_scope" in hooks["Edit|Write|MultiEdit|NotebookEdit"][0]
    assert {"Write(/.claude/**)", "Write(/CLAUDE.md)", "Edit(/.onto/**)"} <= set(settings["permissions"]["deny"])
    # 内容不变，哈希不变：清掉进程缓存再生成还是同一个目录
    assembly.clear_cache()
    assert assembly.ensure(manager) == directory and assembly.is_plugin_dev(directory)


def test_both_engines_read_the_plugin_rules_and_the_offline_tool_works(env):
    _client, manager, tmp_path = env
    directory = assembly.ensure(manager)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    os.symlink(directory, workspace / ".claude")
    os.symlink(".claude/CLAUDE.md", workspace / "CLAUDE.md")
    loader = JiuwenAssetLoader.for_workspace(workspace)
    prompt = loader.system_prompt()
    assert prompt.startswith("# 插件开发助手") and "state_helper" not in prompt and "research" not in prompt
    assert {pattern.pattern for pattern, _ in loader.pre_tool_use_hooks()} == {
        "^(?:Bash)$", "^(?:Read|NotebookRead)$", "^(?:Edit|Write|MultiEdit|NotebookEdit)$"}
    assert loader.expand_command("/ontology-research 合成") == "/ontology-research 合成"
    tool = workspace / ".claude" / "plugin-dev" / "capability" / "scripts" / "pack_tool.py"
    done = subprocess.run([sys.executable, str(tool), "init", "demo-notes", "--out", "plugins"], cwd=workspace,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    done = subprocess.run([sys.executable, str(tool), "check", "plugins/demo-notes"], cwd=workspace,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (workspace / "plugins" / "demo-notes" / "checksums.sha256").is_file()
    assert not list(directory.rglob("__pycache__"))  # 跑工具不往装配目录里留缓存


# ---------------------------------------------------------------------------
# .claude 改指
# ---------------------------------------------------------------------------


def _project_with_copy(client, manager) -> tuple[str, Path]:
    project_id = project(client)
    root = manager.project_root(project_id)
    changesets.copy_for_turn(manager, project_id, root)
    return project_id, root


def test_override_link_survives_other_ensure_calls_and_is_restored(env):
    client, manager, _tmp = env
    project_id, root = _project_with_copy(client, manager)
    copy = worktree.copy_path(root)
    modeling = os.path.realpath(root / ".claude")
    target = assembly.ensure(manager)
    changesets.copy_for_turn(manager, project_id, root, claude_override=target)
    assert os.path.realpath(copy / ".claude") == str(target)
    assert os.path.realpath(copy / "CLAUDE.md") == str(target / "CLAUDE.md")
    # 这一轮里别处借用副本（Studio 复核、修副本）不会改回去
    with manager._project_lock(project_id):
        worktree.ensure(root)
    changesets.staging_dir(manager, project_id)
    assert os.path.realpath(copy / ".claude") == str(target)
    assert git(copy, "status", "--porcelain") == ""
    # 建模一轮开始：清标记、链回建模的装配
    changesets.copy_for_turn(manager, project_id, root, claude_override=None)
    assert os.path.realpath(copy / ".claude") == modeling
    assert not worktree.override_path(root).exists()
    # 标记改成指向别处（不在 plugin-dev/ 下）不认
    elsewhere = _tmp / "elsewhere"
    elsewhere.mkdir()
    worktree.override_path(root).write_text(str(elsewhere), encoding="utf-8")
    with manager._project_lock(project_id):
        worktree.ensure(root)
    assert os.path.realpath(copy / ".claude") == modeling
    # 标记指着的目录没了：按建模装配链
    changesets.copy_for_turn(manager, project_id, root, claude_override=target)
    shutil.rmtree(target)
    with manager._project_lock(project_id):
        worktree.ensure(root)
    assert os.path.realpath(copy / ".claude") == modeling


# ---------------------------------------------------------------------------
# 对话、指纹、注入
# ---------------------------------------------------------------------------


def test_plugin_conversation_create_and_bind(env):
    client, _manager, _tmp = env
    project_id = project(client)
    base = f"/api/v1/projects/{project_id}/conversations"
    created = client.post(base, json={"kind": "plugin"}, headers=AS_OWNER)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["kind"] == "plugin" and body["plugin"] is None and body["title"] == "新插件"
    assert client.post(base, json={"kind": "autobuild"}, headers=AS_OWNER).status_code == 400
    chat = client.post(base, json={}, headers=AS_OWNER).json()
    assert chat["kind"] == "chat" and chat["plugin"] is None
    bound = conversation_store.bind_plugin(project_id, body["id"], "demo-notes", "纪要术语")
    assert bound["plugin"] == "demo-notes" and bound["title"] == "纪要术语"
    assert conversation_store.bind_plugin(project_id, body["id"], "other") is None  # 一条对话只绑一个包
    listed = {item["id"]: item for item in client.get(base, headers=AS_OWNER).json()["items"]}
    assert listed[body["id"]]["plugin"] == "demo-notes"
    assert conversation_store.plugin_conversation(project_id, "demo-notes") == body["id"]
    # 手工改过标题的不跟着 label 改
    second = conversation_store.create(project_id, kind="plugin")
    conversation_store.rename(project_id, second["id"], "我的插件")
    assert conversation_store.bind_plugin(project_id, second["id"], "x-pack", "某包")["title"] == "我的插件"


class _Engine:
    name = "claude"

    def model_fingerprint(self):
        return "m"


def test_fingerprint_counts_plugin_dev_instead_of_assembly(env, monkeypatch):
    client, manager, _tmp = env
    project_id = project(client)
    from app.services.engines import claude

    monkeypatch.setattr(claude, "mcp_fingerprint_source", lambda pid: {})
    chat = fingerprint.compute(_Engine(), project_id, OWNER)
    plugin = fingerprint.compute(_Engine(), project_id, OWNER, "plugin")
    assert "assembly" in chat.parts and "plugin_dev" not in chat.parts
    assert "assembly" not in plugin.parts and "flow" not in plugin.parts
    assert plugin.parts["plugin_dev"] == assembly.digest(manager)
    changed = fingerprint.Fingerprint({**plugin.parts, "plugin_dev": "old"})
    assert fingerprint.changes(_Engine(), project_id, OWNER, changed, "plugin") == ["plugin_dev"]
    assert fingerprint.describe(["plugin_dev"]) == [{"kind": "plugin_dev", "summary": "插件开发说明有更新"}]


def test_injection_carries_only_the_draft_state(env, tmp_path):
    client, manager, _tmp = env
    project_id = project(client)
    root = manager.project_root(project_id)
    conversation = conversation_store.create(project_id, kind="plugin")["id"]
    first = injection.build_context(root, user_text="做个插件", session_start=True, last_injected=None,
                                    conversation_id=conversation)
    assert "新草稿建在 plugins/<包名>/" in first.text and "阶段" not in first.text and "下一步" not in first.text
    # 草稿的命令没带包前缀：预检没过的项带上
    put_draft(root, make_pack(tmp_path / "src", "demo-notes", "0.1.0", files={"commands/run.md": "运行\n"}))
    conversation_store.bind_plugin(project_id, conversation, "demo-notes")
    text = injection.build_context(root, user_text="继续", session_start=False, last_injected=first.digest,
                                   conversation_id=conversation)
    assert "plugins/demo-notes/" in text.text and "还没装" in text.text and "名字不冲突" in text.text
    again = injection.build_context(root, user_text="再看", session_start=False, last_injected=text.digest,
                                    conversation_id=conversation)
    assert again.text == ""
    chat = injection.build_context(root, user_text="继续", session_start=True, last_injected=None,
                                   conversation_id="default")
    assert "插件开发" not in chat.text


# ---------------------------------------------------------------------------
# 会话宿主：插件对话的一轮
# ---------------------------------------------------------------------------


def _draft_files(name: str) -> dict[str, str]:
    data = {"name": name, "version": "0.1.0", "kind": "capability", "label": "纪要术语", "kernel_api": ">=1.0 <2.0",
            "publisher": {"kind": "tenant", "label": ""}, "contributes": {"commands": [f"{name}-run"]}}
    return {"manifest.yaml": yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            f"commands/{name}-run.md": "# 整理纪要术语\n\n运行 $ARGUMENTS\n"}


def test_plugin_round_links_plugin_rules_and_posts_a_draft_card(env, monkeypatch):
    client, manager, _tmp = env
    project_id, root = _project_with_copy(client, manager)
    sessions = SessionManager()
    monkeypatch.setattr(sessions, "_engine_class_for", lambda pid: FakeEngine)
    monkeypatch.setattr(agent_sessions, "manager", sessions)
    monkeypatch.setattr(session_routes, "manager", sessions)
    conversation = client.post(f"/api/v1/projects/{project_id}/conversations", json={"kind": "plugin"},
                               headers=AS_OWNER).json()["id"]
    seen: dict = {}

    def write(ctx):
        cwd = Path(ctx.cwd)
        seen["claude"] = os.path.realpath(cwd / ".claude")
        seen["context"] = ctx.injector.context_for("做一个纪要术语插件")
        for rel, text in _draft_files("demo-notes").items():
            path = cwd / "plugins" / "demo-notes" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    FakeEngine.scripts, FakeEngine.sent = [[write, result()], [result()]], []

    async def run():
        await sessions.send(project_id, root, "做一个纪要术语插件", conversation, username=OWNER)
        await settle(sessions, project_id, until=lambda session: session.state == "idle" and not session.sending
                     and not session.finishing)
        await sessions.send(project_id, root, "/ontology-research 合成资料", conversation, username=OWNER)
        await settle(sessions, project_id, until=lambda session: session.state == "idle" and not session.sending
                     and not session.finishing)

    asyncio.run(run())
    assert assembly.is_plugin_dev(Path(seen["claude"])) and "还没有草稿" in seen["context"]
    history = sessions.get_history(project_id, conversation)
    cards = [event["payload"] for event in history if event.get("type") == "card"]
    assert [card.get("kind") for card in cards] == ["plugin_draft"]
    card = cards[0]
    assert card["name"] == "demo-notes" and card["version"] == "0.1.0" and card["changeset_id"]
    assert card["card_type"] == "plugin_draft" and card["card_id"] == f"pd-{card['changeset_id']}-demo-notes"
    assert card["changed_files"] == ["commands/demo-notes-run.md", "manifest.yaml"]
    assert not any(event.get("type") == "workspace_changes" for event in history)
    bars = [event["payload"] for event in history if event.get("type") == "suggestions" and event["payload"].get(
        "mode") != "running"]
    assert bars and all(bar["primary"] is None and bar["secondary"] == [] and not bar["all_commands"] for bar in bars)
    # 合入主线成一个版本；对话绑上草稿，标题换成 label
    assert (root / "plugins" / "demo-notes" / "manifest.yaml").is_file()
    entry = conversation_store.entry(project_id, conversation)
    assert entry["plugin"] == "demo-notes" and entry["title"] == "纪要术语"
    assert buffered(project_id, "plugin_drafts.changed")[-1]["data"] == {"names": ["demo-notes"]}
    # 斜杠命令不开执行记录
    assert exec_records.open_record(project_id, conversation) is None
    assert FakeEngine.sent[-1].startswith("/ontology-research")
    # 这一轮结束，副本链回建模的装配
    assert os.path.realpath(worktree.copy_path(root) / ".claude") == os.path.realpath(root / ".claude")
    assert not worktree.override_path(root).exists()
    # 草稿卡上的「撤回这一轮」：CS-02 查得到、已合入，CS-05 撤回
    changeset = client.get(f"/api/v1/projects/{project_id}/changesets/{card['changeset_id']}", headers=AS_OWNER)
    assert changeset.status_code == 200 and changeset.json()["state"] == "merged"
    assert changeset.json()["conversation_id"] == conversation
    flow = client.get(f"/api/v1/projects/{project_id}/flow/suggestions", params={"conversation_id": conversation},
                      headers=AS_OWNER).json()
    assert flow["primary"] is None and flow["secondary"] == [] and flow["all_commands"] is False
    full = client.get(f"/api/v1/projects/{project_id}/flow", params={"conversation_id": conversation},
                      headers=AS_OWNER).json()
    assert full["suggestions"]["primary"] is None and full["nodes"]
    reverted = client.post(f"/api/v1/projects/{project_id}/changesets/{card['changeset_id']}/discard", json={},
                           headers=AS_OWNER)
    assert reverted.status_code == 200, reverted.text
    assert not (root / "plugins" / "demo-notes" / "manifest.yaml").exists(), list((root / "plugins").rglob("*"))
    history = sessions.get_history(project_id, conversation)
    assert not any(event.get("type") == "card_updated" for event in history)
    assert [event["payload"]["kind"] for event in history if event.get("type") == "notice"][-1] == "discarded"
    asyncio.run(sessions.shutdown())


def test_failed_plugin_round_restores_the_modeling_link(env, monkeypatch):
    client, manager, _tmp = env
    project_id, root = _project_with_copy(client, manager)
    sessions = SessionManager()
    monkeypatch.setattr(sessions, "_engine_class_for", lambda pid: FakeEngine)
    monkeypatch.setattr(agent_sessions, "manager", sessions)
    conversation = conversation_store.create(project_id, kind="plugin")["id"]
    FakeEngine.scripts, FakeEngine.sent = [[{"type": "_engine_end", "reason": "error", "error": "合成故障"}]], []

    async def run():
        await sessions.send(project_id, root, "做一个插件", conversation, username=OWNER)
        await settle(sessions, project_id, until=lambda session: session.state == "error")
        for _ in range(100):
            if not worktree.override_path(root).exists():
                break
            await asyncio.sleep(0.02)

    asyncio.run(run())
    assert not worktree.override_path(root).exists()
    assert os.path.realpath(worktree.copy_path(root) / ".claude") == os.path.realpath(root / ".claude")
    asyncio.run(sessions.shutdown())


# ---------------------------------------------------------------------------
# 草稿接口 PD-01～PD-05
# ---------------------------------------------------------------------------


def _drafts(project_id: str) -> str:
    return f"/api/v1/projects/{project_id}/plugin-drafts"


def test_draft_list_detail_and_files(env, tmp_path):
    client, manager, _tmp = env
    project_id = project(client)
    root = manager.project_root(project_id)
    put_draft(root, make_node_pack(tmp_path / "src"))  # naming：节点挂在「校验」之后
    broken = root / "plugins" / "broken"
    broken.mkdir(parents=True)
    (broken / "manifest.yaml").write_text("name: [\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "web: 坏草稿")
    listed = client.get(_drafts(project_id), headers=AS_OWNER)
    assert listed.status_code == 200, listed.text
    items = {item["name"]: item for item in listed.json()["items"]}
    assert set(items) == {"naming", "broken"}
    assert items["broken"]["manifest_ok"] is False and items["broken"]["precheck"]["state"] == "failed"
    naming = items["naming"]
    assert "files" not in naming and naming["precheck"]["state"] == "passed", naming["precheck"]
    assert naming["install"] == {"state": "not_installed", "enabled_version": None, "installed_versions": [],
                                 "next_version": "0.1.0", "changes": [], "diff": None, "reuse": False}
    node = naming["contents"]["nodes"][0]
    assert node["id"] == "naming.check" and node["after"] == "core.validate" and node["after_title"]
    assert node["blocks_publish"] is False
    assert naming["contents"]["commands"] == [{"name": "naming-check", "label": "naming-check"}]
    detail = client.get(f"{_drafts(project_id)}/naming", headers=AS_OWNER).json()
    assert {item["path"] for item in detail["files"]} >= {"manifest.yaml", "flow.yaml", "commands/naming-check.md"}
    assert {item["id"] for item in detail["precheck"]["items"]} >= {"manifest", "names", "private_name", "dir_name"}
    assert detail["updated_at"] and detail["conversation_id"] is None
    assert client.get(f"{_drafts(project_id)}/nope", headers=AS_OWNER).status_code == 404
    assert client.get(_drafts(project_id), headers=AS_OTHER).status_code in (403, 404)

    read = client.get(f"{_drafts(project_id)}/naming/files", params={"path": "commands/naming-check.md"},
                      headers=AS_OWNER).json()
    assert read["content"].startswith("按命名约定") and read["binary"] is False and read["size"] == len(
        read["content"].encode("utf-8"))
    assert client.get(f"{_drafts(project_id)}/naming/files", params={"path": "../broken/manifest.yaml"},
                      headers=AS_OWNER).status_code == 404

    url = f"{_drafts(project_id)}/naming/files"
    saved = client.put(url, json={"path": "commands/naming-check.md", "content": "按新约定检查 $ARGUMENTS\n"},
                       headers=AS_OWNER)
    assert saved.status_code == 200, saved.text
    assert saved.json()["path"] == "commands/naming-check.md" and saved.json()["size"] == len(
        "按新约定检查 $ARGUMENTS\n".encode("utf-8"))
    assert saved.json()["draft"]["name"] == "naming" and saved.json()["draft"]["files"]
    assert (root / "plugins/naming/commands/naming-check.md").read_text(encoding="utf-8").startswith("按新约定")
    assert "web: 改插件草稿" in git(root, "log", "-1", "--format=%s")
    assert buffered(project_id, "plugin_drafts.changed")[-1]["data"] == {"names": ["naming"]}
    for bad in ("checksums.sha256", "../x.md", "/etc/x", ".git/config"):
        denied = client.put(url, json={"path": bad, "content": "x"}, headers=AS_OWNER)
        assert denied.status_code == 400, (bad, denied.text)


def test_write_refused_while_the_plugin_conversation_runs(env, tmp_path, monkeypatch):
    client, manager, _tmp = env
    project_id = project(client)
    put_draft(manager.project_root(project_id), make_node_pack(tmp_path / "src"))
    conversation = conversation_store.create(project_id, kind="plugin")["id"]
    monkeypatch.setattr(agent_sessions.manager, "live_status",
                        lambda pid: {"conversation_id": conversation, "state": "running", "busy": True,
                                     "waiting": False})
    busy = client.put(f"{_drafts(project_id)}/naming/files", json={"path": "a.md", "content": "x"}, headers=AS_OWNER)
    assert busy.status_code == 409 and busy.json()["code"] == "session_busy"
    assert client.post(f"{_drafts(project_id)}/naming/install", json={}, headers=AS_OWNER).status_code == 409


def test_install_enables_then_upgrades_with_an_automatic_patch_version(env, tmp_path):
    client, manager, _tmp = env
    project_id = project(client)
    root = manager.project_root(project_id)
    put_draft(root, make_node_pack(tmp_path / "src", tenant=True))
    chat = conversation_store.create(project_id)["id"]
    conversation = conversation_store.create(project_id, kind="plugin")["id"]
    conversation_store.bind_plugin(project_id, conversation, "naming", "命名检查")
    url = f"{_drafts(project_id)}/naming"

    first = client.post(f"{url}/install", json={}, headers=AS_OWNER)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["version"] == "0.1.0" and body["upload_id"].startswith("up-")
    assert body["enabled"]["version"] == "0.1.0" and body["enabled"]["effective"]
    # 装好且没改：再点安装用的就是装好的这个版本
    assert body["draft"]["install"]["state"] == "installed" and body["draft"]["install"]["next_version"] == "0.1.0"
    assert body["draft"]["conversation_id"] == conversation
    repository = manager.packs.repository
    assert repository.owner("naming") == OWNER and repository.has("naming", "0.1.0")
    entry = lockfile.read(root).get("naming")
    assert entry.enabled and entry.version == "0.1.0"
    uploads = client.get(MINE, headers=AS_OWNER).json()["items"]
    assert uploads[0]["state"] == "installed" and uploads[0]["name"] == "naming"
    notices = [event["payload"] for event in agent_sessions.manager.get_history(project_id, conversation)
               if event.get("type") == "notice"]
    assert notices[-1]["kind"] == "plugin_installed" and notices[-1]["text"] == "「naming 包」0.1.0 已装好，启用到本项目"
    chat_notices = [event["payload"] for event in agent_sessions.manager.get_history(project_id, chat)
                    if event.get("type") == "notice"]
    assert chat_notices[-1]["kind"] == "plugin_enabled" and chat_notices[-1]["text"] == "插件「naming 包」0.1.0 已启用"
    assert buffered(project_id, "plugin_drafts.changed")[-1]["data"] == {"names": ["naming"]}

    # 再改：和启用的版本不一样了
    client.put(f"{url}/files", json={"path": "commands/naming-check.md", "content": "按新约定检查 $ARGUMENTS\n"},
               headers=AS_OWNER)
    outdated = client.get(url, headers=AS_OWNER).json()["install"]
    assert outdated["state"] == "outdated" and outdated["enabled_version"] == "0.1.0"
    assert outdated["changes"] == [{"path": "commands/naming-check.md", "status": "modified"}]
    assert outdated["diff"]["path"] == "commands/naming-check.md"
    assert {"op": "+", "text": "按新约定检查 $ARGUMENTS"} in outdated["diff"]["lines"]
    assert outdated["installed_versions"] == ["0.1.0"] and outdated["next_version"] == "0.1.1"

    second = client.post(f"{url}/install", json={}, headers=AS_OWNER)
    assert second.status_code == 200, second.text
    assert second.json()["version"] == "0.1.1"
    # 版本号作为平台提交写回草稿，项目升级到新版本
    assert yaml.safe_load((root / "plugins/naming/manifest.yaml").read_text(encoding="utf-8"))["version"] == "0.1.1"
    assert "版本号升到 0.1.1" in git(root, "log", "--format=%s", "-3")
    assert lockfile.read(root).get("naming").version == "0.1.1"
    assert second.json()["draft"]["install"]["state"] == "installed"

    # 停用（PL-09）也发草稿变化；再装走启用
    disabled = client.post(f"/api/v1/projects/{project_id}/packs/naming/disable", json={}, headers=AS_OWNER)
    assert disabled.status_code == 200, disabled.text
    assert len(buffered(project_id, "plugin_drafts.changed")) >= 4
    after = client.get(url, headers=AS_OWNER).json()["install"]
    assert after["state"] == "not_installed"
    # 草稿和装过的 0.1.1 一模一样：再装直接启用 0.1.1，不再上传、不占新版本号（实测停用后再装白占了一个版本）
    assert after["next_version"] == "0.1.1" and after["reuse"] is True
    uploads_before = len(client.get(MINE, headers=AS_OWNER).json()["items"])
    third = client.post(f"{url}/install", json={}, headers=AS_OWNER)
    assert third.status_code == 200, third.text
    assert third.json()["version"] == "0.1.1" and third.json()["upload_id"] is None
    assert len(client.get(MINE, headers=AS_OWNER).json()["items"]) == uploads_before
    assert repository.versions("naming") == ["0.1.1", "0.1.0"]
    entry = lockfile.read(root).get("naming")
    assert entry.enabled and entry.version == "0.1.1"
    assert third.json()["draft"]["install"]["state"] == "installed"
    notices = [event["payload"] for event in agent_sessions.manager.get_history(project_id, conversation)
               if event.get("type") == "notice"]
    assert notices[-1]["text"] == "「naming 包」0.1.1 已启用到本项目"


def test_reverting_the_draft_to_an_installed_version_rolls_back_without_a_new_upload(env, tmp_path):
    client, manager, _tmp = env
    project_id = project(client)
    root = manager.project_root(project_id)
    put_draft(root, make_node_pack(tmp_path / "src", tenant=True))
    url = f"{_drafts(project_id)}/naming"
    original = (root / "plugins/naming/commands/naming-check.md").read_text(encoding="utf-8")
    assert client.post(f"{url}/install", json={}, headers=AS_OWNER).json()["version"] == "0.1.0"
    client.put(f"{url}/files", json={"path": "commands/naming-check.md", "content": "按新约定检查 $ARGUMENTS\n"},
               headers=AS_OWNER)
    assert client.post(f"{url}/install", json={}, headers=AS_OWNER).json()["version"] == "0.1.1"
    uploads = len(client.get(MINE, headers=AS_OWNER).json()["items"])
    # 撤回到 0.1.0 的内容：清单号和内容都和装过的 0.1.0 一样
    manifest = (root / "plugins/naming/manifest.yaml").read_text(encoding="utf-8").replace("version: 0.1.1", "version: 0.1.0")
    client.put(f"{url}/files", json={"path": "manifest.yaml", "content": manifest}, headers=AS_OWNER)
    client.put(f"{url}/files", json={"path": "commands/naming-check.md", "content": original}, headers=AS_OWNER)
    draft = client.get(url, headers=AS_OWNER).json()["install"]
    assert draft["state"] == "outdated" and draft["reuse"] is True and draft["next_version"] == "0.1.0"
    back = client.post(f"{url}/install", json={}, headers=AS_OWNER)
    assert back.status_code == 200, back.text
    assert back.json()["version"] == "0.1.0" and back.json()["upload_id"] is None
    assert lockfile.read(root).get("naming").version == "0.1.0"
    assert len(client.get(MINE, headers=AS_OWNER).json()["items"]) == uploads


def test_install_refuses_failed_prechecks_and_taken_names(env, tmp_path):
    client, manager, _tmp = env
    project_id = project(client)
    root = manager.project_root(project_id)
    put_draft(root, make_pack(tmp_path / "src", "loose-notes", "0.1.0", files={"commands/run.md": "运行\n"}))
    failed = client.post(f"{_drafts(project_id)}/loose-notes/install", json={}, headers=AS_OWNER)
    assert failed.status_code == 422 and failed.json()["code"] == "static_check_failed"
    details = failed.json()["details"]
    assert [item["id"] for item in details["items"]] == ["names"] and details["items"][0]["result"] == "fail"
    assert details["failed"] == [details["items"][0]["label"]]
    assert client.get(MINE, headers=AS_OWNER).json()["items"] == []  # 预检没过不建上传记录

    # 别人的私有包占了这个名字
    other = make_pack(tmp_path / "theirs", "taken-name", "0.1.0", tenant=True)
    accepted = post_zip(client, MINE, zipped(other), AS_OTHER).json()["id"]
    assert client.post(f"{MINE}/{accepted}/install", json={}, headers=AS_OTHER).status_code == 200
    put_draft(root, make_pack(tmp_path / "mine", "taken-name", "0.1.0", tenant=True))
    detail = client.get(f"{_drafts(project_id)}/taken-name", headers=AS_OWNER).json()
    assert {item["id"]: item["result"] for item in detail["precheck"]["items"]}["private_name"] == "fail"
    taken = client.post(f"{_drafts(project_id)}/taken-name/install", json={}, headers=AS_OWNER)
    assert taken.status_code == 409 and taken.json()["code"] == "pack_name_taken"

    # 目录名和包名不一致
    put_draft(root, make_pack(tmp_path / "moved", "real-name", "0.1.0", tenant=True), name="other-dir")
    items = client.get(f"{_drafts(project_id)}/other-dir", headers=AS_OWNER).json()["precheck"]["items"]
    assert {item["id"]: item["result"] for item in items}["dir_name"] == "fail"


def test_drafts_stay_out_of_search_entries_and_reports(env, tmp_path):
    client, manager, _tmp = env
    project_id = project(client)
    root = manager.project_root(project_id)
    put_draft(root, make_pack(tmp_path / "src", "demo-notes", "0.1.0", files={
        "commands/demo-notes-run.md": "合成独有词语 $ARGUMENTS\n", "reports/x.md": "合成独有词语\n",
        "ontology/object/假对象.md": "# 假对象\n合成独有词语\n"}))
    base = f"/api/v1/projects/{project_id}"
    found = client.get(f"{base}/search", params={"q": "合成独有词语"}, headers=AS_OWNER).json()
    assert all(group["total"] == 0 for group in found["groups"]), found
    entries = client.get(f"{base}/studio/entries", headers=AS_OWNER).json()
    assert "假对象" not in json.dumps(entries, ensure_ascii=False)
    from app.services.publish import layout

    assert not any(path.startswith("plugins") for path in layout.PACKAGE_SOURCE_DIRS)
