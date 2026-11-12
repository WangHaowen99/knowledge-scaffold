"""插件中心的采集类连接器（端到端验证待定事项第 9 条）。

部署了连接器网关时，多源连接器里除了转换器，还列数据库、代码仓库、
REA 这些采集类连接器（平台级、不按项目启用）；没部署网关、网关不通时不列，目录照常出。
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.evidence import collector
from app.main import app
from app.services import auth
from app.services.material_conversion import gateway
from app.services.packs import builtin
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import make_assets

PROVIDERS = [
    {"id": "db-mysql", "version": "0.1.0", "label": "MySQL", "kind": "db", "input": "connection",
     "evidence_types": ["entity", "field", "relation", "sample"], "installed": True, "self_service": False,
     "readonly_method": "只读账号 · 只读会话 · 语句白名单", "license_note": "驱动（MIT）"},
    {"id": "rea-static", "version": "0.1.0", "label": "REA 静态分析", "kind": "rea", "input": "artifact",
     "evidence_types": ["entity", "operation"], "installed": False, "self_service": True,
     "readonly_method": "静态分析，不运行目标程序", "license_note": "要确认软件授权范围"},
]


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    collector.reset()
    manager = WorkspaceManager(tmp_path / "data", make_assets(tmp_path))
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    client = TestClient(app)
    project_id = client.post("/api/v1/projects", json={"name": "连接器", "goal": "合成场景：订单建模"}).json()["id"]
    yield client, project_id, monkeypatch
    collector.reset()
    builtin.clear_cache()


def _providers(body: dict) -> dict:
    return {item["name"]: item for item in body["items"] if item["category"] == "provider"}


def test_no_gateway_lists_no_collectors(env):
    client, _pid, monkeypatch = env
    monkeypatch.delenv(gateway.URL_ENV, raising=False)
    body = client.get("/api/v1/packs").json()
    assert _providers(body) == {} and body["counts"]["provider"] == 0
    assert client.get("/api/v1/packs/db-mysql").status_code == 404


def test_gateway_collectors_are_listed_as_platform_connectors(env):
    client, project_id, monkeypatch = env
    monkeypatch.setenv(gateway.URL_ENV, "http://connector-gateway.invalid:8120")  # NOSONAR(S5332) 合成地址，不连接
    monkeypatch.setattr(collector, "catalog", lambda force=False: [dict(item) for item in PROVIDERS])
    body = client.get("/api/v1/packs", params={"project_id": project_id}).json()
    cards = _providers(body)
    assert set(cards) == {"db-mysql", "rea-static"} and body["counts"]["provider"] == 2
    mysql, rea = cards["db-mysql"], cards["rea-static"]
    assert mysql["project_status"] == "platform_installed" and mysql["converter"] is None
    assert mysql["description"] == "采集实体、字段、关系、脱敏样本"
    assert mysql["collector"] == {"kind": "db", "evidence_types": ["entity", "field", "relation", "sample"],
                                  "input": "connection", "self_service": False, "installed": True,
                                  "readonly_method": "只读账号 · 只读会话 · 语句白名单", "license_note": "驱动（MIT）"}
    # 没装的（没部署旁路容器）标未安装，平台安装仍不开放
    assert rea["project_status"] == "platform_available" and rea["collector"]["installed"] is False
    assert rea["collector"]["self_service"] is True and rea["collector"]["input"] == "artifact"
    # 包仓库里的包照旧：core 带 collector=null
    core = next(item for item in body["items"] if item["name"] == "core")
    assert core["collector"] is None
    # 只看多源连接器、按名字搜
    only = client.get("/api/v1/packs", params={"category": "provider", "q": "mysql"}).json()["items"]
    assert [item["name"] for item in only] == ["db-mysql"]
    # 详情：没有包内容、流程节点和版本记录
    detail = client.get("/api/v1/packs/rea-static").json()
    assert detail["collector"]["installed"] is False and detail["changelog"] == [] and detail["contents"] == []
    assert detail["flow_insertions"] == [] and detail["released_at"] is None


def test_unreachable_gateway_keeps_the_catalog_working(env):
    client, _pid, monkeypatch = env
    with socket.socket() as probe:  # 一个没人监听的本机端口：连接直接被拒
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv(gateway.URL_ENV, f"http://127.0.0.1:{port}")
    started = time.time()
    response = client.get("/api/v1/packs")
    assert response.status_code == 200 and time.time() - started < 5
    assert _providers(response.json()) == {}
    assert any(item["name"] == "core" for item in response.json()["items"])
