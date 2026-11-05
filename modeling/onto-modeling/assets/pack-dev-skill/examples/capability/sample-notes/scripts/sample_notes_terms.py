"""从纪要里找候选术语：引号里的词和大写缩写，按出现次数输出前 20 个（JSON）。"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

LIMIT = 20
PATTERN = re.compile(r"「([^」\n]{2,20})」|\b([A-Z][A-Z0-9]{1,9})\b")


def candidates(text: str) -> Counter:
    return Counter(quoted or abbreviation for quoted, abbreviation in PATTERN.findall(text))


def main() -> int:
    if len(sys.argv) != 2:
        print("用法：sample_notes_terms.py <纪要路径>", file=sys.stderr)
        return 2
    counts = candidates(Path(sys.argv[1]).read_text(encoding="utf-8"))
    top = [{"term": term, "count": count} for term, count in counts.most_common(LIMIT)]
    print(json.dumps({"total": len(counts), "top": top}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
