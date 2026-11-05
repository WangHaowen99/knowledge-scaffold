#!/usr/bin/env python3
"""场景模拟的标准索引（详设 5.9.4「内置解析器搞不定时，由包的脚本在写入时顺手生成一份标准格式的索引」）。

内核能按格式读出场景本身（ontology/testcase/*.md 每个「## 场景 N：…」一项），但推演结论要从推演
结果里算。推演核对通过后由命令调用本脚本，把每个场景的结论写进 `ext/trace/index/scenarios.jsonl`；
内核只读这份索引。索引记着场景文件的哈希，场景文件之后改了而没重新推演，页面显示「待刷新」。

    trace_index.py record TESTCASE --results tmp/trace-results.json
    trace_index.py mock-skipped --object 门店 --reason "纯计算对象，不需要示例数据"
    trace_index.py mock-skipped --clear

只用标准库；场景解析复用同目录的 trace_inputs.parse_source（与交接核对同一份规则）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trace_inputs import parse_source  # noqa: E402

SCENARIOS_INDEX = Path("ext/trace/index/scenarios.jsonl")
MOCK_SKIPPED = Path("ext/trace/index/mock-skipped.jsonl")
TEXT_MAX = 400
LOG_MAX = 4000


def _read_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _clip(text: str, limit: int = TEXT_MAX) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _issue_text(issue) -> str:
    if isinstance(issue, dict):
        parts = [str(issue.get(key)) for key in ("type", "message") if issue.get(key)]
        step = issue.get("step")
        return _clip(("步骤 %s：" % step if step not in (None, "") else "") + "；".join(parts))
    return _clip(str(issue))


def _trace_log(result: dict) -> str:
    steps = result.get("trace") or result.get("steps") or []
    lines = []
    for step in steps if isinstance(steps, list) else []:
        if isinstance(step, dict):
            label = step.get("step") or step.get("index") or len(lines) + 1
            text = step.get("action") or step.get("description") or step.get("result") or json.dumps(step, ensure_ascii=False)
            lines.append(f"{label}. {_clip(text, 200)}")
        else:
            lines.append(f"{len(lines) + 1}. {_clip(step, 200)}")
    text = "\n".join(lines)
    return text[:LOG_MAX]


def record(testcase: Path, results_path: Path) -> list[str]:
    """把一份场景文件的推演结果写进索引，返回写入的键。"""
    payload = parse_source(testcase)
    results = json.loads(results_path.read_text(encoding="utf-8"))
    if not isinstance(results, list):
        raise ValueError("推演结果必须是数组")
    by_id = {str(item.get("scenario_id")): item for item in results if isinstance(item, dict)}
    source_sha = hashlib.sha256(testcase.read_bytes()).hexdigest()[:16]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stem = testcase.stem
    written = []
    records = {item.get("key"): item for item in _read_jsonl(SCENARIOS_INDEX)}
    for scenario in payload["scenarios"]:
        result = by_id.get(str(scenario["id"]))
        if result is None:
            continue
        issues = result.get("issues") or []
        passed = bool(result.get("reached_end")) and not issues and result.get("result") != "halted"
        key = f"{stem}--{scenario['id']}"
        fail_reason = "" if passed else (_issue_text(issues[0]) if issues else "推演中途停下，需要人工确认")
        records[key] = {
            "key": key,
            "fields": {
                "result": "pass" if passed else "fail",
                "last_run_at": now,
                "actual": _clip(result.get("actual") or result.get("outcome") or
                                ("正常走完" if result.get("reached_end") else "中途停止")),
                "fail_reason": fail_reason,
            },
            "sections": {"推演记录": _trace_log(result)},
            "source": testcase.as_posix(),
            "source_sha256": source_sha,
        }
        written.append(key)
    # 场景文件里已经没有的旧键去掉
    alive = {f"{stem}--{scenario['id']}" for scenario in payload["scenarios"]}
    records = {key: value for key, value in records.items()
               if key and (not key.startswith(f"{stem}--") or key in alive)}
    _write_jsonl(SCENARIOS_INDEX, [records[key] for key in sorted(records)])
    return written


def mock_skipped(objects: list[str], reason: str, clear: bool) -> int:
    records = [] if clear else _read_jsonl(MOCK_SKIPPED)
    names = {item.get("entry") for item in records}
    for name in objects:
        if name and name not in names:
            records.append({"entry": name, "kind": "object", "reason": _clip(reason or "没有生成示例数据")})
    _write_jsonl(MOCK_SKIPPED, records)
    return len(records)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("testcase", type=Path)
    rec.add_argument("--results", type=Path, required=True)
    skip = sub.add_parser("mock-skipped")
    skip.add_argument("--object", action="append", default=[])
    skip.add_argument("--reason", default="")
    skip.add_argument("--clear", action="store_true")
    args = parser.parse_args()
    try:
        if args.cmd == "record":
            keys = record(args.testcase, args.results)
            print(f"已写入场景索引 {len(keys)} 项：{SCENARIOS_INDEX}")
        else:
            count = mock_skipped(args.object, args.reason, args.clear)
            print(f"跳过生成的对象 {count} 个：{MOCK_SKIPPED}")
    except (OSError, ValueError) as exc:
        print(f"[trace_index:error] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
