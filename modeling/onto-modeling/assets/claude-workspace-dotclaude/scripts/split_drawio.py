#!/usr/bin/env python3
"""
将 draw.io 多页文件拆分为单独的 .drawio 文件。

用法：
    python3 scripts/split_drawio.py <input.drawio> [output_dir]

示例：
    python3 scripts/split_drawio.py source/ECO变更影响分析业务流程0330.drawio source/diagrams
"""

import re
import os
import sys


def split_drawio(input_path: str, output_dir: str) -> None:
    with open(input_path, "r", encoding="utf-8") as f:
        content = f.read()

    header_match = re.match(r'(<mxfile[^>]*>)', content)
    header = header_match.group(1) if header_match else '<mxfile>'

    os.makedirs(output_dir, exist_ok=True)

    pattern = r'(<diagram\b[^>]*>.*?</diagram>)'
    matches = re.findall(pattern, content, re.DOTALL)

    if not matches:
        print("未找到任何 diagram，请检查输入文件。")
        return

    saved = []
    for diagram_block in matches:
        name_match = re.search(r'name="([^"]+)"', diagram_block)
        name = name_match.group(1) if name_match else "unnamed"
        safe_name = re.sub(r'[/\\:*?"<>|]', '_', name).replace(' ', '')
        filename = os.path.join(output_dir, f"{safe_name}.drawio")
        file_content = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'{header}\n'
            f'  {diagram_block}\n'
            f'</mxfile>\n'
        )
        with open(filename, "w", encoding="utf-8") as f:
            f.write(file_content)
        saved.append(safe_name)

    print(f"已保存 {len(saved)} 个文件到 {output_dir}/")
    for name in saved:
        print(f"  {name}.drawio")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    input_file = sys.argv[1]
    output_folder = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(input_file), "diagrams"
    )
    split_drawio(input_file, output_folder)
