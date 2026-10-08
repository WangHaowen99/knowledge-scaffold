"""Excel 转 JSON 预览：openpyxl 解析，前端用表格组件渲染。

结果体积上限：每 sheet 最多 MAX_ROWS×MAX_COLS，超出部分标记 truncated，
完整内容用 /raw 下载原文件查看。
"""

from __future__ import annotations

from pathlib import Path

MAX_ROWS = 500
MAX_COLS = 60
MAX_SHEETS = 20


class ExcelPreviewError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def preview(path: Path) -> dict:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise ExcelPreviewError("服务端未安装 openpyxl", 501) from exc

    # read_only 模式解析不出 merged_cells；小文件走完整加载拿合并信息，大文件降级
    read_only = path.stat().st_size > 8 * 1024 * 1024
    try:
        wb = load_workbook(path, read_only=read_only, data_only=True)
    except Exception as exc:
        raise ExcelPreviewError(f"Excel 文件无法解析：{exc}") from exc

    sheets = []
    try:
        for ws in wb.worksheets[:MAX_SHEETS]:
            rows = []
            truncated = False
            for row_index, row in enumerate(ws.iter_rows(values_only=True)):
                if row_index >= MAX_ROWS:
                    truncated = True
                    break
                cells = [_cell_text(v) for v in row[:MAX_COLS]]
                if len(row) > MAX_COLS:
                    truncated = True
                rows.append(cells)
            # read_only 模式 merged_cells 仍可用；转成前端可消费的跨行列描述
            merges = []
            try:
                for rng in ws.merged_cells.ranges:
                    if rng.min_row <= MAX_ROWS and rng.min_col <= MAX_COLS:
                        merges.append(
                            {
                                "row": rng.min_row - 1,
                                "col": rng.min_col - 1,
                                "rowspan": rng.max_row - rng.min_row + 1,
                                "colspan": rng.max_col - rng.min_col + 1,
                            }
                        )
            except Exception:
                merges = []
            sheets.append(
                {
                    "name": ws.title,
                    "rows": rows,
                    "merges": merges,
                    "truncated": truncated,
                }
            )
    finally:
        wb.close()
    return {"sheets": sheets, "sheet_truncated": len(wb.worksheets) > MAX_SHEETS}


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
