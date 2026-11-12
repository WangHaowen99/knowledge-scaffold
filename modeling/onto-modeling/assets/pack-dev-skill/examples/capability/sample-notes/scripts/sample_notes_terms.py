"""从纪要里找候选术语：引号里的词和大写缩写，按出现次数输出前 20 个（JSON）。"""

import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path

LIMIT = 20
PATTERN = re.compile(r"「([^」\n]{2,20})」|\b([A-Z][A-Z0-9]{1,9})\b")


# 命令行输出通道：独立 logger，不经根 logger，消息原样（无前缀）
_console = logging.Logger("sample_notes_terms", logging.INFO)
_console_handler = logging.StreamHandler()
_console.addHandler(_console_handler)


def _say(text: object = "", *, error: bool = False) -> None:
    """输出一行：结果写标准输出（助手读它），error=True 写标准错误；每次取当前的流，测试替换了也跟着走。"""
    _console_handler.setStream(sys.stderr if error else sys.stdout)
    _console.info(text)


def candidates(text: str) -> Counter:
    return Counter(quoted or abbreviation for quoted, abbreviation in PATTERN.findall(text))


def main() -> int:
    if len(sys.argv) != 2:
        _say("用法：sample_notes_terms.py <纪要路径>", error=True)
        return 2
    counts = candidates(Path(sys.argv[1]).read_text(encoding="utf-8"))
    top = [{"term": term, "count": count} for term, count in counts.most_common(LIMIT)]
    _say(json.dumps({"total": len(counts), "top": top}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
