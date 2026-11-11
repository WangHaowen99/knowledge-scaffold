"""原始资料分段预览：规则「原始资料来源」芯片点开后的原文（文本按行，Word 按段与表格单元格）。

分段和锚点直接复用工具箱 source_extract（资料分析验收用的同一份抽取），页面上的段号、
行号与报告「来源位置」的锚点一致。结果只含定位点附近的一个窗口：最多 WINDOW 段，
单段正文最多 MAX_TEXT 字，超出的部分标记出来，完整内容走 /raw 下载原文件。
"""

from __future__ import annotations

from pathlib import Path

from .toolkit import source_extract

WINDOW = 300
CONTEXT_BEFORE = 60
MAX_TEXT = 4000
# xlsx 由 /excel 的表格预览负责定位；这里只处理按段展示的格式
SEGMENT_SUFFIXES = source_extract.TEXT_SUFFIXES | {".docx"}


class SourcePreviewError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def preview(path: Path, anchor: str = "") -> dict:
    if path.suffix.lower() not in SEGMENT_SUFFIXES:
        raise SourcePreviewError("该格式不支持分段预览，请下载原文件查看", 415)
    if path.stat().st_size > source_extract.MAX_BYTES:
        limit_mb = source_extract.MAX_BYTES // (1024 * 1024)
        return _unavailable(f"文件超过 {limit_mb} MB，无法在线预览，请下载原文件查看", anchor)
    result = source_extract.extract(path)
    if result["status"] != "ok":
        reason = result.get("reason", "").removeprefix("无法核验：")
        return _unavailable(f"无法预览：{reason}", anchor)

    segments = result["segments"]
    index = next((i for i, s in enumerate(segments) if s["anchor"] == anchor), None) if anchor else None
    start = max(0, index - CONTEXT_BEFORE) if index is not None else 0
    end = min(len(segments), start + WINDOW)
    return {
        "status": "ok",
        "anchor": anchor,
        "anchor_found": (index is not None) if anchor else None,
        "total": len(segments),
        "offset": start,
        "has_more_before": start > 0,
        "has_more_after": end < len(segments),
        "segments": [
            {
                "anchor": segment["anchor"],
                "text": segment["text"][:MAX_TEXT],
                "truncated": len(segment["text"]) > MAX_TEXT,
            }
            for segment in segments[start:end]
        ],
    }


def _unavailable(reason: str, anchor: str) -> dict:
    return {"status": "unavailable", "reason": reason, "anchor": anchor, "segments": []}
