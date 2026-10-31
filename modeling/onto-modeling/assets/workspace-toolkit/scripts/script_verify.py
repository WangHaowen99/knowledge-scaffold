#!/usr/bin/env python3
"""ontology/scripts 脚本的运行验证：codegen 生成的脚本必须真正跑通才能提交。

用法（在工作区根目录执行）：
  python3 .claude/scripts/script_verify.py run ontology/scripts/<脚本>.py --cases tmp/verify/<脚本>.cases.json
  python3 .claude/scripts/script_verify.py check ontology/scripts/<脚本>.py [...]
  python3 .claude/scripts/script_verify.py status

run 按用例逐个执行脚本：经脚本验证网关在 jiuwenbox 沙箱中执行，网关或 jiuwenbox 不可用时在 onto-modeling
容器内执行（见 jiuwenbox_client.run_cases）。用例全部通过才写入验证记录
.ontology-state/script-verifications.json；记录绑定脚本连同依赖模块的源码哈希，任一文件改动即失效。
check 供 pre-commit 门禁使用。

成败按 consumer 的口径判定：成功是退出码 0、stdout 是单个 JSON 且不是 ok=false 这类失败结果；
报错是非 0 退出，或退出码 0 但输出 ok=false 的参数错误。用例可以写 stdout_contains（字符串列表），
要求 stdout 前 1500 字里出现这些结论取值。对外脚本的参数里有编号时，用例必须有一个不带任何编号、
写了 stdout_contains 的成功用例：用户只描述情况、没给编号时，consumer 不允许智能体编造编号，脚本也
不能因此改变结论。

run 的输出有上限：最多 12 个用例，每个用例 stdout/stderr 各截 1500 字，最坏约 40KB。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# 与 ontology_checker.py 一样按同目录导入兄弟模块
sys.path.append(str(Path(__file__).resolve().parent))
import jiuwenbox_client as sandbox  # noqa: E402
from script_argument_shape import identity_keys, is_identity_field  # noqa: E402
from script_contract import parse_script  # noqa: E402

RECORD_FILE = Path(".ontology-state") / "script-verifications.json"
SCRIPTS_DIR = Path("ontology") / "scripts"
MAX_CASES = 12
MAX_EXPECTED_TEXTS = 10
EXPECTATIONS = ("success", "error")
# consumer 判定脚本结果失败的口径（consumer/backend/agent/tools/execute_script.py 的 _tool_payload_failed）
FAILED_FLAGS = ("ok", "success", "_success")
ERROR_KEYS = ("error", "_error", "error_code")
FAILED_STATUSES = frozenset({"error", "failed", "failure", "cancelled", "timeout"})


class UsageError(Exception):
    """命令参数、用例文件或脚本路径不合法。"""


def _emit(text: str, stream=None) -> None:
    """把命令结果写到标准输出（或指定的流）。

    这里写出的是命令的产物，不是运行日志：`run` 的 JSON 报告由 agent 读取，`check` 的结论由
    pre-commit 钩子转给用户，`status` 的 JSON 用来排查沙箱连接。所以直接写流、不走 logging——
    日志级别或格式一变，这些内容就可能被改写或吞掉。与 research_checker._emit 同一写法。
    """
    target = sys.stdout if stream is None else stream
    target.write(f"{text}\n")


def resolve_script(root: Path, value: str) -> tuple[Path, str]:
    path = (root / value).resolve()
    if path.parent != (root / SCRIPTS_DIR).resolve() or path.suffix != ".py" or not path.is_file():
        raise UsageError(f"只能验证 ontology/scripts/ 下已存在的 .py 脚本：{value}")
    return path, path.relative_to(root).as_posix()


def load_cases(path: Path) -> list[dict]:
    try:
        cases = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UsageError(f"用例文件读取失败：{exc}") from exc
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise UsageError(f"用例文件必须是 1~{MAX_CASES} 个用例组成的 JSON 数组")
    for index, case in enumerate(cases, 1):
        if not isinstance(case, dict) or not str(case.get("name") or "").strip():
            raise UsageError(f"第 {index} 个用例缺少 name")
        name, argv = case["name"], case.get("argv")
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise UsageError(f"用例「{name}」的 argv 必须是字符串数组（数字和 JSON 也要写成字符串）")
        if case.get("expect") not in EXPECTATIONS:
            raise UsageError(f"用例「{name}」的 expect 只能是 success 或 error")
        expected = case.get("stdout_contains", [])
        if not isinstance(expected, list) or len(expected) > MAX_EXPECTED_TEXTS or not all(
            isinstance(text, str) and text.strip() for text in expected
        ):
            raise UsageError(
                f"用例「{name}」的 stdout_contains 必须是不超过 {MAX_EXPECTED_TEXTS} 个非空字符串的数组"
                "（写结论取值本身，如「有效线索」，不要写带引号和冒号的 JSON 片段）"
            )
    if not any(case["expect"] == "success" for case in cases):
        raise UsageError("至少要有一个 expect=success 的用例，证明脚本能正常跑通")
    return cases


def require_identity_free_case(source: str, cases: list[dict]) -> None:
    """对外脚本的参数里有编号时，至少一个写了 stdout_contains 的成功用例在 argv 的任何位置都不带编号。"""
    try:
        contract = parse_script(source)
    except ValueError:
        return  # 契约无效由规则检查报告
    if not contract.bound:
        return  # 库模块不被 consumer 调用
    identities = sorted({key for param in contract.params for key in identity_keys(param)})
    if not identities:
        return
    identity_free = [
        case for case in cases if case["expect"] == "success" and not _argv_identity_fields(case["argv"])
    ]
    if not identity_free:
        raise UsageError(
            f"缺少不带编号的成功用例：脚本参数里有编号（{'、'.join(identities)}）。请加一个 expect=success 的用例，"
            "顶层编号参数不传，记录里的编号与外键字段也全部删掉，题面没给取值的记录传 {}，模拟用户只描述情况、"
            "没给编号的提问（consumer 不允许智能体编造编号）。脚本要照常给出结论，资料不全时输出资料不足，不能报错"
        )
    if not any(case.get("stdout_contains") for case in identity_free):
        raise UsageError(
            f"不带编号的成功用例（{'、'.join(case['name'] for case in identity_free)}）要用 stdout_contains 写明预期结论，"
            "与资料完整、带编号的同一场景一致：只看退出码时，脚本把资料完整的场景判成资料不足也会通过"
        )


def _argv_identity_fields(argv: list[str]) -> set[str]:
    found: set[str] = set()
    for item in argv:
        flag, _sep, inline = item.partition("=") if item.startswith("--") else ("", "", item)
        name = flag[2:].replace("-", "_")
        if name and is_identity_field(name):
            found.add(name)
        try:
            found |= _record_identity_fields(json.loads(inline))
        except ValueError:
            continue
    return found


def _record_identity_fields(value) -> set[str]:
    if isinstance(value, list):
        return set().union(*(_record_identity_fields(item) for item in value)) if value else set()
    if not isinstance(value, dict):
        return set()
    found = {str(key) for key, item in value.items() if is_identity_field(str(key)) and item not in (None, "")}
    for item in value.values():
        found |= _record_identity_fields(item)
    return found


def _failed_payload(stdout: str) -> bool:
    """stdout 是 consumer 会判为失败的结果。超过输出上限被截断时解析不了，按成功看：
    参数错误的 ok=false 结果很短，不会被截断。"""
    try:
        payload = json.loads(stdout)
    except ValueError:
        return False
    return isinstance(payload, dict) and (
        any(payload.get(key) is False for key in FAILED_FLAGS)
        or any(bool(payload.get(key)) for key in ERROR_KEYS)
        or str(payload.get("status") or "").strip().casefold() in FAILED_STATUSES
    )


def _missing_texts(expected: list[str], stdout: str) -> list[str]:
    """stdout_contains 里没出现的取值。只看回传的前 1500 字；能解析成 JSON 时也按解析后的内容比对，
    这样脚本用 ensure_ascii 输出、中文被转义时也认得出。"""
    haystack = stdout
    try:
        haystack += json.dumps(json.loads(stdout), ensure_ascii=False)
    except ValueError:
        pass
    return [text for text in expected if text not in haystack]


def judge(case: dict, result: dict) -> str:
    """失败原因；空串表示通过。"""
    if result["timed_out"]:
        return f"超过 {sandbox.CASE_TIMEOUT_SECONDS} 秒没有结束"
    failed_payload = result["exit_code"] == 0 and result["stdout_is_json"] and _failed_payload(result["stdout"])
    if case["expect"] == "success":
        if result["exit_code"] != 0:
            return f"预期成功，实际退出码 {result['exit_code']}"
        if not result["stdout_is_json"]:
            return "stdout 不是单个 JSON：CLI 必须把结果序列化成一个 JSON 写到 stdout"
        if failed_payload:
            return (
                "预期成功，实际输出的是失败结果（ok=false / error / status=failed），consumer 会当成调用失败；"
                "资料不足等业务结论写在业务字段里"
            )
    elif result["exit_code"] == 0 and not failed_payload:
        return "预期报错，实际正常输出了结果"
    missing = _missing_texts(case.get("stdout_contains", []), result["stdout"])
    if missing:
        return (
            f"stdout 前 {sandbox.OUTPUT_LIMIT} 字里没有预期的结论：{'、'.join(missing)}"
            "（核对脚本结论是否正确；结论字段要放在输出 JSON 的最前面）"
        )
    return ""


def is_entry_script(path: Path) -> bool:
    """有 __main__ 入口的脚本才会被以 CLI 调用；纯库模块由 import 它的脚本连带验证。"""
    try:
        return "__main__" in path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return True


def load_records(root: Path) -> dict:
    try:
        data = json.loads((root / RECORD_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return scripts if isinstance(scripts, dict) else {}


def save_record(root: Path, relative: str, record: dict) -> None:
    records = load_records(root)
    records[relative] = record
    path = root / RECORD_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "scripts": records}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def command_run(args, root: Path) -> int:
    script, relative = resolve_script(root, args.script)
    cases = load_cases(Path(args.cases))
    require_identity_free_case(script.read_text(encoding="utf-8", errors="replace"), cases)
    verified_hash = sandbox.source_hash(script)
    outcome = sandbox.run_cases(script, [case["argv"] for case in cases])
    reported = []
    for case, result in zip(cases, outcome["results"]):
        reason = judge(case, result)
        reported.append({
            "name": case["name"],
            "expect": case["expect"],
            "ok": not reason,
            "reason": reason,
            "exit_code": result["exit_code"],
            "duration_ms": result["duration_ms"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
        })
    failed = sum(1 for item in reported if not item["ok"])
    # 验证期间脚本又被改过，这次结果不能代表当前源码
    recorded = failed == 0 and sandbox.source_hash(script) == verified_hash
    if recorded:
        save_record(root, relative, {
            "source_hash": verified_hash,
            "runtime": outcome["runtime"],
            "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "cases": [{"name": case["name"], "expect": case["expect"]} for case in cases],
        })
    report = {
        "script": relative,
        "runtime": outcome["runtime"],
        "passed": len(reported) - failed,
        "failed": failed,
        "recorded": recorded,
        "cases": reported,
    }
    if outcome["fallback_reason"]:
        report["fallback_reason"] = outcome["fallback_reason"]
    _emit(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if recorded else 1


def command_check(args, root: Path) -> int:
    changed = {(root / item).resolve() for item in args.scripts}
    records = load_records(root)
    problems = []
    for script in sorted((root / SCRIPTS_DIR).glob("*.py")):
        if not is_entry_script(script):
            continue
        if not {path.resolve() for path in sandbox.script_dependencies(script)} & changed:
            continue
        relative = script.relative_to(root).as_posix()
        record = records.get(relative)
        if not record:
            problems.append(f"{relative}：还没有通过运行验证")
        elif record.get("source_hash") != sandbox.source_hash(script):
            problems.append(f"{relative}：验证通过后，脚本或它依赖的模块又改过")
    for problem in problems:
        _emit(problem)
    return 1 if problems else 0


def command_status(_args, _root: Path) -> int:
    status = sandbox.gateway_status()
    if status["connected"]:
        state = {"sandbox": "connected", "detail": "脚本运行验证经脚本验证网关在 jiuwenbox 沙箱中执行"}
    else:
        state = {"sandbox": "unavailable", "detail": f"{status['detail']}；脚本运行验证将在 onto-modeling 容器内执行"}
    _emit(json.dumps({"gateway": status["gateway"], **state}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ontology/scripts 脚本运行验证（jiuwenbox 沙箱优先）")
    parser.add_argument("--root", default=".", help="工作区根目录（默认当前目录）")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="按用例运行脚本，全部通过才记录验证结果")
    run.add_argument("script", help="ontology/scripts/<脚本>.py")
    run.add_argument(
        "--cases", required=True,
        help='用例 JSON 文件：[{"name": "...", "argv": ["--flag", "值"], "expect": "success" 或 "error", '
        '"stdout_contains": ["预期结论"]}]；参数里有编号的脚本还要有一个不带任何编号、写了 stdout_contains 的成功用例',
    )
    check = commands.add_parser("check", help="检查脚本是否有与当前源码一致的通过记录（pre-commit 门禁）")
    check.add_argument("scripts", nargs="+", help="改动过的 ontology/scripts/*.py")
    commands.add_parser("status", help="脚本验证网关与 jiuwenbox 沙箱是否可用")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    handlers = {"run": command_run, "check": command_check, "status": command_status}
    handler = handlers.get(args.command)
    if handler is None:
        parser.error(f"未知命令：{args.command}")
    try:
        return handler(args, root)
    except (UsageError, sandbox.BundleError) as exc:
        _emit(f"错误：{exc}", sys.stderr)
        return 2
    except sandbox.SandboxError as exc:
        _emit(f"执行器失败：{exc}", sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
