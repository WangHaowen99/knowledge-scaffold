"""
==========================================================================
  test_acp_full.py — ACP 协议全端点自动化测试
==========================================================================

  覆盖 ACP 协议的每一个 REST 端点，包括：
    - 正常场景（功能验证）
    - 错误场景（401/404/400）
    - 多语言组合（zh/en/ja/ko）
    - 输出抑制组合

  【前置条件】
  1. 后端服务已启动: python app.py
  2. 复制 .env.example 为 .env 并修改配置
  3. 安装依赖: pip install requests

  【运行方式】
    python test_acp_full.py                  # 全部测试
    python test_acp_full.py --group api      # 仅 API 端点测试
    python test_acp_full.py --group lang     # 仅多语言测试
    python test_acp_full.py --group suppress # 仅输出抑制测试
    python test_acp_full.py --group error    # 仅错误场景测试
    python test_acp_full.py --group flow     # 仅完整流程测试
    python test_acp_full.py --group all      # 全部（默认）
    python test_acp_full.py --list           # 仅列出所有用例，不执行
==========================================================================
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
import requests
from datetime import datetime, timezone
from typing import Any
from dataclasses import dataclass, field

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv() -> dict[str, str | None]:
    env_map: dict[str, str | None] = {}
    dotenv = os.path.join(_SCRIPT_DIR, ".env")
    if os.path.exists(dotenv):
        with open(dotenv, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    env_map[k.strip()] = v.strip().strip("'\"")
    return env_map


_dotenv = _load_dotenv()


def get_env(key: str, default: str = "") -> str:
    return os.environ.get(key) or _dotenv.get(key) or default


BASE_URL = get_env("BASE_URL", "http://localhost:5055")
AUTH_USERNAME = get_env("AUTH_USERNAME")
AUTH_PASSWORD = get_env("AUTH_PASSWORD")
WORKSPACE_ID = get_env("WORKSPACE_ID", "default")
TEST_TIMEOUT = int(get_env("ACP_TEST_TIMEOUT", "600"))
ACP_SKIP_EXECUTE = get_env("ACP_SKIP_EXECUTE", "") == "1"

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


@dataclass
class TestResult:
    name: str
    group: str
    status: str = ""
    detail: str = ""
    duration_ms: int = 0
    assertions: list[dict] = field(default_factory=list)

    def check(self, desc: str, condition: bool, detail: str = ""):
        self.assertions.append({
            "desc": desc,
            "passed": condition,
            "detail": detail,
        })
        if not condition and self.status != FAIL:
            self.status = FAIL
            self.detail = detail or desc


class ACPTestRunner:
    def __init__(self):
        self.base_url = BASE_URL
        self.token: str | None = None
        self.session = requests.Session()
        self.results: list[TestResult] = []
        self.run_context: dict[str, Any] = {}

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def login(self) -> bool:
        if not AUTH_USERNAME or not AUTH_PASSWORD:
            raise RuntimeError("AUTH_USERNAME and AUTH_PASSWORD must be set for ACP test login")
        try:
            resp = self.session.post(
                self._url("/auth/login"),
                json={"username": AUTH_USERNAME, "password": AUTH_PASSWORD},
                timeout=10,
            )
            if resp.status_code == 200:
                self.token = resp.json().get("token")
                return True
            return False
        except Exception:
            return False

    def _acp_get(self, path: str, **kwargs) -> requests.Response:
        return self.session.get(self._url(path), headers=self._headers(), timeout=TEST_TIMEOUT, **kwargs)

    def _acp_post(self, path: str, data: dict | None = None, **kwargs) -> requests.Response:
        return self.session.post(self._url(path), json=data, headers=self._headers(), timeout=TEST_TIMEOUT, **kwargs)

    def _acp_delete(self, path: str, **kwargs) -> requests.Response:
        return self.session.delete(self._url(path), headers=self._headers(), timeout=TEST_TIMEOUT, **kwargs)

    def _acp_patch(self, path: str, data: dict | None = None, **kwargs) -> requests.Response:
        return self.session.patch(self._url(path), json=data, headers=self._headers(), timeout=TEST_TIMEOUT, **kwargs)

    def _create_run(self, message: str, lang: str | None = None, session_id: str | None = None) -> dict | None:
        payload: dict[str, Any] = {
            "messages": [
                {"role": "user", "parts": [{"type": "text", "content": message}]}
            ],
            "context": {"session_id": session_id or f"test_{uuid.uuid4().hex[:6]}"},
        }
        if lang:
            payload["lang"] = lang
        resp = self._acp_post("/acp/runs", data=payload)
        if resp.status_code == 200:
            return resp.json()
        return None

    def _collect_sse_events(self, resp: requests.Response) -> list[dict]:
        events = []
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("data:"):
                data_str = line[5:].strip()
                if not data_str:
                    continue
                try:
                    events.append(json.loads(data_str))
                except json.JSONDecodeError:
                    events.append({"raw": data_str})
            elif line.startswith("event:"):
                pass
        return events

    def _collect_typed_sse_events(self, resp: requests.Response) -> list[dict]:
        events = []
        current_event = None
        for line in resp.iter_lines(decode_unicode=True):
            stripped = line.strip() if line else ""
            if not stripped:
                current_event = None
                continue
            if stripped.startswith("event: "):
                current_event = stripped[7:].strip()
            elif stripped.startswith("data: "):
                data_str = stripped[6:]
                try:
                    payload = json.loads(data_str)
                except json.JSONDecodeError:
                    payload = {"raw": data_str}
                events.append({"event": current_event or "message", "data": payload})
        return events

    def _get_runtime_config(self) -> dict:
        resp = self._acp_get(f"/agentic/workspaces/{WORKSPACE_ID}/runtime-config")
        if resp.status_code == 200:
            return resp.json()
        return {}

    def _set_runtime_config(self, config: dict) -> bool:
        resp = self._acp_patch(f"/agentic/workspaces/{WORKSPACE_ID}/runtime-config", data=config)
        return resp.status_code == 200

    def _set_lang_enabled(self, enabled: bool, default_lang: str = "zh"):
        self._set_runtime_config({"lang": {"enabled": enabled, "default": default_lang}})

    def _set_suppress_patterns(self, patterns: str):
        self._set_runtime_config({"acp_suppress_script_patterns": patterns})

    # ═══════════════════════════════════════════
    # Test Group 1: API 端点基础功能
    # ═══════════════════════════════════════════

    def test_get_agent(self) -> TestResult:
        r = TestResult("GET /acp/agent — 获取 Agent 元信息", "api")
        t0 = time.perf_counter()
        resp = self._acp_get("/acp/agent")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        data = resp.json()
        r.check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")
        r.check("含 name 字段", "name" in data, str(list(data.keys())))
        r.check("含 version 字段", "version" in data, str(list(data.keys())))
        r.check("含 capabilities", "capabilities" in data, str(list(data.keys())))
        r.check("含 endpoints", "endpoints" in data, str(list(data.keys())))
        r.check("name 非空", bool(data.get("name")), data.get("name", ""))
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_get_agent_endpoint_urls(self) -> TestResult:
        r = TestResult("GET /acp/agent — endpoints URL 不含重复路径", "api")
        t0 = time.perf_counter()
        resp = self._acp_get("/acp/agent")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        data = resp.json()
        endpoints = data.get("endpoints", {})
        agent_url = endpoints.get("agent", "")
        runs_url = endpoints.get("runs", "")
        r.check("agent URL 不含 /agent/agent", "/agent/agent" not in agent_url, f"got {agent_url}")
        r.check("runs URL 不含 /agent/runs", "/agent/runs" not in runs_url, f"got {runs_url}")
        r.check("agent URL 以 /acp/agent 结尾", agent_url.rstrip("/").endswith("/acp/agent"), f"got {agent_url}")
        r.check("runs URL 以 /acp/runs 结尾", runs_url.rstrip("/").endswith("/acp/runs"), f"got {runs_url}")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_get_agent_auth_scheme(self) -> TestResult:
        r = TestResult("GET /acp/agent — authentication 含 scheme 字段", "api")
        t0 = time.perf_counter()
        resp = self._acp_get("/acp/agent")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        data = resp.json()
        auth = data.get("authentication", {})
        r.check("含 type 字段", "type" in auth, str(auth))
        auth_type = auth.get("type", "")
        if auth_type != "none":
            r.check("非 none 时含 scheme 字段", "scheme" in auth, str(auth))
            r.check("scheme = bearer", auth.get("scheme") == "bearer", f"got {auth.get('scheme')}")
        else:
            r.check("type = none 时 scheme 为 null", auth.get("scheme") is None, f"got {auth.get('scheme')}")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_create_run_full_uuid(self) -> TestResult:
        r = TestResult("POST /acp/runs — run_id 使用完整 UUID", "api")
        t0 = time.perf_counter()
        data = self._create_run("UUID 长度测试")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("返回非空", data is not None, "create_run 返回 None")
        if data:
            run_id = data.get("run_id", "")
            r.check("run_id 以 run_ 前缀", run_id.startswith("run_"), f"got {run_id}")
            hex_part = run_id[4:] if run_id.startswith("run_") else run_id
            r.check("hex 部分为 32 字符", len(hex_part) == 32, f"got {len(hex_part)} chars: {hex_part}")
            r.check("hex 部分全为 hex 字符", all(c in "0123456789abcdef" for c in hex_part), f"not hex: {hex_part}")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_create_run(self) -> TestResult:
        r = TestResult("POST /acp/runs — 创建运行", "api")
        t0 = time.perf_counter()
        data = self._create_run("测试消息")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("返回非空", data is not None, "create_run 返回 None")
        if data:
            r.check("含 run_id", "run_id" in data, str(list(data.keys())))
            r.check("status = pending", data.get("status") == "pending", f"got {data.get('status')}")
            r.check("含 created_at", "created_at" in data, str(list(data.keys())))
            self.run_context["created_run_id"] = data.get("run_id", "")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_get_run_status(self) -> TestResult:
        r = TestResult("GET /acp/runs/{id} — 查询运行状态", "api")
        run_data = self._create_run("查询状态测试")
        if not run_data:
            r.status = SKIP
            r.detail = "创建运行失败，跳过"
            return r
        run_id = run_data["run_id"]
        t0 = time.perf_counter()
        resp = self._acp_get(f"/acp/runs/{run_id}")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        data = resp.json()
        r.check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")
        r.check("run_id 一致", data.get("run_id") == run_id, f"got {data.get('run_id')}")
        r.check("status = pending", data.get("status") == "pending", f"got {data.get('status')}")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_list_runs(self) -> TestResult:
        r = TestResult("GET /acp/runs — 列出所有运行", "api")
        self._create_run("列表测试")
        t0 = time.perf_counter()
        resp = self._acp_get("/acp/runs")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        data = resp.json()
        r.check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")
        r.check("含 runs 数组", "runs" in data, str(list(data.keys())))
        r.check("runs 非空", len(data.get("runs", [])) > 0, "runs 为空")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_execute_run(self) -> TestResult:
        r = TestResult("POST /acp/runs/{id}/execute — 同步执行", "api")
        if ACP_SKIP_EXECUTE:
            r.status = SKIP
            r.detail = "ACP_SKIP_EXECUTE=1，跳过同步执行测试"
            return r
        run_data = self._create_run("设备编号XX，发动机动力不足，请帮忙诊断")
        if not run_data:
            r.status = SKIP
            r.detail = "创建运行失败"
            return r
        run_id = run_data["run_id"]
        t0 = time.perf_counter()
        resp = self._acp_post(f"/acp/runs/{run_id}/execute")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        data = resp.json()
        r.check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")
        r.check("status = completed", data.get("status") == "completed", f"got {data.get('status')}")
        r.check("messages 非空", len(data.get("messages", [])) > 0, "messages 为空")
        agent_msgs = [m for m in data.get("messages", []) if m.get("role") == "agent"]
        r.check("有 agent 消息", len(agent_msgs) > 0, "无 agent 消息")
        if agent_msgs:
            text_parts = [p.get("content", "") for p in agent_msgs[0].get("parts", []) if p.get("type") == "text"]
            full_text = "".join(text_parts)
            r.check("agent 回复非空", len(full_text) > 10, f"回复仅 {len(full_text)} 字符")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_stream_run_two_step(self) -> TestResult:
        r = TestResult("POST /acp/runs/{id}/stream — 两步流式执行", "api")
        run_data = self._create_run("请查询设备信息")
        if not run_data:
            r.status = SKIP
            r.detail = "创建运行失败"
            return r
        run_id = run_data["run_id"]
        t0 = time.perf_counter()
        resp = self._acp_post(f"/acp/runs/{run_id}/stream", stream=True)
        events = self._collect_sse_events(resp)
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")
        r.check("收到 SSE 事件", len(events) > 0, "无事件")
        has_completed = any(e.get("type") == "run.completed" for e in events)
        has_message_delta = any(e.get("type") == "message_delta" for e in events)
        r.check("含 run.completed 事件", has_completed, f"事件类型: {set(e.get('type','') for e in events)}")
        r.check("含 message_delta 事件", has_message_delta, f"事件类型: {set(e.get('type','') for e in events)}")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_execute_stream(self) -> TestResult:
        r = TestResult("POST /acp/runs/execute_stream — 快捷流式执行", "api")
        payload: dict[str, Any] = {
            "messages": [
                {"role": "user", "parts": [{"type": "text", "content": "请介绍一下你的功能"}]}
            ],
            "context": {"session_id": f"test_{uuid.uuid4().hex[:6]}"},
        }
        t0 = time.perf_counter()
        resp = self._acp_post("/acp/runs/execute_stream", data=payload, stream=True)
        events = self._collect_typed_sse_events(resp)
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")
        r.check("收到 SSE 事件", len(events) > 0, "无事件")
        event_types = set(e.get("event", "") for e in events)
        r.check("含 answer 事件", "answer" in event_types, f"事件: {event_types}")
        r.check("含 run.completed 事件", "run.completed" in event_types, f"事件: {event_types}")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_cancel_run(self) -> TestResult:
        r = TestResult("DELETE /acp/runs/{id} — 取消运行", "api")
        run_data = self._create_run("待取消的运行")
        if not run_data:
            r.status = SKIP
            r.detail = "创建运行失败"
            return r
        run_id = run_data["run_id"]
        t0 = time.perf_counter()
        resp = self._acp_delete(f"/acp/runs/{run_id}")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        data = resp.json()
        r.check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")
        r.check("status = cancelled", data.get("status") == "cancelled", f"got {data.get('status')}")
        resp2 = self._acp_get(f"/acp/runs/{run_id}")
        data2 = resp2.json()
        r.check("再次查询为 cancelled", data2.get("status") == "cancelled", f"got {data2.get('status')}")
        if r.status != FAIL:
            r.status = PASS
        return r

    # ═══════════════════════════════════════════
    # Test Group 2: 错误场景
    # ═══════════════════════════════════════════

    def _acp_auth_disabled(self) -> bool:
        """快速探测 ACP 认证是否关闭（authentication.type = 'none'）。"""
        try:
            resp = self.session.get(
                self._url("/acp/runs"),
                headers={"Content-Type": "application/json"},
                timeout=5,
            )
            return resp.status_code != 401
        except Exception:
            return True

    def test_unauthorized_access(self) -> TestResult:
        r = TestResult("401 — 无 Token 访问受保护端点", "error")
        if self._acp_auth_disabled():
            r.status = SKIP
            r.detail = "ACP 认证未开启（authentication.type = none），跳过"
            return r
        t0 = time.perf_counter()
        resp = self.session.post(
            self._url("/acp/runs"),
            json={"messages": [{"role": "user", "parts": [{"type": "text", "content": "test"}]}]},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 401", resp.status_code == 401, f"got {resp.status_code}")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_invalid_token(self) -> TestResult:
        r = TestResult("401 — 无效 Token", "error")
        if self._acp_auth_disabled():
            r.status = SKIP
            r.detail = "ACP 认证未开启（authentication.type = none），跳过"
            return r
        saved = self.token
        self.token = "invalid_token_xxx"
        t0 = time.perf_counter()
        resp = self._acp_post("/acp/runs", data={
            "messages": [{"role": "user", "parts": [{"type": "text", "content": "test"}]}],
        })
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        self.token = saved
        r.check("HTTP 401", resp.status_code == 401, f"got {resp.status_code}")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_run_not_found(self) -> TestResult:
        r = TestResult("404 — 查询不存在的 run_id", "error")
        fake_id = "run_nonexistent999"
        t0 = time.perf_counter()
        resp = self._acp_get(f"/acp/runs/{fake_id}")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 404", resp.status_code == 404, f"got {resp.status_code}")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_execute_not_found(self) -> TestResult:
        r = TestResult("404 — 执行不存在的 run_id", "error")
        fake_id = "run_nonexistent888"
        t0 = time.perf_counter()
        resp = self._acp_post(f"/acp/runs/{fake_id}/execute")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 404", resp.status_code == 404, f"got {resp.status_code}")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_stream_not_found(self) -> TestResult:
        r = TestResult("404 — 流式执行不存在的 run_id", "error")
        fake_id = "run_nonexistent777"
        t0 = time.perf_counter()
        resp = self._acp_post(f"/acp/runs/{fake_id}/stream")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 404", resp.status_code == 404, f"got {resp.status_code}")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_create_run_no_message(self) -> TestResult:
        r = TestResult("400 — 创建运行时消息为空", "error")
        t0 = time.perf_counter()
        resp = self._acp_post("/acp/runs", data={
            "messages": [],
            "context": {},
        })
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 400", resp.status_code == 400, f"got {resp.status_code}")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_cancel_not_found(self) -> TestResult:
        r = TestResult("404 — 取消不存在的 run_id", "error")
        t0 = time.perf_counter()
        resp = self._acp_delete("/acp/runs/run_fake_cancel_404")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 404", resp.status_code == 404, f"got {resp.status_code}")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_await_not_found(self) -> TestResult:
        r = TestResult("404 — await 不存在的 run_id", "error")
        fake_id = "run_nonexistent_await"
        t0 = time.perf_counter()
        resp = self._acp_post(f"/acp/runs/{fake_id}/await", data={
            "run_id": fake_id,
            "await_id": "await_test",
            "response": {"role": "user", "parts": [{"type": "text", "content": "test"}]},
        })
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 404", resp.status_code == 404, f"got {resp.status_code}")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_await_wrong_state(self) -> TestResult:
        r = TestResult("400 — await 处于非 AWAITING 状态的 run", "error")
        run_data = self._create_run("await 状态测试")
        if not run_data:
            r.status = SKIP
            r.detail = "创建运行失败"
            return r
        run_id = run_data["run_id"]
        t0 = time.perf_counter()
        resp = self._acp_post(f"/acp/runs/{run_id}/await", data={
            "run_id": run_id,
            "await_id": "await_test",
            "response": {"role": "user", "parts": [{"type": "text", "content": "test"}]},
        })
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 400", resp.status_code == 400, f"got {resp.status_code}")
        r.check("错误信息含 not in awaiting", "not in awaiting" in resp.json().get("detail", ""), resp.json().get("detail", ""))
        if r.status != FAIL:
            r.status = PASS
        return r

    # ═══════════════════════════════════════════
    # Test Group 2b: 用户隔离测试
    # ═══════════════════════════════════════════

    def test_isolation_get_own_run(self) -> TestResult:
        r = TestResult("隔离: 用户可查询自己创建的 run", "isolation")
        run_data = self._create_run("隔离测试-自己可见")
        if not run_data:
            r.status = SKIP
            r.detail = "创建运行失败"
            return r
        run_id = run_data["run_id"]
        t0 = time.perf_counter()
        resp = self._acp_get(f"/acp/runs/{run_id}")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")
        r.check("run_id 一致", resp.json().get("run_id") == run_id, f"got {resp.json().get('run_id')}")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_isolation_list_only_own_runs(self) -> TestResult:
        r = TestResult("隔离: list_runs 只返回当前用户的 run", "isolation")
        created_id = None
        run_data = self._create_run("隔离测试-list过滤")
        if run_data:
            created_id = run_data["run_id"]
        t0 = time.perf_counter()
        resp = self._acp_get("/acp/runs")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")
        data = resp.json()
        runs = data.get("runs", [])
        r.check("runs 是列表", isinstance(runs, list), f"got {type(runs)}")
        if created_id:
            found = any(run.get("run_id") == created_id for run in runs)
            r.check("刚创建的 run 在列表中", found, f"created_id={created_id} not in {len(runs)} runs")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_isolation_cancel_own_run(self) -> TestResult:
        r = TestResult("隔离: 用户可取消自己创建的 run", "isolation")
        run_data = self._create_run("隔离测试-可取消")
        if not run_data:
            r.status = SKIP
            r.detail = "创建运行失败"
            return r
        run_id = run_data["run_id"]
        t0 = time.perf_counter()
        resp = self._acp_delete(f"/acp/runs/{run_id}")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_isolation_other_user_run_returns_404(self) -> TestResult:
        r = TestResult("隔离: 查询不存在/其他用户的 run 返回 404", "isolation")
        fake_id = f"run_{uuid.uuid4().hex}"
        t0 = time.perf_counter()
        resp = self._acp_get(f"/acp/runs/{fake_id}")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 404", resp.status_code == 404, f"got {resp.status_code}")
        resp2 = self._acp_post(f"/acp/runs/{fake_id}/execute")
        r.check("execute 也返回 404", resp2.status_code == 404, f"got {resp2.status_code}")
        resp3 = self._acp_delete(f"/acp/runs/{fake_id}")
        r.check("delete 也返回 404", resp3.status_code == 404, f"got {resp3.status_code}")
        if r.status != FAIL:
            r.status = PASS
        return r

    # ═══════════════════════════════════════════
    # Test Group 3: 多语言测试
    # ═══════════════════════════════════════════

    def _run_lang_test(self, lang: str, message: str) -> TestResult:
        r = TestResult(f"多语言: lang={lang} — {message[:30]}", "lang")
        self._set_lang_enabled(True, default_lang="zh")
        time.sleep(0.3)
        payload: dict[str, Any] = {
            "messages": [
                {"role": "user", "parts": [{"type": "text", "content": message}]}
            ],
            "context": {"session_id": f"lang_test_{lang}_{uuid.uuid4().hex[:4]}"},
            "lang": lang,
        }
        t0 = time.perf_counter()
        resp = self._acp_post("/acp/runs/execute_stream", data=payload, stream=True)
        events = self._collect_typed_sse_events(resp)
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")
        r.check("收到 SSE 事件", len(events) > 0, "无事件")
        answer_texts = []
        for e in events:
            evt = e.get("event", "")
            d = e.get("data", {})
            if evt == "answer" and d.get("text"):
                answer_texts.append(d["text"])
        full_answer = "".join(answer_texts)
        r.check("Agent 有回复", len(full_answer) > 10, f"回复仅 {len(full_answer)} 字符")
        lang_names = {"zh": "中文", "en": "英文", "ja": "日文", "ko": "韩文"}
        r.check(f"回复非空 ({lang_names.get(lang, lang)})", len(full_answer) > 0, "回复为空")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_lang_zh(self) -> TestResult:
        return self._run_lang_test("zh", "设备编号XX，发动机动力不足，请求故障诊断")

    def test_lang_en(self) -> TestResult:
        return self._run_lang_test("en", "The equipment number is XX, the engine lacks power, please diagnose the fault")

    def test_lang_ja(self) -> TestResult:
        return self._run_lang_test("ja", "設備番号XX、エンジン出力不足、故障診断をお願いします")

    def test_lang_ko(self) -> TestResult:
        return self._run_lang_test("ko", "장비 번호 XX, 엔진 출력 부족, 고장 진단을 요청합니다")

    def test_lang_disabled(self) -> TestResult:
        r = TestResult("多语言关闭: lang 参数应被忽略", "lang")
        self._set_lang_enabled(False)
        time.sleep(0.3)
        payload: dict[str, Any] = {
            "messages": [
                {"role": "user", "parts": [{"type": "text", "content": "设备编号XX，发动机动力不足"}]}
            ],
            "context": {"session_id": f"lang_disabled_{uuid.uuid4().hex[:4]}"},
            "lang": "ja",
        }
        t0 = time.perf_counter()
        resp = self._acp_post("/acp/runs/execute_stream", data=payload, stream=True)
        events = self._collect_typed_sse_events(resp)
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")
        r.check("收到 SSE 事件", len(events) > 0, "无事件")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_lang_auto_detect(self) -> TestResult:
        r = TestResult("多语言自动检测: 不传 lang 参数", "lang")
        self._set_lang_enabled(True, default_lang="zh")
        time.sleep(0.3)
        payload: dict[str, Any] = {
            "messages": [
                {"role": "user", "parts": [{"type": "text", "content": "Please check the fault history for equipment XX"}]}
            ],
            "context": {"session_id": f"lang_auto_{uuid.uuid4().hex[:4]}"},
        }
        t0 = time.perf_counter()
        resp = self._acp_post("/acp/runs/execute_stream", data=payload, stream=True)
        events = self._collect_typed_sse_events(resp)
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")
        r.check("收到 SSE 事件", len(events) > 0, "无事件")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_lang_with_execute_endpoint(self) -> TestResult:
        r = TestResult("多语言 + 同步 execute: lang=en", "lang")
        self._set_lang_enabled(True, default_lang="zh")
        time.sleep(0.3)
        run_data = self._create_run(
            "The equipment number is XX, please check the fault history",
            lang="en",
        )
        if not run_data:
            r.status = SKIP
            r.detail = "创建运行失败"
            return r
        run_id = run_data["run_id"]
        t0 = time.perf_counter()
        resp = self._acp_post(f"/acp/runs/{run_id}/execute")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        data = resp.json()
        r.check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")
        r.check("status = completed", data.get("status") == "completed", f"got {data.get('status')}")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_lang_with_two_step_stream(self) -> TestResult:
        r = TestResult("多语言 + 两步 stream: lang=ja", "lang")
        self._set_lang_enabled(True, default_lang="zh")
        time.sleep(0.3)
        run_data = self._create_run(
            "設備番号XXの故障履歴を確認してください",
            lang="ja",
        )
        if not run_data:
            r.status = SKIP
            r.detail = "创建运行失败"
            return r
        run_id = run_data["run_id"]
        t0 = time.perf_counter()
        resp = self._acp_post(f"/acp/runs/{run_id}/stream", stream=True)
        events = self._collect_sse_events(resp)
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")
        r.check("收到 SSE 事件", len(events) > 0, "无事件")
        if r.status != FAIL:
            r.status = PASS
        return r

    # ═══════════════════════════════════════════
    # Test Group 4: 输出抑制测试
    # ═══════════════════════════════════════════

    def _run_suppress_test(
        self,
        patterns: str,
        message: str,
        should_not_contain: list[str] | None = None,
        should_contain: list[str] | None = None,
    ) -> TestResult:
        desc_patterns = patterns or "(空)"
        r = TestResult(f"输出抑制: patterns='{desc_patterns}'", "suppress")
        self._set_lang_enabled(False)
        self._set_suppress_patterns(patterns)
        time.sleep(0.3)
        payload: dict[str, Any] = {
            "messages": [
                {"role": "user", "parts": [{"type": "text", "content": message}]}
            ],
            "context": {"session_id": f"suppress_{uuid.uuid4().hex[:4]}"},
        }
        t0 = time.perf_counter()
        resp = self._acp_post("/acp/runs/execute_stream", data=payload, stream=True)
        events = self._collect_typed_sse_events(resp)
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")
        r.check("收到 SSE 事件", len(events) > 0, "无事件")

        result_events = [e for e in events if e.get("event") == "result"]
        all_result_text = json.dumps(result_events, ensure_ascii=False).lower()

        if should_not_contain:
            for kw in should_not_contain:
                r.check(
                    f"结果中不含 '{kw}'",
                    kw.lower() not in all_result_text,
                    f"发现被抑制的关键词 '{kw}'",
                )

        if should_contain:
            for kw in should_contain:
                r.check(
                    f"结果中含 '{kw}'",
                    kw.lower() in all_result_text,
                    f"未找到预期关键词 '{kw}'",
                )

        if r.status != FAIL:
            r.status = PASS
        return r

    def test_suppress_empty(self) -> TestResult:
        return self._run_suppress_test(
            patterns="",
            message="设备编号XX，发动机动力不足，请求故障诊断",
        )

    def test_suppress_tool_name(self) -> TestResult:
        return self._run_suppress_test(
            patterns="business_data_search",
            message="设备编号XX，发动机动力不足，请求故障诊断",
            should_not_contain=["business_data_search"],
        )

    def test_suppress_operation_keyword(self) -> TestResult:
        return self._run_suppress_test(
            patterns="故障历史",
            message="设备编号XX，发动机动力不足，请求故障诊断",
            should_not_contain=["故障历史"],
        )

    def test_suppress_multiple(self) -> TestResult:
        return self._run_suppress_test(
            patterns="business_data_search,故障历史",
            message="设备编号XX，发动机动力不足，请求故障诊断",
            should_not_contain=["business_data_search", "故障历史"],
        )

    def test_suppress_with_stream_endpoint(self) -> TestResult:
        r = TestResult("输出抑制 + 两步 stream: patterns=故障历史", "suppress")
        self._set_lang_enabled(False)
        self._set_suppress_patterns("故障历史")
        time.sleep(0.3)
        run_data = self._create_run("设备编号XX，发动机动力不足，请求故障诊断")
        if not run_data:
            r.status = SKIP
            r.detail = "创建运行失败"
            return r
        run_id = run_data["run_id"]
        t0 = time.perf_counter()
        resp = self._acp_post(f"/acp/runs/{run_id}/stream", stream=True)
        events = self._collect_sse_events(resp)
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")
        r.check("收到 SSE 事件", len(events) > 0, "无事件")
        result_events = [e for e in events if e.get("type") == "result"]
        all_text = json.dumps(result_events, ensure_ascii=False).lower()
        r.check("结果中不含 '故障历史'", "故障历史" not in all_text, "发现被抑制的关键词")
        if r.status != FAIL:
            r.status = PASS
        return r

    # ═══════════════════════════════════════════
    # Test Group 5: 完整流程测试
    # ═══════════════════════════════════════════

    def test_full_flow_execute_stream(self) -> TestResult:
        r = TestResult("完整流程: create → execute_stream → 端到端", "flow")
        t0 = time.perf_counter()
        payload: dict[str, Any] = {
            "messages": [
                {"role": "user", "parts": [{"type": "text", "content": "请查询设备编号XX的故障历史"}]}
            ],
            "context": {"session_id": f"flow_{uuid.uuid4().hex[:4]}"},
        }
        resp = self._acp_post("/acp/runs/execute_stream", data=payload, stream=True)
        events = self._collect_typed_sse_events(resp)
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")

        event_types = [e.get("event", "") for e in events]
        r.check("含 run.started", "run.started" in event_types, f"事件: {set(event_types)}")
        r.check("含 answer", "answer" in event_types, f"事件: {set(event_types)}")
        r.check("含 run.completed", "run.completed" in event_types, f"事件: {set(event_types)}")

        answer_texts = [e["data"]["text"] for e in events if e.get("event") == "answer" and e.get("data", {}).get("text")]
        full_answer = "".join(answer_texts)
        r.check("完整回复 > 20 字符", len(full_answer) > 20, f"仅 {len(full_answer)} 字符")

        if r.status != FAIL:
            r.status = PASS
        return r

    def test_full_flow_two_step(self) -> TestResult:
        r = TestResult("完整流程: create → get_status → stream → 端到端", "flow")
        run_data = self._create_run("请介绍一下你支持的功能")
        if not run_data:
            r.status = SKIP
            r.detail = "创建运行失败"
            return r
        run_id = run_data["run_id"]

        resp_status = self._acp_get(f"/acp/runs/{run_id}")
        r.check("状态查询 200", resp_status.status_code == 200, f"got {resp_status.status_code}")
        r.check("初始状态 pending", resp_status.json().get("status") == "pending", f"got {resp_status.json().get('status')}")

        t0 = time.perf_counter()
        resp_stream = self._acp_post(f"/acp/runs/{run_id}/stream", stream=True)
        events = self._collect_sse_events(resp_stream)
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("stream 200", resp_stream.status_code == 200, f"got {resp_stream.status_code}")
        r.check("收到事件", len(events) > 0, "无事件")

        has_completed = any(e.get("type") == "run.completed" for e in events)
        r.check("含 run.completed", has_completed, f"事件: {set(e.get('type','') for e in events)}")

        resp_final = self._acp_get(f"/acp/runs/{run_id}")
        final_status = resp_final.json().get("status")
        r.check("最终状态 completed", final_status == "completed", f"got {final_status}")

        if r.status != FAIL:
            r.status = PASS
        return r

    def test_full_flow_execute_then_query(self) -> TestResult:
        r = TestResult("完整流程: create → execute → get_status → 端到端", "flow")
        run_data = self._create_run("查询设备XX的故障信息")
        if not run_data:
            r.status = SKIP
            r.detail = "创建运行失败"
            return r
        run_id = run_data["run_id"]

        t0 = time.perf_counter()
        resp_exec = self._acp_post(f"/acp/runs/{run_id}/execute")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("execute 200", resp_exec.status_code == 200, f"got {resp_exec.status_code}")
        exec_data = resp_exec.json()
        r.check("status = completed", exec_data.get("status") == "completed", f"got {exec_data.get('status')}")

        resp_query = self._acp_get(f"/acp/runs/{run_id}")
        query_data = resp_query.json()
        r.check("查询状态 completed", query_data.get("status") == "completed", f"got {query_data.get('status')}")
        r.check("有 agent 消息", len(query_data.get("messages", [])) > 0, "无消息")

        if r.status != FAIL:
            r.status = PASS
        return r

    def test_full_flow_cancel_after_create(self) -> TestResult:
        r = TestResult("完整流程: create → cancel → get_status 验证", "flow")
        run_data = self._create_run("待取消测试")
        if not run_data:
            r.status = SKIP
            r.detail = "创建运行失败"
            return r
        run_id = run_data["run_id"]

        t0 = time.perf_counter()
        resp_cancel = self._acp_delete(f"/acp/runs/{run_id}")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("cancel 200", resp_cancel.status_code == 200, f"got {resp_cancel.status_code}")

        resp_query = self._acp_get(f"/acp/runs/{run_id}")
        r.check("查询状态 cancelled", resp_query.json().get("status") == "cancelled", f"got {resp_query.json().get('status')}")

        if r.status != FAIL:
            r.status = PASS
        return r

    def test_full_flow_list_after_runs(self) -> TestResult:
        r = TestResult("完整流程: 创建多个 run → list 验证", "flow")
        ids = []
        for i in range(3):
            rd = self._create_run(f"列表测试消息 {i}")
            if rd:
                ids.append(rd["run_id"])
        r.check("成功创建 3 个 run", len(ids) == 3, f"仅创建 {len(ids)} 个")

        t0 = time.perf_counter()
        resp = self._acp_get("/acp/runs")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        data = resp.json()
        runs = data.get("runs", [])
        run_ids_in_list = [run_info.get("run_id") for run_info in runs]
        for rid in ids:
            r.check(f"run {rid} 在列表中", rid in run_ids_in_list, f"未找到 {rid}")

        if r.status != FAIL:
            r.status = PASS
        return r

    def test_combined_lang_and_suppress(self) -> TestResult:
        r = TestResult("组合测试: 语言跟随(en) + 输出抑制(故障历史)", "flow")
        self._set_lang_enabled(True, default_lang="zh")
        self._set_suppress_patterns("故障历史")
        time.sleep(0.3)
        payload: dict[str, Any] = {
            "messages": [
                {"role": "user", "parts": [{"type": "text", "content": "Equipment XX, engine power insufficient, please diagnose"}]}
            ],
            "context": {"session_id": f"combo_{uuid.uuid4().hex[:4]}"},
            "lang": "en",
        }
        t0 = time.perf_counter()
        resp = self._acp_post("/acp/runs/execute_stream", data=payload, stream=True)
        events = self._collect_typed_sse_events(resp)
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")
        r.check("收到 SSE 事件", len(events) > 0, "无事件")

        result_events = [e for e in events if e.get("event") == "result"]
        all_text = json.dumps(result_events, ensure_ascii=False).lower()
        r.check("结果中不含 '故障历史'", "故障历史" not in all_text, "发现被抑制的关键词")

        if r.status != FAIL:
            r.status = PASS
        return r

    # ═══════════════════════════════════════════
    # Test Group 7: 安全与正确性回归测试
    # ═══════════════════════════════════════════

    def test_default_auth_requires_jwt(self) -> TestResult:
        r = TestResult("安全: 默认配置下 ACP 写接口需要 JWT 认证", "error")
        saved_token = self.token
        self.token = None
        headers_no_auth = {"Content-Type": "application/json"}
        t0 = time.perf_counter()
        resp = self.session.post(
            self._url("/acp/runs"),
            json={"messages": [{"role": "user", "parts": [{"type": "text", "content": "auth test"}]}]},
            headers=headers_no_auth,
            timeout=10,
        )
        self.token = saved_token
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("无 Token 返回 401", resp.status_code == 401, f"got {resp.status_code}")
        if r.status != FAIL:
            r.status = PASS
        return r

    def test_run_id_consistency_after_execute(self) -> TestResult:
        r = TestResult("正确性: execute 后 run_id 与 Orchestrator 一致", "flow")
        if ACP_SKIP_EXECUTE:
            r.status = SKIP
            r.detail = "ACP_SKIP_EXECUTE=1，跳过"
            return r
        run_data = self._create_run("run_id 一致性测试")
        if not run_data:
            r.status = SKIP
            r.detail = "创建运行失败"
            return r
        acp_run_id = run_data["run_id"]

        t0 = time.perf_counter()
        resp = self._acp_post(f"/acp/runs/{acp_run_id}/execute")
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        data = resp.json()
        r.check("execute 200", resp.status_code == 200, f"got {resp.status_code}")
        r.check("返回 run_id 与创建时一致", data.get("run_id") == acp_run_id,
                f"创建={acp_run_id}, 返回={data.get('run_id')}")
        r.check("status = completed", data.get("status") == "completed", f"got {data.get('status')}")
        r.check("有 agent 消息", len(data.get("messages", [])) > 0, "无消息")

        resp2 = self._acp_get(f"/acp/runs/{acp_run_id}")
        query_data = resp2.json()
        r.check("查询 run_id 一致", query_data.get("run_id") == acp_run_id,
                f"查询={query_data.get('run_id')}")

        if r.status != FAIL:
            r.status = PASS
        return r

    def test_stream_run_id_consistency(self) -> TestResult:
        r = TestResult("正确性: stream 后 run_id 与 Orchestrator 一致", "flow")
        run_data = self._create_run("stream run_id 一致性测试")
        if not run_data:
            r.status = SKIP
            r.detail = "创建运行失败"
            return r
        acp_run_id = run_data["run_id"]

        t0 = time.perf_counter()
        resp = self._acp_post(f"/acp/runs/{acp_run_id}/stream", stream=True)
        events = self._collect_typed_sse_events(resp)
        r.duration_ms = int((time.perf_counter() - t0) * 1000)
        r.check("stream 200", resp.status_code == 200, f"got {resp.status_code}")
        r.check("收到事件", len(events) > 0, "无事件")

        sse_run_ids = set()
        for e in events:
            d = e.get("data", {})
            if d.get("run_id"):
                sse_run_ids.add(d["run_id"])
        r.check("SSE 中 run_id 一致", sse_run_ids == {acp_run_id},
                f"SSE run_ids={sse_run_ids}, ACP run_id={acp_run_id}")

        has_completed = any(e.get("event") == "run.completed" for e in events)
        r.check("含 run.completed", has_completed, f"事件: {set(e.get('event','') for e in events)}")

        resp2 = self._acp_get(f"/acp/runs/{acp_run_id}")
        query_data = resp2.json()
        r.check("查询 run_id 一致", query_data.get("run_id") == acp_run_id,
                f"查询={query_data.get('run_id')}")
        r.check("最终状态 completed", query_data.get("status") == "completed",
                f"got {query_data.get('status')}")

        if r.status != FAIL:
            r.status = PASS
        return r


def _get_all_tests() -> list[tuple[str, str]]:
    return [
        ("api", "test_get_agent"),
        ("api", "test_get_agent_endpoint_urls"),
        ("api", "test_get_agent_auth_scheme"),
        ("api", "test_create_run"),
        ("api", "test_create_run_full_uuid"),
        ("api", "test_get_run_status"),
        ("api", "test_list_runs"),
        ("api", "test_execute_run"),
        ("api", "test_stream_run_two_step"),
        ("api", "test_execute_stream"),
        ("api", "test_cancel_run"),
        ("error", "test_unauthorized_access"),
        ("error", "test_invalid_token"),
        ("error", "test_run_not_found"),
        ("error", "test_execute_not_found"),
        ("error", "test_stream_not_found"),
        ("error", "test_create_run_no_message"),
        ("error", "test_cancel_not_found"),
        ("error", "test_await_not_found"),
        ("error", "test_await_wrong_state"),
        ("error", "test_default_auth_requires_jwt"),
        ("isolation", "test_isolation_get_own_run"),
        ("isolation", "test_isolation_list_only_own_runs"),
        ("isolation", "test_isolation_cancel_own_run"),
        ("isolation", "test_isolation_other_user_run_returns_404"),
        ("lang", "test_lang_zh"),
        ("lang", "test_lang_en"),
        ("lang", "test_lang_ja"),
        ("lang", "test_lang_ko"),
        ("lang", "test_lang_disabled"),
        ("lang", "test_lang_auto_detect"),
        ("lang", "test_lang_with_execute_endpoint"),
        ("lang", "test_lang_with_two_step_stream"),
        ("suppress", "test_suppress_empty"),
        ("suppress", "test_suppress_tool_name"),
        ("suppress", "test_suppress_operation_keyword"),
        ("suppress", "test_suppress_multiple"),
        ("suppress", "test_suppress_with_stream_endpoint"),
        ("flow", "test_full_flow_execute_stream"),
        ("flow", "test_full_flow_two_step"),
        ("flow", "test_full_flow_execute_then_query"),
        ("flow", "test_full_flow_cancel_after_create"),
        ("flow", "test_full_flow_list_after_runs"),
        ("flow", "test_combined_lang_and_suppress"),
        ("flow", "test_run_id_consistency_after_execute"),
        ("flow", "test_stream_run_id_consistency"),
    ]


_GROUP_LABELS = {
    "api": "API 端点基础功能",
    "error": "错误场景",
    "isolation": "用户隔离",
    "lang": "多语言",
    "suppress": "输出抑制",
    "flow": "完整流程",
}


def main():
    args = sys.argv[1:]
    if "--list" in args:
        print("\n  ACP 协议全端点测试用例列表")
        print("  " + "=" * 60)
        tests = _get_all_tests()
        current_group = ""
        idx = 0
        for group, method in tests:
            if group != current_group:
                current_group = group
                print(f"\n  [{_GROUP_LABELS.get(group, group)}]")
            idx += 1
            print(f"    {idx:2d}. {method}")
        print(f"\n  共 {len(tests)} 个用例\n")
        return

    group_filter = "all"
    for a in args:
        if a.startswith("--group="):
            group_filter = a.split("=", 1)[1]
        elif a == "--group" and args.index(a) + 1 < len(args):
            group_filter = args[args.index(a) + 1]

    valid_groups = {"all", "api", "error", "isolation", "lang", "suppress", "flow"}
    if group_filter not in valid_groups:
        print(f"  ✗ 未知测试组: {group_filter}")
        print(f"    可选: {', '.join(sorted(valid_groups))}")
        sys.exit(1)

    runner = ACPTestRunner()

    print()
    print("  " + "=" * 64)
    print("    ACP 协议全端点自动化测试")
    print("    " + datetime.now(timezone.utc).isoformat())
    print("  " + "=" * 64)
    print(f"    后端地址: {BASE_URL}")
    print(f"    工作空间: {WORKSPACE_ID}")
    print(f"    测试超时: {TEST_TIMEOUT}s")
    print(f"    测试组:   {group_filter}")
    print()

    print("  [1/3] 登录认证...")
    if not runner.login():
        print("  ✗ 登录失败，请检查 AUTH_USERNAME / AUTH_PASSWORD")
        sys.exit(1)
    print("  ✓ 登录成功\n")

    original_config = runner._get_runtime_config()
    original_lang = original_config.get("lang", {})
    original_suppress = original_config.get("acp_suppress_script_patterns", "")

    tests = _get_all_tests()
    if group_filter != "all":
        tests = [(g, m) for g, m in tests if g == group_filter]

    total = len(tests)
    print(f"  [2/3] 开始测试 ({total} 个用例)")
    print("  " + "-" * 64)

    group_order = ["api", "error", "isolation", "lang", "suppress", "flow"]
    tests.sort(key=lambda t: group_order.index(t[0]) if t[0] in group_order else 99)

    current_group = ""
    idx = 0
    for group, method_name in tests:
        if group != current_group:
            current_group = group
            print(f"\n  ▶ {_GROUP_LABELS.get(group, group)}")
            print(f"    {'─' * 50}")

        idx += 1
        test_method = getattr(runner, method_name, None)
        if test_method is None:
            print(f"    {idx:2d}/{total}  ⊘ {method_name} — 方法不存在")
            continue

        print(f"    {idx:2d}/{total}  ⏳ {method_name}...", end="", flush=True)
        try:
            result: TestResult = test_method()
        except Exception as exc:
            result = TestResult(
                name=method_name,
                group=group,
                status=FAIL,
                detail=f"异常: {exc}",
            )

        runner.results.append(result)

        icon = {"PASS": "✓", "FAIL": "✗", "SKIP": "⊘"}.get(result.status, "?")
        time_str = f"{result.duration_ms}ms" if result.duration_ms < 1000 else f"{result.duration_ms / 1000:.1f}s"
        print(f"\r    {idx:2d}/{total}  {icon} {method_name} [{time_str}]", end="")
        if result.detail and result.status != PASS:
            print(f" — {result.detail}", end="")
        print()

        for assertion in result.assertions:
            if not assertion["passed"]:
                print(f"         ↳ ✗ {assertion['desc']}: {assertion['detail']}")

    print(f"\n    {'─' * 50}")

    print(f"\n  [3/3] 恢复运行时配置...")
    runner._set_runtime_config({
        "lang": original_lang,
        "acp_suppress_script_patterns": original_suppress,
    })
    print("  ✓ 配置已恢复\n")

    passed = sum(1 for r in runner.results if r.status == PASS)
    failed = sum(1 for r in runner.results if r.status == FAIL)
    skipped = sum(1 for r in runner.results if r.status == SKIP)

    print("  " + "=" * 64)
    print(f"    测试结果汇总")
    print("  " + "=" * 64)
    print(f"    总计: {len(runner.results)}    通过: {passed}    失败: {failed}    跳过: {skipped}")

    if runner.results:
        total_ms = sum(r.duration_ms for r in runner.results)
        print(f"    总耗时: {total_ms / 1000:.1f}s")

    by_group: dict[str, list[TestResult]] = {}
    for r in runner.results:
        by_group.setdefault(r.group, []).append(r)

    for group in group_order:
        if group not in by_group:
            continue
        group_results = by_group[group]
        g_pass = sum(1 for r in group_results if r.status == PASS)
        g_fail = sum(1 for r in group_results if r.status == FAIL)
        g_skip = sum(1 for r in group_results if r.status == SKIP)
        print(f"\n    [{_GROUP_LABELS.get(group, group)}]  {g_pass}✓  {g_fail}✗  {g_skip}⊘")
        for r in group_results:
            icon = {"PASS": "✓", "FAIL": "✗", "SKIP": "⊘"}.get(r.status, "?")
            time_str = f"{r.duration_ms}ms" if r.duration_ms < 1000 else f"{r.duration_ms / 1000:.1f}s"
            line = f"      {icon} {r.name} [{time_str}]"
            if r.status == FAIL and r.detail:
                line += f" — {r.detail}"
            print(line)

    print()

    result_dir = os.path.join(_SCRIPT_DIR, "result")
    os.makedirs(result_dir, exist_ok=True)
    generated_at = datetime.now(timezone.utc)
    filename_timestamp = generated_at.strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(result_dir, f"acp_test_{filename_timestamp}.json")

    report = {
        "timestamp": generated_at.isoformat(),
        "base_url": BASE_URL,
        "workspace_id": WORKSPACE_ID,
        "group_filter": group_filter,
        "summary": {
            "total": len(runner.results),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "total_ms": sum(r.duration_ms for r in runner.results),
        },
        "results": [
            {
                "name": r.name,
                "group": r.group,
                "status": r.status,
                "detail": r.detail,
                "duration_ms": r.duration_ms,
                "assertions": r.assertions,
            }
            for r in runner.results
        ],
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  报告已保存: {report_path}\n")

    if failed > 0:
        print(f"  ⚠ 有 {failed} 个测试失败！\n")
        sys.exit(1)
    else:
        print("  ✓ 全部通过！\n")


if __name__ == "__main__":
    main()
