"""个人信息扫描（ADR-0084「快照回到内核时再扫一遍」；`design/evidence-collection.md` §9）。

网关的 `mask-v1` 已经按列名和取值遮过一遍；数据库快照的样本（`sample`）、画像（`profile`）入库前内核再按取值扫一遍，
只认格式明确的四种：手机号、18 位证件号、邮箱、16～19 位银行卡号（数字之间可以有单个空格或连字符）。
命中的整个值换成「«已遮蔽»」（只遮命中的一段，剩下的部分还可能拼出原值）。宁可多遮。
"""

from __future__ import annotations

import re

from . import spec

MASK = "«已遮蔽»"
PATTERNS = (
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("id_card", re.compile(r"(?<![0-9A-Za-z])\d{17}[0-9Xx](?![0-9A-Za-z])")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    ("bank_card", re.compile(r"(?<!\d)(?<!\d[ -])(?:\d[ -]?){15,18}\d(?!\d)(?![ -]\d)")),
)
# 画像里的计数、千分比是统计量，不是取值
_STAT_KEYS = frozenset({"null_pct", "distinct", "rows", "count", "pct", "permille", "ordinal", "rows_estimate"})


def contains_pii(text: str) -> bool:
    return any(pattern.search(text) for _name, pattern in PATTERNS)


def mask_value(value, counter: list[int], key: str = ""):
    """递归遮蔽字符串和整数取值；字典的键、统计量不动。列表里的元素不继承键名（样本的 `rows` 是行列表）。"""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (str, int)):
        if key in _STAT_KEYS or not contains_pii(str(value)):
            return value
        counter[0] += 1
        return MASK
    if isinstance(value, list):
        return [mask_value(item, counter) for item in value]
    if isinstance(value, dict):
        return {name: mask_value(item, counter, str(name)) for name, item in value.items()}
    return value


def mask_record(record: dict) -> tuple[dict, int]:
    """遮蔽一条记录的 `label`、`attrs`（锚点、名字是身份，不动）；有命中就重算 `hash`。"""
    counter = [0]
    masked = {**record, "label": mask_value(record.get("label"), counter),
              "attrs": mask_value(record.get("attrs") or {}, counter)}
    if not counter[0]:
        return record, 0
    masked["hash"] = spec.record_hash(masked)
    return masked, counter[0]
