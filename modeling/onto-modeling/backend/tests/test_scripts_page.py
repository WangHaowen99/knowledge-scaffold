"""脚本页接口（契约 SC-01～SC-03）与代码生成包登记的只读产物（扫描报告）。

用仓库里真实的资产（core 与代码生成包）；脚本、扫描报告全部是合成内容。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.services import auth
from app.services.packs import builtin
from app.services.workspace import WorkspaceManager

SCRIPT = '''"""
功能：补货量测算（合成示例）
绑定 operation：[[安全库存测算]]
输入
- daily_sales  number  日均销量
输出
- qty  integer  建议补货量
"""
import argparse
import json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--daily_sales", type=float, required=True)
    args = parser.parse_args()
    print(json.dumps({"qty": int(args.daily_sales * 3)}))


if __name__ == "__main__":
    main()
'''

SCAN = """# 脚本化候选扫描（合成）

共扫描 3 个操作。

## ✅ 适合写成脚本

| 编号 | 操作 | 覆盖 | 类型 | 输入 | 输出 | 说明 |
|---|---|---|---|---|---|---|
| 1 | 安全库存测算 | 全部 | 计算 | `daily_sales` | `qty` | 纯公式 |

## ❌ 不适合

| 操作 | 原因 |
|---|---|
| 区域审批 | 需要人工判断 |
"""


def test_scripts_page_lists_candidates_contract_and_sandbox(tmp_path, monkeypatch):
    builtin.clear_cache()
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    from app.services import sandbox_status

    monkeypatch.setattr(sandbox_status, "status", lambda: {"connected": False, "python": None, "detail": "未连接"})
    project_id = manager.create_project("脚本")["id"]
    root = manager.project_root(project_id)
    (root / "ontology/scripts/restock_qty.py").write_text(SCRIPT, encoding="utf-8")
    (root / "reports/20260101-000000-codegen-candidates.md").write_text(SCAN, encoding="utf-8")
    client = TestClient(app)
    body = client.get(f"/api/v1/projects/{project_id}/scripts").json()
    assert body["sandbox"]["available"] is False and body["sandbox"]["network"] == "denied"
    assert [item["operation"]["key"] for item in body["candidates"]["items"]] == ["安全库存测算"]
    assert body["candidates"]["items"][0]["suitable"] == "yes" and body["candidates"]["scanned_at"]
    assert body["candidates"]["unsuitable"] == [{"operation": {"kind": "operation", "id": "operation:区域审批",
                                                               "key": "区域审批", "name": "区域审批"},
                                                 "reason": "需要人工判断"}]
    row = next(item for item in body["items"] if item["script"]["key"] == "restock_qty")
    assert row["contract"]["status"] in ("passed", "failed") and row["descriptor"] in ("none", "stale", "synced")
    assert row["sandbox"]["status"] == "not_run" and "regenerate" in row["actions"]
    verification = client.get(f"/api/v1/projects/{project_id}/scripts/restock_qty/verification").json()
    assert verification["status"] == "not_run" and verification["network_access"] is False
    assert client.get(f"/api/v1/projects/{project_id}/scripts/none/verification").status_code == 404
    summary = client.get("/api/v1/sandbox/status").json()
    assert summary["available"] is False and summary["checked_at"]
    builtin.clear_cache()


def test_codegen_artifacts_are_registered_read_only(tmp_path, monkeypatch):
    builtin.clear_cache()
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    project_id = manager.create_project("扫描报告")["id"]
    root = manager.project_root(project_id)
    (root / "reports/20260101-000000-codegen-candidates.md").write_text(SCAN, encoding="utf-8")
    client = TestClient(app)
    rows = client.get(f"/api/v1/projects/{project_id}/artifacts/codegen.scan_report").json()["items"]
    assert rows[0]["key"] == "20260101-000000" and rows[0]["title"] == "脚本化候选扫描（合成）"
    detail = client.get(f"/api/v1/projects/{project_id}/artifacts/codegen.scan_report/20260101-000000").json()
    assert detail["content"]["candidates"][0]["operation"] == "安全库存测算"
    readonly = client.put(f"/api/v1/projects/{project_id}/artifacts/codegen.scan_report/20260101-000000",
                          json={"base_revision": detail["revision"], "fields": {"title": "x"}})
    assert readonly.status_code == 403 and readonly.json()["details"]["reason"] == "report"
    assert json.loads(json.dumps(detail))["format"] == "md-report"
    builtin.clear_cache()
