"""F 第一批第 12 条（测试部署时发现）：模型网关地址进了容器日志。

openjiuwen 的 ImageModalityProbe 以 INFO 级别打出 `api_base=<网关地址>`；它的日志器不经过我们的根日志器，
直接写 stdout。不改 openjiuwen 包：在它的日志器上挂脱敏过滤器。顺带：httpx、openai 等经根日志器出来的
记录，内测引擎写进错误留档的原始错误，都去掉地址和密钥。
"""
from __future__ import annotations

import asyncio
import json
import logging

from app.governance import telemetry
from app.services.engines import jiuwen
from tests.test_jiuwen_engine import chunk, make_ctx

GATEWAY = "https://gateway.example.invalid/compatible/v1"
KEY = "sk-" + "a1b2c3d4e5f6a7b8"


class _Capture(logging.Handler):  # NOSONAR(S4792) 测试里挂一个收集日志的 handler，验证日志脱敏
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def test_openjiuwen_probe_log_hides_gateway():
    from openjiuwen.core.common.logging import logger

    jiuwen.install_log_redaction()
    getattr(logger, "info")  # 先让 openjiuwen 建好它的日志器（建的时候会清掉已有的 handler）
    capture = _Capture()
    target = logging.getLogger("common")
    level = target.level
    target.setLevel(logging.INFO)  # 部署里是 INFO，网关地址就是这样进的容器日志
    target.addHandler(capture)
    try:
        logger.info("[ImageModalityProbe] image modality probed: api_base=%s model=%s supported=%s",
                    GATEWAY, "synthetic", False)
        logger.warning("[ImageModalityProbe] image modality probe call failed: %s",
                       f"Client error '401' for url '{GATEWAY}/chat/completions' key={KEY}")
    finally:
        target.removeHandler(capture)
        target.setLevel(level)
    assert len(capture.lines) == 2
    assert all("gateway.example.invalid" not in line and KEY not in line for line in capture.lines)
    assert "model=synthetic" in capture.lines[0]


def test_root_json_log_hides_http_client_endpoints():
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1, 'HTTP Request: POST %s "HTTP/1.1 200 OK"',
                               (f"{GATEWAY}/chat/completions",), None)
    line = json.loads(telemetry.JsonFormatter().format(record))["message"]
    assert "gateway.example.invalid" not in line and "HTTP Request: POST" in line


def test_engine_error_archive_hides_gateway(tmp_path, monkeypatch):
    from openjiuwen.core.runner import Runner

    def broken_stream(agent, inputs, session=None):
        async def gen():
            yield chunk("llm_output", {"content": "开始"})
            raise RuntimeError(f"Server error '502 Bad Gateway' for url '{GATEWAY}/chat/completions'")

        return gen()

    monkeypatch.setattr(Runner, "run_agent_streaming", staticmethod(broken_stream))
    logged = []
    ctx = make_ctx(tmp_path)

    def log_error(kind, text):
        logged.append(text)

    ctx.log_error = log_error
    engine = jiuwen.JiuwenEngine(ctx)
    engine.client = object()
    asyncio.run(engine._run_turn("开始"))
    assert logged and all("gateway.example.invalid" not in text for text in logged)
    assert "502" in logged[0]
