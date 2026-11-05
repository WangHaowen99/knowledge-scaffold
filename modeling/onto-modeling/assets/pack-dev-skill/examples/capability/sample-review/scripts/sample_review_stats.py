"""统计 inputs 目录里的 Markdown 文件，报告写到标准输出（节点把它存进 script.output）。"""

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


if __name__ == "__main__":
    print(report(Path(sys.argv[1] if len(sys.argv) > 1 else "inputs")))
