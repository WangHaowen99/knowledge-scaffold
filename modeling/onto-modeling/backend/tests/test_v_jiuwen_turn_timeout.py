"""V 阶段真实模型发现：内测引擎每一轮被框架默认的 completion_timeout（600 秒）掐断。

真实模型下资料分析、对象提取一轮常超过 10 分钟；到点后框架取消这一轮，结果却记成正常结束（is_error=false），
「这一轮改了什么」卡带着做了一半的修改出来，命令锁也没释放。一轮的长短由最大迭代数与用户打断兜底。
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app import config
from app.services.engines.jiuwen import JiuwenEngine
from tests.test_jiuwen_engine import make_ctx


class _Captured(Exception):
    pass


def test_deep_agent_turn_is_not_capped_at_ten_minutes(tmp_path, monkeypatch):
    import openjiuwen.harness as harness

    from app.services import model_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    captured = {}

    def fake_create(model, **kwargs):
        captured.update(kwargs)
        raise _Captured()

    monkeypatch.setattr(harness, "create_deep_agent", fake_create)
    engine = JiuwenEngine(make_ctx(tmp_path))
    cfg = {"base_url": "http://127.0.0.1:9/unused", "api_key": uuid.uuid4().hex, "model": "synthetic",
           "context": model_settings.jiuwen_effective_context()}
    with pytest.raises(_Captured):
        engine._build_agent(cfg)
    assert "completion_timeout" in captured and captured["completion_timeout"] is None
