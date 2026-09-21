"""数据评测编排测试（方案 P2）：trace 用例转换、编排协议全链（stub modeling）、
状态与报告落盘、端点门控。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from app import config
from app.services import modeling_eval

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
        posts = [p for m, p in StubModeling.calls if m == "POST"]
        assert posts[0] == "/api/package-workspaces/import"
        assert posts[1].endswith("/testcases/batch")
        assert posts[2].endswith("/evaluate")
    finally:
        server.shutdown()


def test_run_endpoint_gated_without_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MODELING_URL", "")
    assert not modeling_eval.enabled()
    assert not modeling_eval.reachable()
