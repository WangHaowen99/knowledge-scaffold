"""模型单价表（方案 B14、契约 SY-18）：费用 = token × 单价，自主构建的预算与运行报告按它算。

- 表存在数据目录 `governance/model-pricing.json`：`{currency, items: [{engine, model, input_per_mtok,
  output_per_mtok, cache_read_per_mtok}]}`，单价按每百万 token；
- 查价：先按（引擎, 模型）精确匹配，再取同一引擎 `model="*"` 的一行；
- 没有单价时：引擎自己报了费用（Claude SDK 的 `total_cost_usd`）就用它，否则记 0 并标为估算——Jiuwen 不报费用，
  没配单价时只能按 token 记账（预算的 token 维度照常生效）。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .. import config

CURRENCIES = ("USD", "CNY")
DEFAULT_CURRENCY = "USD"  # 与 C-01 billing.currency 一致
MAX_ITEMS = 100
_lock = threading.Lock()


class PricingError(ValueError):
    def __init__(self, message: str, fields: dict):
        super().__init__(message)
        self.fields = fields


def _file() -> Path:
    return Path(config.DATA_DIR) / "governance" / "model-pricing.json"


def load() -> dict:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    currency = data.get("currency") if data.get("currency") in CURRENCIES else DEFAULT_CURRENCY
    items = [item for item in data.get("items") or [] if isinstance(item, dict)]
    return {"currency": currency, "items": items}


def _engines() -> tuple[str, ...]:
    from ..api.views import ENGINE_LABELS

    return tuple(ENGINE_LABELS)


def _price(value, *, optional: bool = False) -> bool:
    if value is None:
        return optional
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def validate(body: dict) -> dict:
    """SY-18 PUT 的请求体 → 规整后的表；不合法抛 PricingError（fields 指到哪一行）。"""
    currency = body.get("currency")
    if currency not in CURRENCIES:
        raise PricingError("币种只能是 USD 或 CNY", {"currency": "enum"})
    raw = body.get("items")
    if not isinstance(raw, list) or len(raw) > MAX_ITEMS:
        raise PricingError(f"items 是不超过 {MAX_ITEMS} 行的列表", {"items": "list"})
    items, seen = [], set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise PricingError(f"第 {index + 1} 行单价不对", {f"items.{index}": "object"})
        engine = item.get("engine")
        model = str(item.get("model") or "").strip()
        ok = (engine in _engines() and 0 < len(model) <= 120 and _price(item.get("input_per_mtok"))
              and _price(item.get("output_per_mtok")) and _price(item.get("cache_read_per_mtok"), optional=True))
        if not ok:
            raise PricingError(f"第 {index + 1} 行单价不对", {f"items.{index}": "invalid"})
        if (engine, model) in seen:
            raise PricingError(f"第 {index + 1} 行和前面重复", {f"items.{index}": "duplicate"})
        seen.add((engine, model))
        items.append({"engine": engine, "model": model, "input_per_mtok": float(item["input_per_mtok"]),
                      "output_per_mtok": float(item["output_per_mtok"]),
                      "cache_read_per_mtok": None if item.get("cache_read_per_mtok") is None
                      else float(item["cache_read_per_mtok"])})
    return {"currency": currency, "items": items}


def save(body: dict) -> dict:
    table = validate(body)
    path = _file()
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix="pricing-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(table, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return load()


def current_model(engine: str, owner: str = "") -> str:
    """这个引擎现在用的模型名：所有者个人配置里写了就用它，否则用管理员配置；取不到为空串。"""
    from . import model_settings, personal_model_settings

    try:
        if owner and personal_model_settings.model_owner(owner, engine):
            model = personal_model_settings.resolve(owner, engine).get("model") or ""
            if model:
                return model
    except Exception:  # noqa: BLE001 - 个人配置读不出时按管理员配置
        pass
    if engine == "jiuwen":
        return model_settings.jiuwen_config().get("model") or ""
    return str(model_settings.load().get("model") or os.environ.get("ANTHROPIC_MODEL") or "")


def lookup(engine: str, model: str, table: dict | None = None) -> dict | None:
    table = table or load()
    rows = [item for item in table["items"] if item.get("engine") == engine]
    return next((item for item in rows if item.get("model") == model), None) or \
        next((item for item in rows if item.get("model") == "*"), None)


def cost(usage: dict, *, engine: str, model: str, reported: float | None = None,
         table: dict | None = None) -> tuple[float, bool]:
    """一条用量的费用（单价表的币种）：(金额, 是否估算)。有单价按单价算；没有时用引擎报的费用（美元，只在单价表
    也是美元时用）；都没有记 0。"""
    usage = usage or {}
    table = table or load()
    row = lookup(engine, model, table)
    if row is not None:
        cache_price = row.get("cache_read_per_mtok")
        if cache_price is None:
            cache_price = row["input_per_mtok"]
        amount = (int(usage.get("input_tokens") or 0) * row["input_per_mtok"]
                  + int(usage.get("output_tokens") or 0) * row["output_per_mtok"]
                  + int(usage.get("cache_read_input_tokens") or 0) * cache_price) / 1_000_000
        return amount, False
    if table["currency"] == "USD" and isinstance(reported, (int, float)) and not isinstance(reported, bool):
        return float(reported), False
    return 0.0, True


def tokens_of(usage: dict) -> int:
    """计入预算的 token：输入 + 缓存读 + 输出（与配额台账同一口径）。"""
    usage = usage or {}
    return sum(max(0, int(usage.get(key) or 0)) for key in ("input_tokens", "cache_read_input_tokens", "output_tokens"))
