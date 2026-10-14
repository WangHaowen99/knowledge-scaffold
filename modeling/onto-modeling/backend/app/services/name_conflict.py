"""任务名称同名判定与候选名生成（与知识包建模工具的同名校验同一口径）。

纯函数，不读盘：调用方给出查重范围内已占用的名称，这里只判定是否冲突、给出候选名。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

MAX_DISPLAY_NAME_LENGTH = 200
CANDIDATE_LIMIT = 3
_MAX_SEQUENCE = 10001
# 原名已带序号（如「示例任务（2）」）时，候选名从去掉序号的基础名重新编号
_SEQUENCE_SUFFIX = re.compile(r"\s*[（(][0-9]+[）)]$")


def normalized_display_name(value) -> str:
    """同名判定口径：去首尾空白 → NFKC（全角/半角统一）→ 小写。"""
    text = "" if value is None else str(value)
    return unicodedata.normalize("NFKC", text.strip()).lower()


def display_name_candidate(base: str, sequence: int) -> str:
    """「基础名（序号）」；超长时截断基础名，保证候选名不超过名称长度上限。"""
    suffix = f"（{sequence}）"
    return base[: MAX_DISPLAY_NAME_LENGTH - len(suffix)].rstrip() + suffix


def detect_name_conflict(
    occupied_names: Iterable[str], attempted_name: str, original_name: str = ""
) -> dict | None:
    """attempted_name 已被占用时返回冲突详情（含至多 3 个未被占用的候选名），否则 None。

    候选名总以 original_name（导入包里的原名；新建时即输入的名称）为基础编号，
    改名重试再次冲突时，候选名不会叠成「名称（2）（2）」。
    """
    occupied = {normalized_display_name(name) for name in occupied_names}
    attempted = str(attempted_name or "").strip()
    if normalized_display_name(attempted) not in occupied:
        return None
    original = str(original_name or "").strip() or attempted
    base = _SEQUENCE_SUFFIX.sub("", original).strip() or original
    candidates: list[str] = []
    for sequence in range(2, _MAX_SEQUENCE + 1):
        candidate = display_name_candidate(base, sequence)
        if normalized_display_name(candidate) in occupied:
            continue
        candidates.append(candidate)
        if len(candidates) == CANDIDATE_LIMIT:
            break
    return {
        "original_name": original,
        "attempted_name": attempted,
        "candidates": candidates,
    }
