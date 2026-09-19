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
    """返回 {items: [...], summary} 或 codegen 候选专属结构（自动识别）。"""
    if _is_codegen_report(text):
        return parse_codegen(text)
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


# ---- codegen 候选报告专属解析（对准 script-candidate-agent 的报告契约） ----


def _is_codegen_report(text: str) -> bool:
    head = text[:400]
    return "脚本化候选扫描" in head or "codegen-candidates" in head


def _table_rows(block: str) -> list[list[str]]:
    rows = []
    for line in block.splitlines():
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-+:?", c or "-") for c in cells):
            continue  # 分隔行
        rows.append(cells)
    return rows[1:] if rows else []  # 去表头


def _section_blocks(text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    current = ""
    for line in text.splitlines():
        heading = HEADING_RE.match(line)
        if heading:
            current = heading.group(2).strip()
            blocks[current] = ""
            continue
        if current:
            blocks[current] += line + "\n"
    return blocks


def parse_codegen(text: str) -> dict:
    """✅/⚠️ 两表编号连续；候选带片段范围/类型/输入/输出，供勾选后组装
    `/ontology-codegen logic名[:片段区间]` 指令。"""
    candidates = []
    unsuitable = []
    generated = []
    for title, block in _section_blocks(text).items():
        if title.startswith("✅") or title.startswith("⚠️"):
            verdict = "适合" if title.startswith("✅") else "部分适合"
            for cells in _table_rows(block):
                if len(cells) < 6 or not cells[0].isdigit():
                    continue
                candidates.append(
                    {
                        "id": int(cells[0]),
                        "logic": cells[1],
                        "fragment": cells[2],
                        "type": cells[3],
                        "inputs": cells[4].strip("`"),
                        "outputs": cells[5].strip("`"),
                        "note": cells[6] if len(cells) > 6 else "",
                        "verdict": verdict,
                    }
                )
        elif title.startswith("❌"):
            for cells in _table_rows(block):
                if len(cells) >= 2:
                    unsuitable.append({"logic": cells[0], "reason": cells[1]})
        elif title.startswith("📦"):
            for cells in _table_rows(block):
                if len(cells) >= 2:
                    generated.append(
                        {"logic": cells[0], "scripts": cells[1].replace("<br>", "、")}
                    )
    summary = next((l.strip() for l in text.splitlines() if l.strip().startswith("共扫描")), "")
    return {
        "type": "codegen",
        "summary": summary,
        "candidates": candidates,
        "unsuitable": unsuitable,
        "generated": generated,
        "items": [],  # 兼容通用评审的消费方
    }
