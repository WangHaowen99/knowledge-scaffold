"""数据评测编排测试（方案 P2）：trace 用例转换、编排协议全链（stub modeling）、
状态与报告落盘、端点门控。
"""

import asyncio
import json
import secrets
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config
from app.services import modeling_eval


@pytest.fixture(autouse=True)
def without_interactive_asset_links(monkeypatch):
    from app.services.workspace import WorkspaceManager

    # 本模块验证评测编排，不依赖交互式资产挂载或系统符号链接权限。
    monkeypatch.setattr(WorkspaceManager, "_mount_assets", lambda self, root: None)


TC_DOC = """# 产品匹配 — 测试用例

## TC-1：正常路径 — 双维叠加

| 项 | 值 |
|---|---|
| 场景类型 | 正常路径 |
| 输入-企业 | 合成企业A |
| 输入-信号 | 招标方=是 |
| 预期-产品 | [产品甲, 产品乙] |
| 覆盖分支 | 招标 if |

## TC-2：边界 — 无信号

| 项 | 值 |
|---|---|
| 输入-信号 | 无 |
| 预期-结论 | 不推荐任何产品 |
"""


def _bind_object_to_table(root: Path) -> str:
    """让工作区通过数据绑定门禁：上传一张真实数据表 + 一个列名对得上的对象，返回文件 ID。"""
    import io

    import openpyxl

    from app.services import eval_data_files

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "订单"
    sheet.append(["ORDER_ID", "AMOUNT"])
    sheet.append(["o1", 12])
    buffer = io.BytesIO()
    workbook.save(buffer)
    file_id = eval_data_files.save(root, "业务数据.xlsx", buffer.getvalue())["id"]
    (root / "ontology" / "object").mkdir(parents=True, exist_ok=True)
    (root / "ontology" / "object" / "订单.md").write_text(
        "# 数据来源\n订单\n\n# 对象属性清单\n"
        "| 中文名称 | 英文名称 | 主键 |\n| --- | --- | --- |\n"
        "| 订单号 | ORDER_ID | 是 |\n| 金额 | AMOUNT | 否 |\n",
        encoding="utf-8",
    )
    return file_id


def test_trace_cases_conversion(tmp_path):
    root = tmp_path
    tc = root / "ontology" / "testcase"
    tc.mkdir(parents=True)
    (tc / "产品匹配.md").write_text(TC_DOC, encoding="utf-8")
    (tc / "index.md").write_text("# 目录", encoding="utf-8")
    cases = modeling_eval.trace_cases(root)
    assert len(cases) == 2
    assert "产品匹配" in cases[0]["question"]
    assert "企业：合成企业A" in cases[0]["question"]
    assert "产品：[产品甲, 产品乙]" in cases[0]["groundTruth"]
    assert cases[1]["groundTruth"] == "结论：不推荐任何产品"


SCENE_DOC = """# 测试场景：订单审核

> 由 /ontology-trace 生成。
> 对应逻辑：[[订单审核]]

## 场景 1：金额超限

- **输入**：
  - `订单号` = `ORD-001`
  - `金额` = `50000`
- **预期路径**：异常终止于步骤2，提示金额超限
- **数据来源**：正常数据第1行

## 场景 2：缺少预期

- **输入**：
  - `订单号` = `ORD-002`
- **数据来源**：自定义
"""


def test_trace_cases_accepts_trace_scene_format(tmp_path):
    """/ontology-trace 按 testcase-template.md 写的场景段也要能转成用例，缺预期的场景跳过。"""
    tc = tmp_path / "ontology" / "testcase"
    tc.mkdir(parents=True)
    (tc / "订单审核.md").write_text(SCENE_DOC, encoding="utf-8")
    cases = modeling_eval.trace_cases(tmp_path)
    assert len(cases) == 1
    assert "订单审核" in cases[0]["question"] and "场景 1：金额超限" in cases[0]["question"]
    assert "订单号：ORD-001" in cases[0]["question"] and "金额：50000" in cases[0]["question"]
    assert cases[0]["groundTruth"] == "预期路径：异常终止于步骤2，提示金额超限"


def test_report_keeps_long_answer_in_sidecar_and_marks_summary(tmp_path):
    """报告摘要可截断，但完整 finalAnswer 必须进入可追溯附件。"""
    answer = "长回答起点：" + ("结论证据片段。" * 160)
    root = tmp_path
    (root / ".ontology-state").mkdir(parents=True)
    rel = modeling_eval._write_report(root, {"id": "round-long"}, [{
        "id": "result-long",
        "question": "Q-long",
        "groundTruth": "G-long",
        "finalAnswer": answer,
        "verdict": "fail",
        "reason": "测试",
    }])

    report = (root / rel).read_text(encoding="utf-8")
    assert "- **标准答案**：" in report
    assert "- **智能体回答**：" in report
    assert "  > - **原始回答长度**：" in report
    assert "  > - **完整回答**：[按结果 ID 查看附件]" in report
    assert "- **判分理由**：" in report
    assert "#### 元信息" not in report
    assert "#### 回答区" not in report
    assert "  > 长回答起点：" in report
    assert "原始回答长度" in report and "是否截断：是" in report
    sidecar = root / rel.replace(".md", ".answers.json")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    item = payload["answers"][0]
    assert item["resultId"] == "result-long"
    assert item["finalAnswer"] == answer
    assert item["originalLength"] == len(answer)
    assert len(item["sha256"]) == 64


def test_report_hides_full_answer_link_for_short_answer(tmp_path):
    root = tmp_path
    (root / ".ontology-state").mkdir(parents=True)
    rel = modeling_eval._write_report(root, {"id": "round-short"}, [{
        "id": "result-short",
        "question": "Q-short",
        "groundTruth": "G-short",
        "finalAnswer": "简短回答",
        "verdict": "pass",
        "reason": "一致",
    }])
    report = (root / rel).read_text(encoding="utf-8")
    assert "原始回答长度" in report and "是否截断：否" in report
    assert "完整回答" not in report
    assert (root / rel.replace(".md", ".answers.json")).is_file()


class StubModeling(BaseHTTPRequestHandler):
    """最小 modeling 评测 API stub：导入→用例→evaluate→round 详情。"""

    calls: list = []
    poll_count = 0

    def log_message(self, *args):  # 静音
        pass

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        StubModeling.calls.append(("GET", self.path))
        if self.path == "/":
            return self._send(200, {"ok": True})
        if "/rounds/" in self.path:
            StubModeling.poll_count += 1
            done = StubModeling.poll_count >= 2
            return self._send(200, {
                "round": {
                    "id": "r1",
                    "status": "done" if done else "running",
                    "progressDone": 2 if done else 1,
                    "progressTotal": 2,
                },
                "results": [
                    {"question": "Q1", "groundTruth": "G1", "finalAnswer": "A1",
                     "verdict": "pass", "reason": "一致"},
                    {"question": "Q2", "groundTruth": "G2", "finalAnswer": "A2",
                     "verdict": "fail", "reason": "结论错误"},
                ] if done else [],
            })
        return self._send(404, {})

    def do_PUT(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        StubModeling.calls.append(("PUT", self.path))
        if self.path.endswith("/data-source"):
            assert json.loads(raw) == {"mode": "none"}
            return self._send(200, {"ok": True})
        return self._send(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        StubModeling.calls.append(("POST", self.path))
        if self.path == "/api/package-workspaces/import":
            assert b"package.zip" in raw
            return self._send(201, {"project": {"id": "mp-1"}, "version": 1})
        if self.path.endswith("/testcases/batch"):
            data = json.loads(raw)
            assert len(data["cases"]) == 2
            return self._send(200, {"added": 2})
        if self.path.endswith("/evaluate"):
            return self._send(200, {"roundId": "r1"})
        return self._send(404, {})


def test_orchestration_full_protocol(tmp_path, monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), StubModeling)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(config, "MODELING_URL", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(modeling_eval, "POLL_INTERVAL_SECONDS", 0.1)

        from app.services.workspace import WorkspaceManager

        manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
        meta = manager.create_project("评测编排")
        root = manager.project_root(meta["id"])

        assert modeling_eval.reachable()
        cases = [
            {"question": "Q1", "groundTruth": "G1"},
            {"question": "Q2", "groundTruth": "G2"},
        ]
        modeling_eval._run_sync(meta["id"], root, "test-token", cases)

        state = modeling_eval.read_state(root)
        assert state["status"] == "done", state
        assert state["modeling_project"] == "mp-1"
        # 执行详情的「用时」= 终态写入时间 - 发起时间
        assert 0 < state["started_at"] <= state["ts"]
        assert state["summary"]["pass"] == 1 and state["summary"]["fail"] == 1
        assert state["summary"]["score"] == 50
        report = root / state["report"]
        content = report.read_text(encoding="utf-8")
        assert "综合得分" in content and "结论错误" in content
        # 报告已 git 提交
        import subprocess

        log = subprocess.run(["git", "log", "--oneline"], cwd=root,
                             capture_output=True, text=True).stdout
        assert "eval:" in log
        # 协议顺序：import → batch → evaluate → poll
        ordered = [(m, p) for m, p in StubModeling.calls if m in ("POST", "PUT")]
        assert ordered[0] == ("POST", "/api/package-workspaces/import")
        assert ordered[1][0] == "PUT" and ordered[1][1].endswith("/data-source")
        assert ordered[2][1].endswith("/testcases/batch")
        assert ordered[3][1].endswith("/evaluate")
    finally:
        server.shutdown()


def test_new_round_clears_previous_graph_import(tmp_path, monkeypatch):
    """上一轮做过图导入、这一轮没有：执行详情不能再显示上一轮的图导入结果。"""
    server = HTTPServer(("127.0.0.1", 0), StubModeling)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        StubModeling.calls, StubModeling.poll_count = [], 0
        monkeypatch.setattr(config, "MODELING_URL", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(modeling_eval, "POLL_INTERVAL_SECONDS", 0.05)
        from app.services.workspace import WorkspaceManager

        manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
        meta = manager.create_project("图导入残留")
        root = manager.project_root(meta["id"])
        modeling_eval._write_state(root, status="done", graph_import={"nodes": 8, "relationships": 5})
        modeling_eval._run_sync(
            meta["id"], root, "t",
            [{"question": "Q1", "groundTruth": "G1"}, {"question": "Q2", "groundTruth": "G2"}],
        )
        state = modeling_eval.read_state(root)
        assert state["status"] == "done", state
        assert state.get("graph_import") is None
    finally:
        server.shutdown()


def test_failed_round_surfaces_modeling_log(tmp_path, monkeypatch):
    class FailingStub(StubModeling):
        def do_GET(self):
            if "/rounds/" in self.path:
                return self._send(200, {
                    "round": {"id": "r2", "status": "failed",
                              "log": "轮次失败：缺少有效的 MODELING_SERVICE_TOKEN\n"},
                    "results": [],
                })
            return StubModeling.do_GET(self)

    server = HTTPServer(("127.0.0.1", 0), FailingStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(config, "MODELING_URL", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(modeling_eval, "POLL_INTERVAL_SECONDS", 0.1)
        from app.services.workspace import WorkspaceManager

        manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
        meta = manager.create_project("失败轮")
        root = manager.project_root(meta["id"])
        modeling_eval._run_sync(
            meta["id"], root, "t",
            [{"question": "Q1", "groundTruth": "G1"}, {"question": "Q2", "groundTruth": "G2"}],
        )
        state = modeling_eval.read_state(root)
        assert state["status"] == "failed"
        assert "MODELING_SERVICE_TOKEN" in state["message"]
        assert not list((root / "reports").glob("eval-*.md"))  # 失败不出报告
    finally:
        server.shutdown()


def test_run_endpoint_gated_without_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MODELING_URL", "")
    assert not modeling_eval.enabled()
    assert not modeling_eval.reachable()


def test_parse_case_json_modeling_contract():
    import pytest

    # {cases: [...]} 形式 + 字段别名
    raw = json.dumps({"cases": [
        {"question": "金额25元能配送吗", "groundTruth": "可以配送"},
        {"prompt": "金额15元呢", "ground_truth": "不可配送"},
        {"question": "无答案的问题"},
    ]}, ensure_ascii=False)
    cases = modeling_eval.parse_case_json(raw)
    assert len(cases) == 3
    assert cases[1] == {"question": "金额15元呢", "groundTruth": "不可配送"}
    assert cases[2]["groundTruth"] == ""

    # 顶层数组形式
    assert modeling_eval.parse_case_json('[{"question": "Q", "answer": "A"}]') == [
        {"question": "Q", "groundTruth": "A"}
    ]

    # 错误分支与 modeling 同语义
    with pytest.raises(ValueError, match="不是有效的 JSON"):
        modeling_eval.parse_case_json("not json")
    with pytest.raises(ValueError, match="cases 数组"):
        modeling_eval.parse_case_json('{"x": 1}')
    with pytest.raises(ValueError, match="第 2 条缺少问题"):
        modeling_eval.parse_case_json('[{"question": "Q"}, {"groundTruth": "G"}]')
    with pytest.raises(ValueError, match="重复问题：第 2 条"):
        modeling_eval.parse_case_json('[{"question": "Q  1"}, {"question": "q 1"}]')

    # 模板本身可被解析（往返）
    assert len(modeling_eval.parse_case_json(modeling_eval.case_template_json())) == 2


def test_case_progress_states():
    """逐用例状态：已出结果带判分，第一个未出结果标执行中，其余待执行。"""
    cases = [{"question": "Q1"}, {"question": "Q2"}, {"question": "Q3"}]
    results = [{"question": "Q1", "verdict": "pass", "finalAnswer": "A", "reason": "对"}]
    out = modeling_eval._case_progress(cases, results, round_running=True)
    assert [c["verdict"] for c in out] == ["pass", "running", "pending"]
    assert out[0]["answer"] == "A"
    # 轮次不在执行中（收尾/失败）：未出结果的都是待执行
    out = modeling_eval._case_progress(cases, results, round_running=False)
    assert [c["verdict"] for c in out] == ["pass", "pending", "pending"]


def test_poll_survives_401_with_token_relay(tmp_path, monkeypatch):
    """token 过期不判死整轮：401 宽限期内前端接力新 token 后续跑到终态。"""

    class AuthStub(StubModeling):
        def do_GET(self):
            if "/rounds/" in self.path:
                auth = self.headers.get("Authorization") or ""
                if auth != "Bearer fresh":
                    return self._send(401, {"error": "Unauthorized"})
                return self._send(200, {
                    "round": {"id": "r1", "status": "done",
                              "progressDone": 1, "progressTotal": 1},
                    "results": [{"question": "Q1", "groundTruth": "G1",
                                 "finalAnswer": "A1", "verdict": "pass", "reason": "一致"}],
                })
            return StubModeling.do_GET(self)

    server = HTTPServer(("127.0.0.1", 0), AuthStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(config, "MODELING_URL", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(modeling_eval, "POLL_INTERVAL_SECONDS", 0.05)
        root = tmp_path / "ws"
        (root / ".ontology-state").mkdir(parents=True)
        modeling_eval._running.add("p-relay")
        worker = threading.Thread(
            target=modeling_eval._poll_round,
            args=("p-relay", root, "stale", "mp-1", "r1", [{"question": "Q1"}]),
        )
        worker.start()
        import time as _time

        _time.sleep(0.3)  # 先让它吃几个 401 进入宽限
        state = modeling_eval.read_state(root)
        assert "等待页面接力" in state.get("message", "")
        modeling_eval.update_token("p-relay", "fresh")  # 前端轮询接力
        worker.join(timeout=10)
        assert not worker.is_alive()
        state = modeling_eval.read_state(root)
        assert state.get("status") == "done", state
        assert state["cases"][0]["verdict"] == "pass"
    finally:
        modeling_eval._running.discard("p-relay")
        modeling_eval._live_tokens.pop("p-relay", None)
        server.shutdown()


def test_resume_attaches_to_existing_round(tmp_path, monkeypatch):
    """凭据过期中断后重跑（用例集未变）：续接收取结果，不重复导入。"""

    class ResumeStub(StubModeling):
        calls: list = []

        def do_GET(self):
            ResumeStub.calls.append(("GET", self.path))
            if "/rounds/" in self.path:
                return self._send(200, {
                    "round": {"id": "r9", "status": "done",
                              "progressDone": 1, "progressTotal": 1},
                    "results": [{"question": "Q1", "groundTruth": "G1",
                                 "finalAnswer": "A1", "verdict": "pass", "reason": "一致"}],
                })
            return StubModeling.do_GET(self)

        def do_POST(self):
            ResumeStub.calls.append(("POST", self.path))
            return self._send(500, {"error": "resume 不应再发 POST"})

    server = HTTPServer(("127.0.0.1", 0), ResumeStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(config, "MODELING_URL", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(modeling_eval, "POLL_INTERVAL_SECONDS", 0.05)

        from app.services.workspace import WorkspaceManager

        manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
        meta = manager.create_project("续接验证")
        root = manager.project_root(meta["id"])
        # 上一轮中断现场：failed + 轮次坐标 + 相同用例集
        modeling_eval._write_state(
            root, status="failed", message="HTTP Error 401: Unauthorized",
            modeling_project="mp-1", round_id="r9",
            cases=[{"question": "Q1", "verdict": "running"}],
            object_fingerprint=modeling_eval.object_snapshot_fingerprint(
                modeling_eval.evaluation_object_snapshot(root),
            ),
        )
        modeling_eval._run_sync(meta["id"], root, "t", [{"question": "Q1", "groundTruth": "G1"}])
        state = modeling_eval.read_state(root)
        assert state["status"] == "done", state
        assert state["summary"]["pass"] == 1
        assert not [c for c in ResumeStub.calls if c[0] == "POST"], ResumeStub.calls
    finally:
        server.shutdown()


def test_run_sync_rechecks_relation_structure_before_resume_or_import(tmp_path, monkeypatch):
    pid, root = _eval_workspace(tmp_path, "合成关系校验")
    object_dir = root / "ontology" / "object"
    object_dir.mkdir(parents=True, exist_ok=True)
    (object_dir / "object_a.md").write_text(
        "# 关联对象\n- 当前对象 → references → [[missing_object]]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(modeling_eval, "_try_resume", lambda *args, **kwargs: pytest.fail("invalid package resumed"))
    calls = []
    monkeypatch.setattr(modeling_eval, "_http", lambda *args, **kwargs: calls.append(args))

    modeling_eval._run_sync(
        pid, root, "synthetic-token", [{"question": "synthetic question", "groundTruth": "synthetic answer"}],
        allow_unbound=True,
    )

    state = modeling_eval.read_state(root)
    assert state["status"] == "failed"
    assert state["error_code"] == "EVALUATION_PACKAGE_INVALID"
    assert "missing_object" in state["message"]
    assert calls == []


def test_run_sync_rejects_duplicates_before_resume_or_import(tmp_path, monkeypatch):
    pid, root = _eval_workspace(tmp_path, "合成重复关系校验")
    object_dir = root / "ontology" / "object"
    (object_dir / "object_a.md").write_text(
        "# 关联对象\n- 当前对象 → references → [[object_b]]\n"
        "- 当前对象名 -> references -> [[object_b|合成别名]]\n",
        encoding="utf-8",
    )
    (object_dir / "object_b.md").write_text("# 对象描述\n合成对象 B\n", encoding="utf-8")
    monkeypatch.setattr(modeling_eval, "_try_resume", lambda *args, **kwargs: pytest.fail("duplicate input resumed"))
    monkeypatch.setattr(modeling_eval, "_http", lambda *args, **kwargs: pytest.fail("duplicate input imported"))

    modeling_eval._run_sync(
        pid, root, "synthetic-token", [{"question": "synthetic question", "groundTruth": "synthetic answer"}],
        allow_unbound=True,
    )

    state = modeling_eval.read_state(root)
    assert state["status"] == "failed"
    assert state["error_code"] == "EVALUATION_PACKAGE_INVALID"
    problem = state["error_details"]["problems"][0]
    assert "重复关系" in problem
    assert "ontology/object/object_a.md 第 2 行" in problem
    assert "ontology/object/object_a.md 第 3 行" in problem


def test_changed_objects_do_not_resume_previous_round(tmp_path, monkeypatch):
    root = tmp_path
    modeling_eval._write_state(
        root, status="failed", modeling_project="project_alpha", round_id="round_demo",
        object_fingerprint=modeling_eval.object_snapshot_fingerprint({"object_a": "old synthetic document"}),
    )
    monkeypatch.setattr(modeling_eval, "_http", lambda *args, **kwargs: pytest.fail("changed objects resumed"))

    assert not modeling_eval._try_resume(
        "project_alpha", root, "synthetic-token", [],
        object_fingerprint=modeling_eval.object_snapshot_fingerprint({"object_a": "new synthetic document"}),
    )


def test_unreadable_object_is_not_silently_skipped_by_evaluation(tmp_path):
    from app.services import package_mapping

    object_dir = tmp_path / "ontology" / "object"
    object_dir.mkdir(parents=True)
    (object_dir / "object_a.md").write_bytes(b"\xff")

    with pytest.raises(modeling_eval.EvaluationPackageInvalid, match="object_a.md"):
        modeling_eval.evaluation_object_snapshot(tmp_path)
    with pytest.raises(UnicodeDecodeError):
        package_mapping.read_object_texts(tmp_path)


def test_dataset_mode_runs_quick_import(tmp_path, monkeypatch):
    """数据集模式：quick-import 代替 mode=none，导入完成后才 evaluate。"""

    class DatasetStub(StubModeling):
        calls: list = []
        poll_count = 0
        import_polls = 0

        def do_GET(self):
            DatasetStub.calls.append(("GET", self.path))
            if self.path.endswith("/evaluation-database/imports"):
                DatasetStub.import_polls += 1
                status = "done" if DatasetStub.import_polls >= 2 else "running"
                return self._send(200, {"imports": [{"id": "run1", "status": status}]})
            if "/rounds/" in self.path:
                return self._send(200, {
                    "round": {"id": "r1", "status": "done",
                              "progressDone": 1, "progressTotal": 1},
                    "results": [{"question": "Q1", "groundTruth": "G1",
                                 "finalAnswer": "A1", "verdict": "pass", "reason": "一致"}],
                })
            if self.path == "/":
                return self._send(200, {"ok": True})
            return self._send(404, {})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            DatasetStub.calls.append(("POST", self.path))
            if self.path == "/api/package-workspaces/import":
                return self._send(201, {"project": {"id": "mp-9"}, "version": 1})
            if self.path.endswith("/evaluation-database/quick-import"):
                assert b"seed.xlsx" in raw or b".xlsx" in raw
                return self._send(202, {"run": {"id": "run1"}, "database": {}})
            if self.path.endswith("/testcases/batch"):
                return self._send(200, {"added": 1})
            if self.path.endswith("/evaluate"):
                return self._send(200, {"roundId": "r1"})
            return self._send(404, {})

        def do_PUT(self):
            DatasetStub.calls.append(("PUT", self.path))
            return self._send(500, {"error": "数据集模式不应配置 mode=none"})

    server = HTTPServer(("127.0.0.1", 0), DatasetStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(config, "MODELING_URL", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(modeling_eval, "POLL_INTERVAL_SECONDS", 0.05)

        from app.services.workspace import WorkspaceManager

        manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
        meta = manager.create_project("数据集评测")
        root = manager.project_root(meta["id"])
        file_id = _bind_object_to_table(root)

        modeling_eval._run_sync(
            meta["id"], root, "t", [{"question": "Q1", "groundTruth": "G1"}],
            {"mode": "dataset", "files": [file_id]},
        )
        state = modeling_eval.read_state(root)
        assert state["status"] == "done", state
        posts = [p_ for m, p_ in DatasetStub.calls if m == "POST"]
        assert any(p_.endswith("/evaluation-database/quick-import") for p_ in posts)
        assert not [c for c in DatasetStub.calls if c[0] == "PUT"], "不应再配置 mode=none"
        # 顺序：包导入 → quick-import → evaluate
        assert posts.index("/api/package-workspaces/import") \
            < posts.index("/api/projects/mp-9/evaluation-database/quick-import") \
            < posts.index("/api/projects/mp-9/evaluate")
    finally:
        server.shutdown()


# ---- 停止评测与中断续接 ----


class CancelStub(StubModeling):
    """支持 evaluate/cancel 的 modeling 替身；轮次始终停在 running。"""

    cancels: list = []
    cancel_status = 200
    round_status = "running"

    def do_GET(self):
        StubModeling.calls.append(("GET", self.path))
        if self.path == "/":
            return self._send(200, {"ok": True})
        if "/rounds/" in self.path:
            return self._send(200, {
                "round": {"id": "r9", "status": CancelStub.round_status,
                          "progressDone": 1, "progressTotal": 2},
                "results": [{"question": "Q1", "groundTruth": "G1", "finalAnswer": "A1",
                             "verdict": "pass", "reason": "一致"}],
            })
        return self._send(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        StubModeling.calls.append(("POST", self.path))
        if self.path.endswith("/evaluate/cancel"):
            CancelStub.cancels.append((self.path, json.loads(raw or "{}")))
            if CancelStub.cancel_status != 200:
                return self._send(CancelStub.cancel_status, {"error": "该轮评测已经结束"})
            return self._send(200, {"round": {"id": "r9", "status": "failed"}})
        return self._send(404, {})


def _eval_workspace(tmp_path, name: str):
    from app.services.workspace import WorkspaceManager

    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    meta = manager.create_project(name)
    return meta["id"], manager.project_root(meta["id"])


def _serve(handler, tmp_path, monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(config, "MODELING_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(modeling_eval, "POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(modeling_eval, "ROUND_TIMEOUT_SECONDS", 5)  # 测试不等 30 分钟
    return server


def test_stop_cancels_modeling_round_and_marks_stopped(tmp_path, monkeypatch):
    """停止评测：取消 modeling 侧轮次，状态落 stopped（可重跑，不是 failed）。"""
    StubModeling.calls, CancelStub.cancels = [], []
    CancelStub.cancel_status = 200
    server = _serve(CancelStub, tmp_path, monkeypatch)
    try:
        pid, root = _eval_workspace(tmp_path, "停止评测")
        monkeypatch.setattr(modeling_eval, "_running", {pid})
        monkeypatch.setattr(modeling_eval, "_stop_requested", set())
        modeling_eval._write_state(
            root, status="running", phase="evaluate", message="评测执行中 1/2",
            modeling_project="mp-1", round_id="r9",
            cases=[{"question": "Q1", "verdict": "pass"}],
        )

        result = modeling_eval.request_stop(pid, root, "t")
        assert result["stopped"] and result["cancelled"]
        assert CancelStub.cancels == [("/api/projects/mp-1/evaluate/cancel", {"roundId": "r9"})]
        # 编排线程收到协作取消标记
        assert pid in modeling_eval._stop_requested
        state = modeling_eval.read_state(root)
        assert state["status"] == "stopped", state
        assert state["round_id"] == "r9"  # 轮次坐标保留，执行详情仍可看
    finally:
        server.shutdown()


def test_stop_tolerates_already_finished_round(tmp_path, monkeypatch):
    """轮次已结束（modeling 返回 409）：目标状态本就达成，仍收敛为 stopped。"""
    StubModeling.calls, CancelStub.cancels = [], []
    CancelStub.cancel_status = 409
    server = _serve(CancelStub, tmp_path, monkeypatch)
    try:
        pid, root = _eval_workspace(tmp_path, "停止已结束轮次")
        monkeypatch.setattr(modeling_eval, "_running", set())
        monkeypatch.setattr(modeling_eval, "_stop_requested", set())
        modeling_eval._write_state(
            root, status="interrupted", message="中断",
            modeling_project="mp-1", round_id="r9",
        )

        result = modeling_eval.request_stop(pid, root, "t")
        assert result["stopped"] and not result["cancelled"]
        assert modeling_eval.read_state(root)["status"] == "stopped"
        # 进程内无编排时不留取消标记，免得影响下一轮编排
        assert pid not in modeling_eval._stop_requested
    finally:
        server.shutdown()


def test_stop_rejected_when_no_eval_in_flight(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    pid, root = _eval_workspace(tmp_path, "无评测可停")
    modeling_eval._write_state(root, status="done", message="完成")
    with pytest.raises(ValueError):
        modeling_eval.request_stop(pid, root, "t")


def test_stop_request_aborts_orchestration_before_spending(tmp_path, monkeypatch):
    """取消标记命中：编排在发起评测前退出，不导入包、不烧模型，状态落 stopped。"""
    StubModeling.calls, CancelStub.cancels = [], []
    server = _serve(CancelStub, tmp_path, monkeypatch)
    try:
        pid, root = _eval_workspace(tmp_path, "取消标记")
        monkeypatch.setattr(modeling_eval, "_stop_requested", {pid})
        modeling_eval._run_sync(pid, root, "t", [{"question": "Q1", "groundTruth": "G1"}])
        state = modeling_eval.read_state(root)
        assert state["status"] == "stopped", state
        assert not [p for m, p in StubModeling.calls if m == "POST"], StubModeling.calls
        assert not list((root / "reports").glob("eval-*.md"))
    finally:
        server.shutdown()


def test_poll_round_exits_on_stop_without_report(tmp_path, monkeypatch):
    """轮询中被停止：编排先取消手里的轮次再抛 EvalStopped，不落报告、不覆写停止话术。

    停止请求可能早于轮次坐标落盘（如删除项目），停止方取消不到这一轮，只能靠编排兜底。
    """
    import pytest

    StubModeling.calls, CancelStub.cancels = [], []
    CancelStub.cancel_status = 200
    CancelStub.round_status = "running"
    server = _serve(CancelStub, tmp_path, monkeypatch)
    try:
        pid, root = _eval_workspace(tmp_path, "轮询中停止")
        monkeypatch.setattr(modeling_eval, "_stop_requested", {pid})
        with pytest.raises(modeling_eval.EvalStopped):
            modeling_eval._poll_round(pid, root, "t", "mp-1", "r9",
                                      [{"question": "Q1", "groundTruth": "G1"}])
        assert CancelStub.cancels == [("/api/projects/mp-1/evaluate/cancel", {"roundId": "r9"})]
        assert not list((root / "reports").glob("eval-*.md"))
    finally:
        server.shutdown()


def test_sweep_marks_orphan_running_as_interrupted(tmp_path, monkeypatch):
    """进程启动扫描：running 状态在新进程里必属孤儿，改判中断并保留轮次坐标。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    pid_a, root_a = _eval_workspace(tmp_path, "孤儿运行中")
    pid_b, root_b = _eval_workspace(tmp_path, "已完成")
    modeling_eval._write_state(root_a, status="running", message="评测执行中 3/8",
                               modeling_project="mp-1", round_id="r9")
    modeling_eval._write_state(root_b, status="done", message="完成")

    touched = modeling_eval.sweep_interrupted(tmp_path / "data")
    assert touched == [pid_a]
    state = modeling_eval.read_state(root_a)
    assert state["status"] == "interrupted"
    assert state["round_id"] == "r9" and "续接" in state["message"]
    assert modeling_eval.read_state(root_b)["status"] == "done"  # 终态不动
    # 没有工作区目录时不报错
    assert modeling_eval.sweep_interrupted(tmp_path / "nowhere") == []


def test_status_view_detects_stale_running(tmp_path, monkeypatch):
    """/eval/status 陈旧判定：内存无编排 + ts 停滞 → 中断且标记可续接。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(modeling_eval, "_running", set())
    pid, root = _eval_workspace(tmp_path, "陈旧运行中")
    modeling_eval._write_state(root, status="running", message="评测执行中 3/8",
                               modeling_project="mp-1", round_id="r9")

    # 刚刷过 ts：仍按运行中呈现，不误判
    assert modeling_eval.status_view(pid, root)["status"] == "running"

    path = root / modeling_eval.STATE_REL
    stale = json.loads(path.read_text(encoding="utf-8"))
    stale["ts"] = stale["ts"] - modeling_eval.STALE_AFTER_SECONDS - 1
    path.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")

    view = modeling_eval.status_view(pid, root)
    assert view["status"] == "interrupted" and view["resumable"] is True
    assert modeling_eval.read_state(root)["status"] == "interrupted"  # 已持久化

    # 进程内仍有编排时，陈旧也不改判（长耗时阶段不写状态）
    modeling_eval._write_state(root, status="running")
    stale = json.loads(path.read_text(encoding="utf-8"))
    stale["ts"] = stale["ts"] - modeling_eval.STALE_AFTER_SECONDS - 1
    path.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(modeling_eval, "_running", {pid})
    assert modeling_eval.status_view(pid, root)["status"] == "running"


def test_resume_collects_interrupted_round(tmp_path, monkeypatch):
    """续接中断轮次：只 GET 已有轮次并落报告，不再发起评测。"""
    StubModeling.calls = []
    CancelStub.round_status = "done"
    server = _serve(CancelStub, tmp_path, monkeypatch)
    try:
        pid, root = _eval_workspace(tmp_path, "续接中断轮")
        monkeypatch.setattr(modeling_eval, "_running", set())
        monkeypatch.setattr(modeling_eval, "_stop_requested", set())
        modeling_eval._write_state(
            root, status="interrupted", message=modeling_eval.INTERRUPTED_MESSAGE,
            modeling_project="mp-1", round_id="r9",
            cases=[{"question": "Q1", "verdict": "running"}],
        )
        assert modeling_eval.can_resume(root)

        modeling_eval._resume_sync(pid, root, "t")
        state = modeling_eval.read_state(root)
        assert state["status"] == "done", state
        assert state["summary"]["pass"] == 1
        assert not [p for m, p in StubModeling.calls if m == "POST"], StubModeling.calls
    finally:
        CancelStub.round_status = "running"
        server.shutdown()


class EmptyFailedStub(CancelStub):
    """轮次已失败且一条结果都没有（如启动即失败）。"""

    def do_GET(self):
        StubModeling.calls.append(("GET", self.path))
        if "/rounds/" in self.path:
            return self._send(200, {
                "round": {"id": "r9", "status": "failed", "log": "轮次失败：启动即失败\n"},
                "results": [],
            })
        return CancelStub.do_GET(self)


def test_resume_reports_unresumable_round(tmp_path, monkeypatch):
    """modeling 侧轮次已失败且没有任何结果：续接失败，状态留在中断并提示重跑。"""
    StubModeling.calls = []
    server = _serve(EmptyFailedStub, tmp_path, monkeypatch)
    try:
        pid, root = _eval_workspace(tmp_path, "无法续接")
        monkeypatch.setattr(modeling_eval, "_stop_requested", set())
        modeling_eval._write_state(
            root, status="interrupted", message="中断",
            modeling_project="mp-1", round_id="r9",
            cases=[{"question": "Q1", "verdict": "running"}],
        )
        modeling_eval._resume_sync(pid, root, "t")
        state = modeling_eval.read_state(root)
        assert state["status"] == "interrupted"
        assert "无法续接" in state["message"]
        assert not list((root / "reports").glob("eval-*.md"))
    finally:
        server.shutdown()


def test_resume_collects_failed_round_partial_results(tmp_path, monkeypatch):
    """modeling 侧轮次异常终止但已有部分结果：续接收取成报告。

    状态落 failed，未执行的用例计为异常，且不能再次续接。
    """
    StubModeling.calls = []
    CancelStub.round_status = "failed"
    server = _serve(CancelStub, tmp_path, monkeypatch)
    try:
        pid, root = _eval_workspace(tmp_path, "异常终止续接")
        monkeypatch.setattr(modeling_eval, "_stop_requested", set())
        modeling_eval._write_state(
            root, status="interrupted", message="中断",
            modeling_project="mp-1", round_id="r9",
            cases=[{"question": "Q1", "verdict": "pass"}, {"question": "Q2", "verdict": "running"}],
        )
        modeling_eval._resume_sync(pid, root, "t")
        state = modeling_eval.read_state(root)
        assert state["status"] == "failed", state
        assert state["summary"]["pass"] == 1 and state["summary"]["error"] == 1
        assert "报告已生成" in state["message"]
        assert "未执行" in (root / state["report"]).read_text(encoding="utf-8")
        assert not modeling_eval.can_resume(root)
        assert modeling_eval.evolution_hint(state)["errors"] == 1
    finally:
        CancelStub.round_status = "running"
        server.shutdown()


def test_can_resume_requires_round_coordinates(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    _pid, root = _eval_workspace(tmp_path, "无轮次坐标")
    modeling_eval._write_state(root, status="interrupted", message="中断")
    assert not modeling_eval.can_resume(root)
    modeling_eval._write_state(root, modeling_project="mp-1", round_id="r9")
    assert modeling_eval.can_resume(root)


def test_write_state_does_not_recreate_deleted_workspace(tmp_path):
    """项目已删除（工作区已移除）后编排线程才收尾：不得在原位置重建出状态文件残壳。"""
    root = tmp_path / "gone"
    modeling_eval._write_state(root, status="stopped", message="停止")
    assert not root.exists()


def test_write_report_does_not_recreate_deleted_workspace(tmp_path):
    root = tmp_path / "gone"
    with pytest.raises(FileNotFoundError):
        modeling_eval._write_report(root, {"id": "r1"}, [])
    assert not root.exists()


# ---- 评测凭据续期：refresh token 透传给 modeling 的 /evaluate ----

SYNTHETIC_REFRESH = "synthetic-refresh-value"


def _fake_modeling_http(calls: list, traces: dict | None = None):
    """替身 _http：记录每次调用（方法、路径、请求体），按路径回协议响应。"""

    def fake_http(method, path, token, body=None, data=None, content_type="", timeout=60.0):
        calls.append((method, path, body))
        if path == "/api/package-workspaces/import":
            return {"project": {"id": "mp-1"}, "version": 1}
        if path.endswith("/evaluate"):
            return {"roundId": "r1"}
        if "/rounds/" in path:
            return {
                "round": {"id": "r1", "status": "done", "progressDone": 1, "progressTotal": 1},
                "results": [{"id": "er-1", "question": "Q1", "groundTruth": "G1",
                             "finalAnswer": "", "verdict": "error",
                             "reason": "运行失败：Tool execution stopped after a repeated failure."}],
                "traces": traces or {},
            }
        return {}

    return fake_http


@pytest.mark.parametrize("refresh_token", [SYNTHETIC_REFRESH, ""])
def test_evaluate_start_passes_refresh_token_only_in_request(tmp_path, monkeypatch, refresh_token):
    """评测轮常超 access token 有效期：/evaluate 请求体带上 refresh token 让 modeling 续期。

    无鉴权部署没有 refresh token 时仍发空 body。refresh token 不得落进工作区任何文件。
    """
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(modeling_eval, "POLL_INTERVAL_SECONDS", 0.01)
    calls: list = []
    monkeypatch.setattr(modeling_eval, "_http", _fake_modeling_http(calls))
    pid, root = _eval_workspace(tmp_path, "凭据续期")

    modeling_eval._run_sync(
        pid, root, "access", [{"question": "Q1", "groundTruth": "G1"}],
        allow_unbound=True, refresh_token=refresh_token,
    )

    evaluate = [body for method, path, body in calls if path.endswith("/evaluate")]
    assert evaluate == [{"refresh_token": refresh_token} if refresh_token else {}]
    # 其余调用都不携带 refresh token
    assert all(
        SYNTHETIC_REFRESH not in json.dumps(body or {})
        for method, path, body in calls if not path.endswith("/evaluate")
    )
    assert modeling_eval.read_state(root)["status"] == "done"
    for path in root.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            assert SYNTHETIC_REFRESH not in path.read_text(encoding="utf-8", errors="ignore"), path


def test_eval_router_forwards_refresh_token_only_for_user_token(tmp_path, monkeypatch):
    from app.routers import projects
    from app.services.workspace import WorkspaceManager

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(projects, "workspaces", manager)
    monkeypatch.setattr(modeling_eval, "enabled", lambda: True)
    monkeypatch.setattr(modeling_eval, "reachable", lambda: True)
    pid = manager.create_project("凭据转交")["id"]
    request = SimpleNamespace(state=SimpleNamespace(user=None), headers={"authorization": "Bearer t"})
    captured: list = []

    async def fake_run(project_id, root_, token, cases, data_source, eval_set_name="",
                       allow_unbound=False, refresh_token=""):
        captured.append((token, refresh_token))

    monkeypatch.setattr(modeling_eval, "run", fake_run)
    body = projects.EvalRunBody(
        cases=[{"question": "Q1", "groundTruth": "G1"}], allow_unbound=True,
        refresh_token=SYNTHETIC_REFRESH,
    )
    assert SYNTHETIC_REFRESH not in repr(body)  # 请求体模型的 repr 不暴露 refresh token

    async def call():
        result = await projects.eval_run(pid, body, request)
        await asyncio.sleep(0)
        return result

    monkeypatch.setattr(config, "MODELING_TOKEN", "")
    assert asyncio.run(call())["started"] is True
    # 部署级兜底令牌不属于当前用户：不转交用户的 refresh token
    monkeypatch.setattr(config, "MODELING_TOKEN", "deploy-token")
    assert asyncio.run(call())["started"] is True
    assert captured == [("t", SYNTHETIC_REFRESH), ("deploy-token", "")]


# ---- 报告「工具失败」小节：供 /ontology-evolution 判断运行异常根因 ----

VALIDATION_ERROR = json.dumps({
    "ok": False,
    "error": {
        "code": "SCRIPT_ARGUMENT_VALIDATION_FAILED",
        "message": "Script arguments failed validation; the script did not run.",
        "correctable": True,
        "issues": [{"code": "ungrounded_identity", "path": "arguments.customer_code"}],
    },
})


def test_report_lists_tool_failures_from_round_traces(tmp_path, monkeypatch):
    """异常用例报告只有「运行失败」一句话不够归因：从轮次详情 traces 附上工具失败摘要。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(modeling_eval, "POLL_INTERVAL_SECONDS", 0.01)
    calls: list = []
    traces = {"er-1": {
        "status": "complete",
        "toolCalls": ["business_data_search", "execute_script", "execute_script"],
        "toolFailures": [
            {"toolName": "execute_script", "error": VALIDATION_ERROR},
            {"toolName": "execute_script", "error": VALIDATION_ERROR},
        ],
    }}
    monkeypatch.setattr(modeling_eval, "_http", _fake_modeling_http(calls, traces))
    pid, root = _eval_workspace(tmp_path, "工具失败")

    modeling_eval._run_sync(pid, root, "t", [{"question": "Q1", "groundTruth": "G1"}],
                            allow_unbound=True)

    report = (root / modeling_eval.read_state(root)["report"]).read_text(encoding="utf-8")
    assert "- **工具失败**：" in report
    assert (
        "  - `execute_script` · `SCRIPT_ARGUMENT_VALIDATION_FAILED` · "
        "问题：`ungrounded_identity arguments.customer_code` · "
        "详情：Script arguments failed validation; the script did not run.（×2）"
    ) in report
    assert "  - 本用例共调用工具 3 次，失败 2 次" in report


def test_tool_failure_section_handles_text_errors_and_caps(tmp_path):
    root = tmp_path
    (root / ".ontology-state").mkdir(parents=True)
    truncated_json = VALIDATION_ERROR[:-3]  # consumer 端把 error 截到 1KB，JSON 可能不完整
    many = [{"toolName": f"tool_{i}", "error": f"失败 {i}"} for i in range(8)]
    # 测试密钥运行时生成，代码里不写凭据样式的字面量
    leaked = secrets.token_hex(8)
    rel = modeling_eval._write_report(root, {"id": "r1"}, [
        {"id": "er-script", "question": "Q1", "verdict": "error", "reason": "运行失败"},
        {"id": "er-many", "question": "Q2", "verdict": "fail", "reason": "错"},
        {"id": "er-pass", "question": "Q3", "verdict": "pass", "reason": "对"},
        {"id": "er-none", "question": "Q4", "verdict": "error", "reason": "运行失败"},
        {"id": "er-lost", "question": "Q5", "verdict": "error", "reason": "运行失败"},
    ], traces={
        "er-script": {"status": "complete", "toolCalls": [], "toolFailures": [
            {"toolName": "execute_script",
             "error": f"Error: Script exited with code 2. KeyError: CUSTOMER_CODE token={leaked} "
                      + "x" * 400},
            {"toolName": "execute_script", "error": truncated_json},
        ]},
        "er-many": {"status": "complete", "toolCalls": [], "toolFailures": many},
        "er-pass": {"status": "complete", "toolCalls": [], "toolFailures": many},
        "er-lost": {"status": "unavailable", "error": "Consumer 未返回 run_id", "toolCalls": []},
    })
    report = (root / rel).read_text(encoding="utf-8")
    sections = report.split("### ")
    script, many_case, pass_case, none_case, lost_case = sections[1:]

    assert "`execute_script` · 退出码 2 · 详情：Error: Script exited with code 2." in script
    assert "CUSTOMER_CODE" in script and "`CUSTOMER_CODE`" not in script  # 列名不当错误码
    assert leaked not in script and "token=***" in script
    assert "x" * (modeling_eval.TOOL_FAILURE_DETAIL_CHARS + 1) not in script
    # 截断的 JSON 仍能抽出错误码与问题路径
    assert "`SCRIPT_ARGUMENT_VALIDATION_FAILED`" in script
    assert "`ungrounded_identity arguments.customer_code`" in script
    assert "详情：Script arguments failed validation; the script did not run." in script

    assert many_case.count("  - `tool_") == modeling_eval.TOOL_FAILURES_PER_CASE
    assert "另有 3 类失败未列出" in many_case
    assert "工具失败" not in pass_case  # 通过用例不附失败摘要
    assert "工具失败" not in none_case  # 没有轨迹的用例不出空小节
    assert "执行轨迹未取到（Consumer 未返回 run_id）" in lost_case


def test_evolution_command_reads_tool_failure_section():
    """报告小节名与 /ontology-evolution 的归因指引必须对得上，否则命令读不到工具失败证据。"""
    command = (
        Path(config.ASSETS_DIR) / "workspace-toolkit" / "commands" / "ontology-evolution.md"
    ).read_text(encoding="utf-8")
    assert "「工具失败」" in command
    assert "SCRIPT_ARGUMENT_VALIDATION_FAILED" in command
    assert "执行轨迹未取到" in command


@pytest.fixture()
def eval_pool(tmp_path, monkeypatch):
    """评测专用线程池：容量 2，编排主体换成可控阻塞的假实现。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "EVAL_MAX_CONCURRENT_ROUNDS", 2)
    monkeypatch.setattr(modeling_eval, "_executor", None)
    monkeypatch.setattr(modeling_eval, "_slots", weakref.WeakKeyDictionary())
    monkeypatch.setattr(modeling_eval, "_queued", {})
    monkeypatch.setattr(modeling_eval, "_running", set())
    monkeypatch.setattr(modeling_eval, "_stop_requested", set())
    release = threading.Event()
    started: list[str] = []

    def fake_run_sync(project_id, root, token, cases, *rest):
        started.append(project_id)
        release.wait(timeout=10)
        modeling_eval._write_state(root, status="done", message="完成")

    monkeypatch.setattr(modeling_eval, "_run_sync", fake_run_sync)
    yield release, started
    release.set()
    if modeling_eval._executor is not None:
        modeling_eval._executor.shutdown(wait=True)


async def _wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("条件未在时限内满足")
        await asyncio.sleep(0.01)


def test_eval_rounds_do_not_starve_default_executor(tmp_path, eval_pool):
    """评测占满专用池时，默认线程池上的短任务（上传、git）照常即时完成。"""
    release, started = eval_pool
    projects = [_eval_workspace(tmp_path, f"评测{i}") for i in range(3)]

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        tasks = [
            asyncio.create_task(modeling_eval.run(pid, root, "t", [{"question": "Q", "groundTruth": "G"}]))
            for pid, root in projects
        ]
        await _wait_for(lambda: len(started) == 2)
        # 第 3 轮排队，状态视图显示位次，而状态文件不被改写
        third_pid, third_root = projects[2]
        await _wait_for(lambda: modeling_eval.queue_position(third_pid) == 1)
        view = modeling_eval.status_view(third_pid, third_root)
        assert view["status"] == "running" and view["phase"] == "queued", view
        assert view["queue_position"] == 1
        assert modeling_eval.read_state(third_root) == {}
        # 评测线程全占着，默认线程池仍可立即执行短任务
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "ok"), timeout=1) == "ok"
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)

    asyncio.run(scenario())
    assert sorted(started) == sorted(pid for pid, _ in projects)
    assert modeling_eval._queued == {}
    for _pid, root in projects:
        assert modeling_eval.read_state(root)["status"] == "done"


def test_stop_while_queued_skips_orchestration(tmp_path, eval_pool):
    """排队中停止：立即落 stopped，轮到时不再执行编排（不产生 modeling 侧开销）。"""
    release, started = eval_pool
    projects = [_eval_workspace(tmp_path, f"排队{i}") for i in range(3)]

    async def scenario():
        tasks = [
            asyncio.create_task(modeling_eval.run(pid, root, "t", [{"question": "Q", "groundTruth": "G"}]))
            for pid, root in projects
        ]
        third_pid, third_root = projects[2]
        await _wait_for(lambda: modeling_eval.queue_position(third_pid) == 1)
        result = modeling_eval.request_stop(third_pid, third_root, "t")
        assert result["stopped"] and not result["cancelled"]
        assert modeling_eval.read_state(third_root)["status"] == "stopped"
        # 停止后立即出队、释放项目锁：状态视图不再显示排队，可马上重新运行
        await _wait_for(lambda: third_pid not in modeling_eval._running)
        assert modeling_eval.queue_position(third_pid) is None
        assert modeling_eval.status_view(third_pid, third_root)["status"] == "stopped"
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)

    asyncio.run(scenario())
    assert projects[2][0] not in started
    assert modeling_eval.read_state(projects[2][1])["status"] == "stopped"


def test_delete_while_queued_dequeues_round(tmp_path, eval_pool):
    """项目在评测排队期间被删除：排队的轮次立即出队，不再等槽位也不执行编排。"""
    release, started = eval_pool
    projects = [_eval_workspace(tmp_path, f"删除{i}") for i in range(3)]

    async def scenario():
        tasks = [
            asyncio.create_task(modeling_eval.run(pid, root, "t", [{"question": "Q", "groundTruth": "G"}]))
            for pid, root in projects
        ]
        third_pid, third_root = projects[2]
        await _wait_for(lambda: modeling_eval.queue_position(third_pid) == 1)
        assert modeling_eval.abort_for_delete(third_pid, third_root, "t") is None
        assert modeling_eval.queue_position(third_pid) is None
        await _wait_for(lambda: third_pid not in modeling_eval._running)
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)

    asyncio.run(scenario())
    assert projects[2][0] not in started
