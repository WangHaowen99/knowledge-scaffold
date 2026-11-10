"""内测引擎的用量带上缓存命中数，口径同稳定引擎（Anthropic）：input_tokens 不含命中部分，命中记在
cache_read_input_tokens。OpenAI 口径的 prompt_tokens 本来就含命中部分，要拆开、不能重复计；
拆开后计费、项目用量、时间线里「输入 + 缓存读」的总数不变；配额按成本口径把缓存读折成 1/10（ADR-0088）。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.governance import quota
from app.services import model_pricing
from app.services.engines.jiuwen import JiuwenEngine, _call_usage_rail
from app.services.session_host import timeline
from tests.test_jiuwen_engine import make_ctx


def _call(input_tokens, output_tokens, cache_tokens):
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens, cache_tokens=cache_tokens)
    return SimpleNamespace(inputs=SimpleNamespace(response=SimpleNamespace(usage_metadata=usage)))


def test_rail_reports_cache_hits_of_each_call():
    calls = []
    rail = _call_usage_rail(lambda *args: calls.append(args))

    async def scenario():
        await rail.after_model_call(_call(1200, 80, 1000))
        await rail.after_model_call(_call(1500, 40, 0))

    asyncio.run(scenario())
    assert calls == [(1200, 80, 1000), (1500, 40, 0)]
    assert rail.total_cache_tokens == 1000


def test_model_usage_splits_cached_part_out_of_input(tmp_path):
    engine = JiuwenEngine(make_ctx(tmp_path))
    engine._record_call_usage(1200, 80, 1000)
    event = engine._queue.get_nowait()
    assert event["usage"] == {"input_tokens": 200, "cache_read_input_tokens": 1000, "output_tokens": 80}
    # 拆开后缓存读单独计：配额按成本口径折算（ADR-0088），200 + 80 + 1000 // 10
    assert model_pricing.tokens_of(event["usage"]) == 380
    assert quota._usage_of({"type": "model_usage", "usage": event["usage"]})[0] == 380
    assert timeline._usage(event["usage"]) == {"input_tokens": 200, "output_tokens": 80, "cache_read_tokens": 1000}


def test_turn_result_usage_carries_cache_hits(tmp_path):
    engine = JiuwenEngine(make_ctx(tmp_path))
    engine._tracker = _call_usage_rail(engine._record_call_usage)
    before = engine._usage_snapshot()
    asyncio.run(engine._tracker.after_model_call(_call(1200, 80, 1000)))
    result = engine._turn_result("success", 0.0, before)
    assert result["usage"] == {"input_tokens": 200, "cache_read_input_tokens": 1000, "output_tokens": 80}
