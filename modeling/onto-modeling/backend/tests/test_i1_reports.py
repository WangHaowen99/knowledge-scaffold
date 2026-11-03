"""I1：报告页（RP-01～RP-06），收录 reports/ 的检查报告、research/ 的资料分析报告与内核算的链接检查；
资料分析小结卡的「看报告」指向资料分析报告。测试数据全部为合成内容，假引擎不起真实 CLI。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.routers import sessions as session_routes
from app.services import agent_sessions, auth, streams
from app.services.agent_sessions import SessionManager
from app.services.flow import blueprint as blueprint_service
from app.services.workspace import WorkspaceManager
from tests.test_changesets import main_write
from tests.test_i1_research_view import NOTE, REPORT
from tests.test_session_host import FakeEngine

RULE_REPORT = """# 规则检查报告 · 全量

范围：全部条目

## 错误
- [ ] E-ROUTE-01 操作[[受理补货申请]]的输出没有交接对象
- [x] E-SRC-02 对象「超量清单」缺少数据来源

## 警告
- [ ] W-REF-03 规则引用了未定义的对象
"""


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    manager = SessionManager()
    monkeypatch.setattr(manager, "_engine_class_for", lambda pid: FakeEngine)
    monkeypatch.setattr(agent_sessions, "manager", manager)
    monkeypatch.setattr(session_routes, "manager", manager)
    FakeEngine.scripts, FakeEngine.sent = [], []
    streams.reset_all()
    with TestClient(app) as client:
        project_id = workspaces.create_project("报告", owner="employee_0001")["id"]
        root = workspaces.project_root(project_id)
        base = f"/api/v1/projects/{project_id}"
        assert client.post(f"{base}/resources", files={"file": ("01-补货说明.md", NOTE.encode())}).status_code == 201
        main_write(root, "reports/20261102-120000-all-rule.md", RULE_REPORT, "validate: 合成报告")
        main_write(root, "ontology/operation/受理补货申请.md", "# 受理补货申请\n\n## 逻辑描述\n\n合成。\n", "web: 合成操作")
        main_write(root, "research/20261102-合成补货.md", REPORT, "research: 合成补货")
        yield client, workspaces, project_id, root, base
    streams.reset_all()


def _sent(prefix: str) -> str:
    for _ in range(300):
        found = [text for text in FakeEngine.sent if text.startswith(prefix)]
        if found:
            return found[-1]
        time.sleep(0.01)
    raise AssertionError(FakeEngine.sent)


def test_list_detail_items_and_raw(env):
    client, _workspaces, _pid, _root, base = env
    listed = client.get(f"{base}/reports").json()
    by_id = {item["id"]: item for item in listed["items"]}
    # 工作区模板自带的 reports/index.md 是占位索引，不算报告
    assert not any(item["path"].endswith("/index.md") for item in listed["items"])
    rule = by_id["20261102-120000-all-rule"]
    assert rule == {"id": "20261102-120000-all-rule", "name": "规则检查报告 · 全量", "type": "rule", "type_label": "规则",
                    "at": rule["at"], "summary": "错误 1 · 警告 1", "path": "reports/20261102-120000-all-rule.md"}
    research = by_id["research:20261102-合成补货"]
    assert research["type"] == "research" and research["path"] == "research/20261102-合成补货.md"
    assert research["summary"] == "核验 1/4"
    assert [item["id"] for item in client.get(f"{base}/reports", params={"type": "research"}).json()["items"]] == [
        "research:20261102-合成补货"]
    detail = client.get(f"{base}/reports/20261102-120000-all-rule").json()
    assert detail["progress"] == {"handled": 1, "total": 3} and detail["mode"] == "full"
    first = detail["items"][0]
    assert first["code"] == "E-ROUTE-01" and first["level"] == "error" and first["status"] == "pending"
    assert first["text"] == "操作[[受理补货申请]]的输出没有交接对象"
    assert first["location"]["target"]["id"] == "operation:受理补货申请" and first["location"]["page"] == "studio-table"
    assert detail["items"][1]["status"] == "fixed" and detail["items"][2]["level"] == "warning"
    assert set(detail) >= {"id", "name", "type", "type_label", "at", "summary", "path", "scope_text", "mode",
                           "duration_ms", "previous", "progress", "items", "items_next_cursor", "repair",
                           "arbitration", "extras"}
    items = client.get(f"{base}/reports/20261102-120000-all-rule/items", params={"level": "error"}).json()
    assert [item["code"] for item in items["items"]] == ["E-ROUTE-01", "E-SRC-02"]
    assert items["counts"] == {"error": 2, "warning": 1, "fixed": 1}
    assert client.get(f"{base}/reports/20261102-120000-all-rule/items", params={"status": "x"}).status_code == 400
    raw = client.get(f"{base}/reports/20261102-120000-all-rule/raw").json()
    assert raw == {"path": "reports/20261102-120000-all-rule.md", "content_md": RULE_REPORT, "truncated": False}
    assert client.get(f"{base}/reports/没有").json()["code"] == "not_found"
    assert client.get(f"{base}/reports/../ontology/x").status_code == 404


def test_research_report_and_summary_card(env):
    client, _workspaces, _pid, root, base = env
    got = client.get(f"{base}/reports/research%3A20261102-合成补货").json()
    assert got["type"] == "research" and got["mode"] == "research" and got["items"] == []
    assert got["progress"] == {"handled": 1, "total": 4}
    extras = got["extras"]
    assert extras["total"] == 1 and extras["read_pct"] == 100
    assert extras["found"] == {"items": 4, "terms": 0, "rule_candidates": 1}
    assert extras["candidates"] == {"objects": 1, "rules": 1}
    assert extras["contradictions"] == {"total": 1, "resolved": 0}
    assert extras["purposes"] == [{"value": "auto", "count": 1}] and extras["verification"]["total"] == 4
    assert client.get(f"{base}/reports/research%3A20261102-合成补货/items").json()["items"] == []
    assert client.get(f"{base}/reports/research%3A20261102-合成补货/raw").json()["content_md"] == REPORT
    card = blueprint_service.research_card(root, "seed-research")
    assert card["report_id"] == "research:20261102-合成补货"
    assert card["open"] == {"kind": "page", "target": "reports", "ref": {"report": "research:20261102-合成补货"}}
    hits = client.get(f"{base}/search", params={"q": "合成补货", "groups": "reports"}).json()
    assert [item["target"]["report_id"] for item in hits["groups"][0]["items"]] == ["research:20261102-合成补货"]


def test_fix_and_recheck(env):
    client, _workspaces, _pid, _root, base = env
    fixed = client.post(f"{base}/reports/20261102-120000-all-rule/fix", json={"item_ids": ["1"]})
    assert fixed.status_code == 202, fixed.text
    assert fixed.json()["task"]["origin"]["page"] == "reports"
    text = _sent("/ontology-validate")
    assert "E-ROUTE-01" in text and "W-REF-03" not in text
    none_left = client.post(f"{base}/reports/20261102-120000-all-rule/fix", json={"item_ids": ["2"]})
    assert none_left.status_code == 400 and none_left.json()["code"] == "invalid_argument"
    link = client.post(f"{base}/reports/recheck", json={"kind": "link"})
    assert link.status_code == 200, link.text
    report = link.json()["report"]
    assert report["id"] == "link" and report["type"] == "link" and set(report["extras"]) == {"orphans", "unused", "dangling"}
    assert report["extras"]["orphans"] >= 1
    assert any(item["code"] == "LINK-ORPHAN" for item in report["items"])
    assert "link" in [item["id"] for item in client.get(f"{base}/reports").json()["items"]]
    assert client.get(f"{base}/reports/link").json()["summary"] == report["summary"]
    assert client.get(f"{base}/reports/link/raw").json()["content_md"].startswith("# 链接检查报告")
    again = client.post(f"{base}/reports/recheck", json={"kind": "link"}).json()["report"]
    assert again["previous"] == {"at": report["at"], "summary": report["summary"]}
    bad = client.post(f"{base}/reports/recheck", json={"kind": "bogus"})
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_argument"
