"""校验/追踪/codegen 报告的条目化解析：供评审视图做编号勾选与逐条确认。

报告是 agent 按命令约定生成的 markdown（### 文件名 分组 + 列表条目），
这里做尽力而为的结构化：解析失败的行原样保留为条目，不丢信息。
"""

from __future__ import annotations

import re

ITEM_RE = re.compile(r"^\s*(?:[-*]|\d+[.、)])\s+(.+)$")
HEADING_RE = re.compile(r"^(#{2,4})\s+(.+)$")
WARN_HINT = re.compile(r"警告|⚠|warning|建议|提示", re.IGNORECASE)
OK_HINT = re.compile(r"^(无|通过|无问题|没有问题|全部通过|0 个|无孤立|无失效)")


def parse(text: str) -> dict:
    """返回 {items: [{id, group, text, severity}], summary}。"""
    items = []
    group = ""
    summary_lines = []
    for line in text.splitlines():
        heading = HEADING_RE.match(line)
        if heading:
            group = heading.group(2).strip()
            continue
        match = ITEM_RE.match(line)
        if not match:
            if line.strip() and not items and len(summary_lines) < 6:
                summary_lines.append(line.strip())
            continue
        content = match.group(1).strip()
        if not content or OK_HINT.match(content):
            continue
        items.append(
            {
                "id": len(items) + 1,
                "group": group,
                "text": content[:500],
                "severity": "warning" if WARN_HINT.search(content) else "error",
            }
        )
    return {"items": items, "summary": " ".join(summary_lines)[:300]}
