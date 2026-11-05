"""证据采集三方共用的格式工具（`docs/plugin-architecture/design/evidence-collection.md`；ADR-0082）。

规范 JSON、票据范围哈希、记录哈希、锚点编码、任务说明头。内核和连接器网关都用它；网关镜像只带 `app/evidence`，
所以这里不能导入后端其他模块。rea-sidecar 用 Node 实现同一套规则，两边共用 `tests/fixtures/evidence_vectors.json` 核对。
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

# 详设 5.8.5 规则 2：名字里这些字符一律百分号编码
ANCHOR_ESCAPE = frozenset("%/.#@|[] ")
SEPARATOR = "::"


def canonical_json(value: Any) -> str:
    """按键排序、无空白、不转义非 ASCII。记录里不放浮点数，免得两种语言的数字写法不同。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scope_hash(value: Any) -> str:
    """票据 `scope` 声明：规范 JSON 的 sha256。"""
    return sha256_text(canonical_json(value))


def job_scope(job_id: str) -> str:
    """查进度、取消、取快照包、清理这几个接口的票据 `scope`。"""
    return scope_hash({"job_id": job_id})


def record_hash(record: dict) -> str:
    """记录的 `hash`：去掉 `hash` 键后的规范 JSON 的 sha256。"""
    return scope_hash({key: value for key, value in record.items() if key != "hash"})


def encode_segment(text: str) -> str:
    return "".join(f"%{ord(char):02X}" if char in ANCHOR_ESCAPE else char for char in str(text))


def qualified(*segments: str) -> str:
    """限定名：各段单独编码，用 `::` 连接。"""
    return SEPARATOR.join(encode_segment(segment) for segment in segments)


def bin_anchor(source_id: str, kind: str, *segments: str) -> str:
    return f"bin:{source_id}/{kind}:{qualified(*segments)}"


def relation_anchor(source_id: str, from_anchor: str, name: str, to: str) -> str:
    """关系记录的锚点；`to` 是目标锚点，没有记录的外部端点用它的 URL。"""
    return f"bin:{source_id}/rel:{sha256_text(f'{from_anchor}|{name}|{to}')[:16]}"


def encode_job_header(spec: dict) -> str:
    """`X-Onto-Job` 头：任务说明的规范 JSON，base64url 不带填充。"""
    return base64.urlsafe_b64encode(canonical_json(spec).encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_job_header(value: str) -> dict:
    padded = value + "=" * (-len(value) % 4)
    data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("任务说明不是对象")
    return data
