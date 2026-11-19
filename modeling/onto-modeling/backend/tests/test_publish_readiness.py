"""发布就绪（ADR-0103）：校验之后按发布检查同一份代码再查，会挡发布的问题交给助手修，修不掉的进待处理清单。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.api import deps
from app.kernel.changesets import service as changesets
from app.kernel.changesets import store
from app.services import agent_sessions
from app.services import publish as publish_service
from app.services.autobuild import runner as runner_module
from app.services.publish import readiness
from app.services.session_host import exec_records
from app.services.workspace import WorkspaceManager, _git
from tests.publish_fixtures import complete_publish_stages
from tests.test_changesets import agent_write, obj, project  # noqa: F401 - project 是 fixture
from tests.test_publish_binding_gate import FIELDS_HEADER, write_object, write_schema

DUPLICATE_KEY_OBJECT = (
    "# 对象描述\n合成对象。\n\n# 英文名称\nRecord\n\n# 数据来源\n无\n\n" + FIELDS_HEADER
    + "| 编号 | record_id | 合成字段 | 是 | 字符串 | 无 | 无 |\n"
    + "| 原编号 | record_id | 合成字段 | 否 | 字符串 | 无 | 无 |\n"
)


@pytest.fixture()
def manager(tmp_path: Path, monkeypatch) -> WorkspaceManager:
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    instance = WorkspaceManager(tmp_path / "data", assets)
    monkeypatch.setattr(instance, "_mount_assets", lambda root, packs=None: None)
    return instance


def _committed(manager: WorkspaceManager, objects: dict[str, str]) -> tuple[str, Path]:
    pid = manager.create_project("发布就绪")["id"]
    root = manager.project_root(pid)
    (root / "ontology" / "object").mkdir(parents=True, exist_ok=True)
    for key, text in objects.items():
        (root / "ontology" / "object" / f"{key}.md").write_text(text, encoding="utf-8")
    complete_publish_stages(root, commit=False)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "test: synthetic fixture")
    return pid, root


# ---- 与发布检查同一份结论 ----


def test_findings_are_the_publish_gates_that_degrading_cannot_bypass(manager):
    pid, root = _committed(manager, {"记录": DUPLICATE_KEY_OBJECT})
    found = readiness.findings_at(manager, pid, "HEAD")
    check = publish_service.package_binding_check(manager, pid)
    structure = next(gate for gate in check["gates"] if gate["id"] == "package_structure")
    assert structure["blocking"] and found
    assert {item["message"] for item in found} == {item["message"] for item in structure["findings"]}
    # 树对象（助手副本的快照）和提交得出同样的结论
    assert readiness.findings_at(manager, pid, _git(root, "rev-parse", "HEAD^{tree}").strip()) == found


def test_data_binding_alone_is_not_a_readiness_problem(manager):
    pid = manager.create_project("只差绑定")["id"]
    root = manager.project_root(pid)
    write_schema(root, {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    write_object(root, "客户", "T_CLIENT", ["CUSTOMER_ID", "NAME"])
    complete_publish_stages(root, commit=False)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "test: synthetic fixture")
    check = publish_service.package_binding_check(manager, pid)
    assert next(gate for gate in check["gates"] if gate["id"] == "data_binding")["blocking"]
    assert readiness.findings_at(manager, pid, "HEAD") == []
    assert readiness.mainline_findings(manager, pid) == []


# ---- 交给助手的话与待处理清单 ----


def test_fix_prompt_lists_findings_with_locations_and_caps_the_list():
    findings = [{"code": "relation_structure_invalid", "message": f"关系端点「对象{n}」不存在",
                 "path": "ontology/object/甲.md", "line": n} for n in range(25)]
    text = readiness.fix_prompt(findings)
    assert text.startswith("发布检查发现 25 个会挡住发布的问题")
    assert "关系端点「对象0」不存在（ontology/object/甲.md 第 0 行）" not in text  # 行号 0 不算位置
    assert "（ontology/object/甲.md 第 3 行）" in text and text.count("\n- ") == readiness.PROMPT_LIMIT + 1
    assert "另有 5 个" in text and "从文档里删掉这一条" in text and readiness.LEFTOVER_PATH in text


def test_leftover_is_written_as_pending_items_and_removed_once_clean(manager):
    pid, root = _committed(manager, {"记录": DUPLICATE_KEY_OBJECT})
    found = readiness.findings_at(manager, pid, "HEAD")
    commit = readiness.record_leftover(manager, pid, found, "employee_0001")
    path = root / readiness.LEFTOVER_PATH
    assert commit and path.is_file()
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("- ")]
    assert lines and all(line.startswith("- [ ] ❌ [发布检查] ") for line in lines)
    log = _git(root, "log", "-1", "--format=%B")
    assert log.startswith("chore: 发布检查还有") and "Onto-Source: plugin" in log
    # 内容不变不提交；查干净了删掉清单
    assert readiness.record_leftover(manager, pid, found, "employee_0001") is None
    assert readiness.record_leftover(manager, pid, [], "employee_0001")
    assert not path.exists()
    assert readiness.record_leftover(manager, pid, [], "employee_0001") is None


# ---- 自主构建：校验这一步的变更集带上发布就绪检查 ----


def _observe(workspaces, project_id, root, node_id: str, text: str):
    copy = changesets.copy_for_turn(workspaces, project_id, root)
    agent_write(copy, "ontology/object/记录.md", text, commit="agent: 记录")
    return changesets.observe(workspaces, project_id, running=False, conversation_id="default", checks=True,
                              exec_record={"id": "ex-1", "command_label": "校验",
                                           "node": {"id": node_id, "version": "core@0.6.1"}})


def test_validate_changesets_carry_publish_findings(project):
    workspaces, project_id, root = project
    duplicate = obj("记录", "合成记录", rows=(("编号", "code", "主键编号", "是", "字符串"),
                                          ("原编号", "code", "上游编号", "否", "字符串")))
    record = _observe(workspaces, project_id, root, "core.validate", duplicate)
    assert changesets.readiness_step(record)
    publish = store.get(root, record["id"])["checks"]["publish"]
    assert publish and all(item["level"] == "error" for item in publish)


def test_other_changesets_skip_the_publish_check(project):
    workspaces, project_id, root = project
    record = _observe(workspaces, project_id, root, "core.operation", obj("记录", "合成记录"))
    assert not changesets.readiness_step(record)
    assert "publish" not in store.get(root, record["id"])["checks"]


def test_autobuild_fix_message_covers_publish_findings(monkeypatch):
    sent = []

    async def fake_send(project_id, run_id, unit, text, **_kwargs):
        sent.append(text)

    async def nothing(*_args, **_kwargs):
        return None

    instance = runner_module.AutobuildRunner()
    monkeypatch.setattr(instance, "_send", fake_send)
    monkeypatch.setattr(instance, "_wait_turn", nothing)
    monkeypatch.setattr(instance, "_account", nothing)
    monkeypatch.setattr(runner_module.store, "get", lambda *_args: {"conversation_id": "default"})
    publish = [{"code": "field_aliases_invalid", "level": "error", "message": "字段 key 重复"}]
    asyncio.run(instance._fix("p", "r", {"name": "校验"}, [], publish))
    assert sent and sent[0].startswith("发布检查发现 1 个会挡住发布的问题") and "只改「校验」" not in sent[0]


# ---- 陪伴建模：校验做完后平台代发修复，最多 ROUNDS 轮 ----


class _Recorder:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.leftovers: list[list] = []


@pytest.fixture()
def companion(tmp_path, monkeypatch):
    recorder = _Recorder()
    findings = [{"code": "field_aliases_invalid", "level": "error", "message": "字段 key 重复"}]
    monkeypatch.setattr(readiness, "mainline_findings", lambda _ws, _pid: list(findings))
    monkeypatch.setattr(readiness, "record_leftover",
                        lambda _ws, _pid, found, _actor: recorder.leftovers.append(found))
    monkeypatch.setattr(agent_sessions, "project_owner", lambda _pid: "employee_0001")
    monkeypatch.setattr(deps, "workspaces", type("Workspaces", (), {"project_root": lambda _self, _pid: tmp_path})())
    manager = agent_sessions.SessionManager()

    async def fake_send(project_id, cwd, text, conversation_id=None, username="", *, meta=None):
        recorder.sent.append((text, meta or {}))

    monkeypatch.setattr(manager, "send", fake_send)
    session = agent_sessions.Session(id="s-1", project_id="p-1", cwd=tmp_path)
    return manager, session, recorder, findings


def test_readiness_is_due_after_a_finished_validate_or_a_fix_round(companion, monkeypatch):
    manager, session, _recorder, _ = companion
    records = {"ex-v": {"state": "done", "node": {"id": "core.validate"}},
               "ex-w": {"state": "waiting", "node": {"id": "core.validate"}},
               "ex-o": {"state": "done", "node": {"id": "core.operation"}}}
    monkeypatch.setattr(exec_records, "get", lambda _pid, exec_id: records.get(exec_id))
    assert manager._readiness_due(session, "ex-v")
    assert not manager._readiness_due(session, "ex-w") and not manager._readiness_due(session, "ex-o")
    assert not manager._readiness_due(session, None)
    session.readiness_round = 1
    assert manager._readiness_due(session, None)
    session.run_id = "run-1"  # 自主构建的对话由运行器查
    assert not manager._readiness_due(session, "ex-v")


def test_companion_sends_fixes_then_records_what_is_left(companion):
    manager, session, recorder, findings = companion
    for _ in range(readiness.ROUNDS):
        asyncio.run(manager._check_publish_readiness(session))
    assert len(recorder.sent) == readiness.ROUNDS and session.readiness_round == readiness.ROUNDS
    text, meta = recorder.sent[0]
    assert text == readiness.fix_prompt(findings)
    assert meta["origin"]["label"] == "发布检查" and meta["publish_readiness"] is True
    assert recorder.leftovers == []
    asyncio.run(manager._check_publish_readiness(session))  # 轮数用完：记进待处理清单，不再代发
    assert len(recorder.sent) == readiness.ROUNDS and recorder.leftovers == [findings]
    assert session.readiness_round == 0


def test_companion_does_not_interrupt_when_you_already_moved_on(companion):
    manager, session, recorder, _ = companion
    session.turns_in_flight = 1
    asyncio.run(manager._check_publish_readiness(session))
    assert recorder.sent == [] and recorder.leftovers == []


def test_companion_clean_check_removes_the_list(companion, monkeypatch):
    manager, session, recorder, _ = companion
    monkeypatch.setattr(readiness, "mainline_findings", lambda _ws, _pid: [])
    session.readiness_round = 1
    asyncio.run(manager._check_publish_readiness(session))
    assert recorder.sent == [] and recorder.leftovers == [[]] and session.readiness_round == 0
