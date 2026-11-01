#!/usr/bin/env python3
"""
提取 draw.io 流程图的可读摘要，每个图单独保存到 ontology/diagrams-summary/<图名>.md。

用法：
    python3 .claude/scripts/batch_summarize.py                         # 全量：ontology/diagrams/ 下所有图
    python3 .claude/scripts/batch_summarize.py ontology/diagrams       # 同上
    python3 .claude/scripts/batch_summarize.py a.drawio b.drawio       # 增量：只更新指定文件的摘要
"""

import os
import sys

sys.path.append(os.path.dirname(__file__))
from summarize_diagram import summarize

DEFAULT_DIR = "ontology/diagrams"
DEFAULT_LEGEND = "resource/diagrams/图示说明定义.drawio"
SUMMARY_DIR = "ontology/diagrams-summary"


def resolve_paths(args):
    """将命令行参数解析为文件路径列表。"""
    if not args:
        args = [DEFAULT_DIR]
    paths = []
    for arg in args:
        if os.path.isdir(arg):
            paths.extend(sorted(
                os.path.join(arg, f) for f in os.listdir(arg)
                if f.endswith(".drawio") and not f.endswith("_fixed.drawio")
            ))
        elif arg.endswith(".drawio") and os.path.isfile(arg):
            paths.append(arg)
        else:
            print("跳过：%s（不是 .drawio 文件或不存在）" % arg)
    return paths


def main(target_paths, legend_path=None):
    if legend_path is None:
        legend_path = DEFAULT_LEGEND

    os.makedirs(SUMMARY_DIR, exist_ok=True)

    for fpath in target_paths:
        name = os.path.basename(fpath).replace(".drawio", "")
        out_path = os.path.join(SUMMARY_DIR, "%s.md" % name)
        try:
            text = summarize(fpath, legend_path)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(text)
            print("  OK %s → %s" % (name, out_path))
        except Exception as e:
            if os.path.exists(out_path):
                print("  !! %s: %s（保留上一份摘要，未覆盖）" % (name, e))
            else:
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write("# %s — 摘要提取失败\n\n错误：%s" % (name, e))
                print("  !! %s: %s" % (name, e))

    print()
    print("已更新 %d 个摘要 → %s/" % (len(target_paths), SUMMARY_DIR))


if __name__ == "__main__":
    target_paths = resolve_paths(sys.argv[1:])
    if not target_paths:
        print("没有找到 .drawio 文件")
        sys.exit(1)
    main(target_paths)
