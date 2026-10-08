"""评测数据源配置（modeling 三种 provider）、评测集、外部数据源编排与评测演进提示的测试。"""

import asyncio
import json
import secrets
import threading
from http.server import HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config
from app.services import eval_data_sources, eval_sets, modeling_eval
from tests.test_modeling_eval import StubModeling

# 测试密钥运行时生成，代码里不写凭据样式的字面量
FAKE_PASSWORD = secrets.token_hex(8)
FAKE_TOKEN = secrets.token_hex(8)

NL2SQL = {
    "mode": "connection",
    "provider": "nl2sql_template",
    "config": {
        "url": "https://nl2sql.example.com/",
        "token": FAKE_TOKEN,
        "scene": "mv_shadow_demo",
        "evaluation_workspace_id": "onto-eval-demo",
    },
}
NEO4J = {
    "mode": "connection",
    "provider": "neo4j",
    "config": {
        "uri": "bolt://graph.example.com:7687",
        "username": "reader",
        "password": FAKE_PASSWORD,
        "workspace_property": "d2o_workspace_id",
        "workspace_value": "graph-eval",
        "ontology_property": "d2o_ontology_id",
    },
}


# ---- 数据源配置：校验与 modeling 同口径 ----


def test_nl2sql_source_normalized_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    source = eval_data_sources.create_source("p-1", NL2SQL)
    assert source["config"]["url"] == "https://nl2sql.example.com"  # 去尾斜杠
    assert source["name"] == "NL2SQL Template · mv_shadow_demo"
    view = eval_data_sources.public_view(source)
    assert "token" not in view["config"] and view["config"]["has_token"] is True
    # 密钥落数据目录且仅属主可读，不进工作区
    stored = tmp_path / "eval-data-sources" / "p-1.json"
    assert stored.stat().st_mode & 0o777 == 0o600
    assert FAKE_TOKEN in stored.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"scene": "bad scene"}, "Scene 格式非法"),
        ({"scene": ""}, "Scene 不能为空"),
        ({"evaluation_workspace_id": "Upper"}, "评测工作空间 ID格式非法"),
        ({"url": "nl2sql.example.com/query"}, "数据服务 URL"),
    ],
)
def test_nl2sql_validation(patch, message):
    body = {**NL2SQL, "config": {**NL2SQL["config"], **patch}}
    with pytest.raises(eval_data_sources.DataSourceError, match=message):
        eval_data_sources.normalize(body)


def test_neo4j_requires_password_and_keeps_it_on_update(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    with pytest.raises(eval_data_sources.DataSourceError, match="Password"):
        eval_data_sources.normalize({**NEO4J, "config": {**NEO4J["config"], "password": ""}})
    with pytest.raises(eval_data_sources.DataSourceError, match="Workspace 对应值"):
        eval_data_sources.normalize({**NEO4J, "config": {**NEO4J["config"], "workspace_value": ""}})

    source = eval_data_sources.create_source("p-2", NEO4J)
    # 编辑时 Password 留空 = 沿用已保存的值
    updated = eval_data_sources.update_source(
        "p-2", source["id"], {**NEO4J, "name": "图库", "config": {**NEO4J["config"], "password": ""}}
    )
    assert updated["config"]["password"] == FAKE_PASSWORD
    assert updated["name"] == "图库"
    assert eval_data_sources.public_view(updated)["config"]["has_password"] is True
    # Ontology 隔离值恒等于知识包 ID，发起评测时填写
    payload = eval_data_sources.modeling_payload(updated, "pkg-id")
    assert payload["config"]["ontology_value"] == "pkg-id"
    assert payload["config"]["password"] == FAKE_PASSWORD


def test_dataset_and_none_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    with pytest.raises(eval_data_sources.DataSourceError, match="至少一份"):
        eval_data_sources.normalize({"mode": "dataset"})
    mock = eval_data_sources.create_source("p-3", {"mode": "dataset", "include_mock": True})
    assert mock["name"] == "示例数据种子"
    none = eval_data_sources.create_source("p-3", {"mode": "none"})
    assert none["name"] == "不使用数据源"
    ids = [s["id"] for s in eval_data_sources.list_sources("p-3")]
    assert set(ids) == {mock["id"], none["id"]}
    eval_data_sources.delete_source("p-3", mock["id"])
    assert eval_data_sources.get_source("p-3", mock["id"]) is None
    with pytest.raises(KeyError):
        eval_data_sources.delete_source("p-3", mock["id"])


# ---- 评测集 ----


def test_eval_set_lifecycle(tmp_path):
    root = tmp_path
    cases = [{"question": " Q1 ", "groundTruth": "G1"}, {"question": ""}, {"question": "Q2"}]
    first = eval_sets.create_set(root, "回归集", cases, source="import")
    assert first["cases"] == [
        {"question": "Q1", "groundTruth": "G1"},
        {"question": "Q2", "groundTruth": ""},
    ]
    second = eval_sets.create_set(root, "回归集", [{"question": "Q3"}])
    assert second["name"] == "回归集（2）"  # 同名自动编号
    listed = eval_sets.list_sets(root)
    assert {s["id"] for s in listed} == {first["id"], second["id"]}
    assert all("cases" not in s for s in listed)

    eval_sets.update_set(root, first["id"], cases=[{"question": "Q9"}])
    assert eval_sets.get_set(root, first["id"])["cases"] == [{"question": "Q9", "groundTruth": ""}]
    with pytest.raises(eval_sets.EvalSetError):
        eval_sets.update_set(root, first["id"], cases=[{"question": " "}])
    eval_sets.delete_set(root, second["id"])
    assert [s["id"] for s in eval_sets.list_sets(root)] == [first["id"]]


# ---- 路由：导入即存评测集、选用配置发起评测 ----


def _router(tmp_path, monkeypatch):
    from app.routers import projects
    from app.services.workspace import WorkspaceManager

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(projects, "workspaces", manager)
    meta = manager.create_project("评测预设")
    request = SimpleNamespace(state=SimpleNamespace(user=None), headers={"authorization": "Bearer t"})
    return projects, meta["id"], manager.project_root(meta["id"]), request


def test_case_import_saves_eval_set(tmp_path, monkeypatch):
    projects, pid, root, request = _router(tmp_path, monkeypatch)
    upload = SimpleNamespace(
        filename="回归用例.json",
        read=lambda: _async(json.dumps({"cases": [{"question": "Q1", "groundTruth": "G1"}]}).encode()),
    )
    data = asyncio.run(projects.eval_cases_import(pid, request, upload, True, ""))
    assert data["set"]["name"] == "回归用例" and data["set"]["count"] == 1
    assert projects.list_eval_sets(pid, request)["sets"][0]["id"] == data["set"]["id"]


def test_run_uses_saved_source_and_syncs_eval_set(tmp_path, monkeypatch):
    projects, pid, root, request = _router(tmp_path, monkeypatch)
    monkeypatch.setattr(modeling_eval, "enabled", lambda: True)
    monkeypatch.setattr(modeling_eval, "reachable", lambda: True)
    monkeypatch.setattr(config, "MODELING_TOKEN", "")
    captured = {}

    async def fake_run(project_id, root_, token, cases, data_source, eval_set_name="", allow_unbound=False):
        captured.update(cases=cases, data_source=data_source, eval_set_name=eval_set_name)

    monkeypatch.setattr(modeling_eval, "run", fake_run)
    created = projects.create_eval_data_source(pid, projects.EvalDataSourceBody(**NL2SQL), request)
    source_id = created["source"]["id"]
    listed = projects.list_eval_data_sources(pid, request)
    assert listed["sources"][0]["config"]["has_token"] is True
    assert "token" not in listed["sources"][0]["config"]
    assert listed["defaults"]["ontology_id"] == pid

    item = eval_sets.create_set(root, "冒烟", [{"question": "Q1", "groundTruth": "G1"}])
    body = projects.EvalRunBody(
        cases=[{"question": "Q1", "groundTruth": "改过的答案"}],
        data_source_id=source_id,
        eval_set_id=item["id"],
    )

    async def call():
        result = await projects.eval_run(pid, body, request)
        await asyncio.sleep(0)  # 让后台任务跑起来
        return result

    assert asyncio.run(call())["started"] is True
    assert captured["data_source"]["config"]["token"] == FAKE_TOKEN  # 密钥只在服务端拼装
    assert captured["eval_set_name"] == "冒烟"
    # 复核时对用例的修改回写评测集
    assert eval_sets.get_set(root, item["id"])["cases"][0]["groundTruth"] == "改过的答案"

    # 外部连接不能内联提交（密钥不经前端回传）
    inline = projects.EvalRunBody(cases=[{"question": "Q1"}], data_source=NL2SQL)
    assert asyncio.run(projects.eval_run(pid, inline, request)).status_code == 400
    missing = projects.EvalRunBody(cases=[{"question": "Q1"}], data_source_id="nope")
    assert asyncio.run(projects.eval_run(pid, missing, request)).status_code == 404


async def _async(value):
    return value


# ---- 编排：外部数据源 + 异常终止轮 ----


class ConnectionStub(StubModeling):
    puts: list = []
    round_payload: dict = {}
    put_status = 200

    def do_GET(self):
        if "/rounds/" in self.path:
            return self._send(200, ConnectionStub.round_payload)
        return StubModeling.do_GET(self)

    def do_PUT(self):
        length = int(self.headers.get("Content-Length") or 0)
        ConnectionStub.puts.append((self.path, json.loads(self.rfile.read(length) or b"{}")))
        if ConnectionStub.put_status != 200:
            return self._send(ConnectionStub.put_status, {"error": "数据源连接测试失败，请检查连接配置和上游服务状态"})
        return self._send(200, {"dataSource": {"mode": "connection"}})


def _serve(tmp_path, monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), ConnectionStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(config, "MODELING_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(modeling_eval, "POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(modeling_eval, "ROUND_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(modeling_eval, "_stop_requested", set())
    from app.services.workspace import WorkspaceManager

    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    meta = manager.create_project("外部数据源")
    return server, meta["id"], manager.project_root(meta["id"])


CASES = [{"question": "Q1", "groundTruth": "G1"}, {"question": "Q2", "groundTruth": "G2"}]


def test_connection_mode_puts_provider_config(tmp_path, monkeypatch):
    ConnectionStub.puts, ConnectionStub.put_status = [], 200
    ConnectionStub.round_payload = {
        "round": {"id": "r1", "status": "done", "progressDone": 2, "progressTotal": 2},
        "results": [
            {"question": "Q1", "verdict": "pass", "reason": "一致"},
            {"question": "Q2", "verdict": "pass", "reason": "一致"},
        ],
    }
    server, pid, root = _serve(tmp_path, monkeypatch)
    try:
        source = eval_data_sources.create_source(pid, {**NEO4J, "name": "图谱库"})
        modeling_eval._run_sync(pid, root, "t", CASES, source, "冒烟集")
        state = modeling_eval.read_state(root)
        assert state["status"] == "done", state
        path, payload = ConnectionStub.puts[0]
        assert path == "/api/projects/mp-1/data-source"
        assert payload["provider"] == "neo4j"
        assert payload["config"]["password"] == FAKE_PASSWORD
        assert payload["config"]["ontology_value"] == pid  # 知识包 ID
        report = (root / state["report"]).read_text(encoding="utf-8")
        assert "数据源：图谱库" in report and "评测集：冒烟集" in report
        assert FAKE_PASSWORD not in json.dumps(state, ensure_ascii=False)  # 密钥不落状态
        assert modeling_eval.evolution_hint(state) is None  # 满分无需演进
    finally:
        server.shutdown()


def test_connection_failure_surfaces_modeling_reason(tmp_path, monkeypatch):
    ConnectionStub.puts, ConnectionStub.put_status = [], 502
    server, pid, root = _serve(tmp_path, monkeypatch)
    try:
        source = eval_data_sources.create_source(pid, NL2SQL)
        modeling_eval._run_sync(pid, root, "t", CASES, source)
        state = modeling_eval.read_state(root)
        assert state["status"] == "failed"
        assert "数据源配置失败（NL2SQL Template）" in state["message"]
        assert "连接测试失败" in state["message"]
        assert FAKE_TOKEN not in state["message"]
    finally:
        ConnectionStub.put_status = 200
        server.shutdown()


def test_failed_round_with_error_cases_writes_report_and_hints_evolution(tmp_path, monkeypatch):
    """连续环境异常熔断：modeling 轮次 failed 但已有异常用例——照样出报告并提示演进。"""
    ConnectionStub.puts, ConnectionStub.put_status = [], 200
    ConnectionStub.round_payload = {
        "round": {
            "id": "r7", "status": "failed", "progressDone": 1, "progressTotal": 2,
            "log": "轮次失败：评测环境持续异常，已提前终止本轮\n",
        },
        "results": [{"question": "Q1", "verdict": "error", "reason": "运行失败：连接被拒绝"}],
    }
    server, pid, root = _serve(tmp_path, monkeypatch)
    try:
        modeling_eval._run_sync(pid, root, "t", CASES)
        state = modeling_eval.read_state(root)
        assert state["status"] == "failed"
        assert state["summary"]["error"] == 2 and state["summary"]["score"] == 0
        report = (root / state["report"]).read_text(encoding="utf-8")
        assert "轮次异常终止" in report and "本用例未执行" in report
        hint = modeling_eval.evolution_hint(state)
        assert hint == {
            "report": state["report"], "score": 0, "errors": 2, "fails": 0, "round_failed": True,
        }
        # 项目 meta 同步带出提示（聊天区「评测优化」建议据此出现）
        from app.services.workspace import WorkspaceManager

        progress = WorkspaceManager._project_progress(root)
        assert progress["eval_evolution"]["errors"] == 2
    finally:
        server.shutdown()


# ---- 演进提示判定 ----


def test_evolution_hint_rules():
    base = {"status": "done", "report": "reports/eval-1.md", "report_round": "r1"}
    hint = modeling_eval.evolution_hint
    # 高分但有异常用例：旧逻辑只看 <90 分会漏掉
    assert hint({**base, "summary": {"score": 95, "error": 1, "fail": 0}})["errors"] == 1
    assert hint({**base, "summary": {"score": 95, "error": 0, "fail": 1}}) is None
    assert hint({**base, "summary": {"score": 60, "error": 0, "fail": 4}})["score"] == 60
    # 新一轮已发起（report_round 被清空）、或新一轮在出结果前失败：旧报告不再提示
    assert hint({**base, "report_round": None, "status": "failed",
                 "summary": {"score": 10, "error": 5}}) is None
    assert hint({**base, "status": "running", "summary": {"score": 10}}) is None
    # 旧版状态文件没有 report_round：done 视为最近一轮
    legacy = {"status": "done", "report": "reports/eval-0.md", "summary": {"score": 40}}
    assert hint(legacy)["score"] == 40
