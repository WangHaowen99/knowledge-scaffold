"""统计 inputs 目录里的 Markdown 文件，报告写到标准输出（节点把它存进 script.output）。"""

import logging
import sys
from pathlib import Path

LIST_LIMIT = 20
OUTPUT_LIMIT = 1400


def report(root: Path) -> str:
    names = sorted(path.stem for path in root.rglob("*.md") if path.is_file())
    lines = ["# 对象文件统计", "", f"文件数：{len(names)}", ""]
    lines += [f"- {name}" for name in names[:LIST_LIMIT]]
    if len(names) > LIST_LIMIT:
        lines.append(f"- 其余 {len(names) - LIST_LIMIT} 个未列出")
    return "\n".join(lines)[:OUTPUT_LIMIT]


def _emit(text: str) -> None:
    # 报告就是脚本的结果：单独的输出通道，不受应用日志配置影响
    output = logging.Logger("sample_review_stats.out", logging.INFO)
    output.addHandler(logging.StreamHandler(sys.stdout))
    output.info(text)


if __name__ == "__main__":
    _emit(report(Path(sys.argv[1] if len(sys.argv) > 1 else "inputs")))
