"""I1：资料分析的视图（R-07 资料地图、R-08 候选、R-09 核验、R-10 资料问题转待答，R-01 / R-06 的 coverage）。

数据取资料分析自己的产出（research/ 下的报告、建模蓝图），按 core 的 research_checker 同一套解析。全部为合成内容。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.services import auth, project_events, streams
from app.services.packs import contracts
from app.services.workspace import WorkspaceManager
from tests.test_changesets import main_write
from tests.test_event_streams import buffered

NOTE = "# 合成补货说明\n\nITEM-01 门店按周补货。\n\n受理补货申请分三步。\n\n上限 96 箱。\n"
REPORT = """# 分析主题
合成补货。

# 核心业务对象候选
- **门店**：合成说明

# 核心业务逻辑候选
- **受理补货申请**：合成说明

# 核心规则候选
- **超量规则**：合成说明

# 核心指标候选
无

# 详细信息
合成。

# 业务项清单
| 业务项ID | 业务项名称 | 分类 | 规则ID | 引用字段 | 来源位置 |
| --- | --- | --- | --- | --- | --- |
| ITEM-01 | 门店 | 对象 | 无 | 无 | resource/others/01-补货说明.md#L3 |
| ITEM-02 | 受理补货申请 | 操作 | 无 | 无 | resource/others/01-补货说明.md#L5 |
| ITEM-03 | 超量规则 | 规则 | 无 | 无 | resource/others/01-补货说明.md#L99 |
| ITEM-04 | 门店等级 | 对象 | 无 | 无 | 无 |

# 分类小计
| 分类 | 条数 |
| --- | --- |
| 对象 | 2 |

# 合计
4

# ID变更对照
无

# 数据与术语来源
无

# 信息来源
- resource/others/01-补货说明.md

# 缺口与待决
- [ ] 陈列上限两份资料矛盾：resource/others/01-补货说明.md#L7 与配送说明不一致
- [x] 冷链品要不要单独建对象：不建
- [ ] 缺少退货流程的资料
"""
DRAFT = {"goal_md": "合成目标", "slices": [{"name": "全部"}], "terms": {"aligned": ["门店"]},
         "questions": [{"title": "台账要不要建成对象？", "options": ["要", "不要"]}]}


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    streams.reset_all()
    with TestClient(app) as client:
        project_id = workspaces.create_project("资料分析视图", owner="employee_0001")["id"]
        base = f"/api/v1/projects/{project_id}"
        for name, data in (("01-补货说明.md", NOTE.encode()), ("02-未读.md", "合成。\n".encode())):
            assert client.post(f"{base}/resources", files={"file": (name, data)}).status_code == 201
        yield client, workspaces, project_id, workspaces.project_root(project_id), base
    streams.reset_all()


def test_nothing_analysed_yet(env):
    client, _workspaces, _pid, _root, base = env
    got = client.get(f"{base}/materials/map").json()
    assert got["total"] == 2 and got["read_pct"] == 0 and got["problems"] == []
    assert {item["status"] for item in got["items"]} == {"queued"} and got["can_generate_blueprint"] is False
    assert got["verification"]["total"] == 0 and "analysis_mode" not in got and "web_available" not in got
    assert client.get(f"{base}/materials/candidates").json() == {"total": 0, "items": []}
    assert client.get(f"{base}/materials/verification").json()["items"] == []
    listed = client.get(f"{base}/resources").json()
    assert all(item["coverage"] is None for group in listed["groups"] for item in group["items"])
    preview = client.get(f"{base}/resources/preview", params={"path": "resource/others/01-补货说明.md"}).json()
    assert preview["coverage"] is None


def test_map_candidates_verification_from_the_report(env):
    client, _workspaces, _pid, root, base = env
    main_write(root, "research/20261102-合成补货.md", REPORT, "research: 合成补货")
    got = client.get(f"{base}/materials/map").json()
    items = {item["name"]: item for item in got["items"]}
    read = items["01-补货说明.md"]
    assert read["read_pct"] == 100 and read["status"] == "done" and read["counts"] == {"objects": 1, "terms": 0, "rules": 1}
    assert read["contradiction"] == "1 处矛盾" and items["02-未读.md"]["status"] == "queued"
    assert got["read_pct"] == 50
    problems = got["problems"]
    assert [(p["kind"], p["state"]) for p in problems] == [("contradiction", "open"), ("missing", "resolved"),
                                                          ("missing", "open")]
    assert problems[0]["anchors"] == ["file:resource/others/01-补货说明.md#L7"]
    assert set(got["verification"]) == {"total", "passed", "unverifiable", "pending", "checked_at"}
    verification = client.get(f"{base}/materials/verification").json()
    statuses = {item["subject"]: (item["status"], item["reason"]) for item in verification["items"]}
    assert statuses["ITEM-01 门店"] == ("passed", None)
    assert statuses["ITEM-02 受理补货申请"] == ("unverifiable", "原文里没有编号 ITEM-02")
    assert statuses["ITEM-03 超量规则"] == ("unverifiable", "原文里找不到这个位置")
    assert statuses["ITEM-04 门店等级"] == ("unverifiable", "没有来源位置")
    assert verification["total"] == 4 and verification["passed"] == 1 and verification["problems"] == 3
    first = verification["items"][0]
    # F4c：给人看的出处是「资料名 · 位置」，行号换成它所在的小节标题（ADR-0075）
    assert first["anchor"] == "file:resource/others/01-补货说明.md#L3"
    assert first["anchor_label"] == "01-补货说明 · 合成补货说明"
    assert first["kind"] == "object"
    candidates = client.get(f"{base}/materials/candidates").json()
    assert [(item["kind"], item["name"]) for item in candidates["items"]] == [("object", "门店"), ("rule", "超量规则")]
    assert candidates["items"][0]["anchor"] == "file:resource/others/01-补货说明.md#L3"
    after = client.get(f"{base}/materials/candidates", params={"after": candidates["items"][0]["id"]}).json()
    assert [item["name"] for item in after["items"]] == ["超量规则"] and after["total"] == 2
    # 资料区的 coverage：报告引用过算读过；没验收通过不算读全
    listed = client.get(f"{base}/resources").json()
    coverage = {item["name"]: item["coverage"] for group in listed["groups"] for item in group["items"]}
    assert coverage["01-补货说明.md"] == {"read_pct": 100, "label": "已读"}
    assert coverage["02-未读.md"] == {"read_pct": 0, "label": "未读"}
    preview = client.get(f"{base}/resources/preview", params={"path": "resource/others/01-补货说明.md"}).json()
    assert preview["coverage"] == {"read_sections": "", "read_pct": 100, "complete": False}


def test_promote_problem_into_blueprint_question_and_inbox(env):
    client, _workspaces, _pid, root, base = env
    main_write(root, "research/20261102-合成补货.md", REPORT, "research: 合成补货")
    problem = client.get(f"{base}/materials/map").json()["problems"][2]
    missing_blueprint = client.post(f"{base}/materials/problems/{problem['id']}/promote", json={"as": "ask_me"})
    assert missing_blueprint.status_code == 404 and missing_blueprint.json()["code"] == "blueprint_not_found"
    main_write(root, "research/blueprint.json", contracts.default().load("blueprint").dumps(
        contracts.default().load("blueprint").from_draft(DRAFT)), "research: 合成蓝图")
    got = client.post(f"{base}/materials/problems/{problem['id']}/promote", json={"as": "ask_me"})
    assert got.status_code == 200, got.text
    assert got.json() == {"question_id": "bq-q2", "inbox_item_id": "decision:bq-q2"}
    again = client.post(f"{base}/materials/problems/{problem['id']}/promote", json={"as": "blueprint_question"}).json()
    assert again == {"question_id": "bq-q2", "inbox_item_id": None}
    questions = client.get(f"{base}/blueprint").json()["questions"]
    assert questions[-1]["id"] == "q2" and questions[-1]["title"] == "缺少退货流程的资料"
    assert client.get(f"{base}/materials/map").json()["problems"][2]["state"] == "promoted"
    inbox = client.get(f"{base}/inbox").json()
    assert "decision:bq-q2" in [item["id"] for item in inbox["items"]]
    assert client.post(f"{base}/materials/problems/pb-none/promote", json={"as": "ask_me"}).status_code == 404
    bad = client.post(f"{base}/materials/problems/{problem['id']}/promote", json={"as": "bogus"})
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_argument"
    # 候选里的术语取蓝图里已经统一的叫法
    terms = [item for item in client.get(f"{base}/materials/candidates").json()["items"] if item["kind"] == "term"]
    assert [item["name"] for item in terms] == ["门店"]


def test_materials_updated_after_upload(env):
    client, _workspaces, project_id, _root, base = env
    project_events.materials_changed(project_id)  # 记下基线
    assert client.post(f"{base}/resources", files={"file": ("03-新资料.md", "合成。\n".encode())}).status_code == 201
    for _ in range(100):
        events = buffered(project_id, "materials.updated")
        if any(item["path"] == "resource/others/03-新资料.md" for event in events for item in event["data"]["items"]):
            break
        time.sleep(0.05)
    data = buffered(project_id, "materials.updated")[-1]["data"]
    assert [item["path"] for item in data["items"]] == ["resource/others/03-新资料.md"]
    assert set(data) == {"items", "read_pct", "problems_count"}


# ---- R-11、R-12 外部反馈随反馈演进包删除 ----


def test_feedback_endpoints_are_gone(env):
    client, _workspaces, _pid, _root, base = env
    assert client.get(f"{base}/feedback").status_code in (404, 405)
    assert "feedback_group" not in client.get(f"{base}/resources").json()
