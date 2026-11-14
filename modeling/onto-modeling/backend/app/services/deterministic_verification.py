"""在建模侧验证确定性 Parser → Schema → Handler 执行闭包。

用户生成的 Python 只经现有脚本验证网关送入 JiuwenBox；本模块不在 Web 进程或主容器
回退执行。workspace_provider 用例使用测试向量内的合成白名单返回值，不连接真实数据源。
"""

from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from . import deterministic_contract
from .packs import contracts as contract_modules
from .workspace.errors import WorkspaceError

_sandbox = contract_modules.default().load("jiuwenbox_client")
_script_contract = contract_modules.default().load("script_contract")


def _json_equal(left: Any, right: Any) -> bool:
    return deterministic_contract._json_equal(left, right)  # pylint: disable=protected-access


def _bundle(files: Mapping[str, bytes]) -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    if len(encoded) > _sandbox.MAX_ARGUMENT_BYTES:
        raise WorkspaceError("确定性执行验证闭包超过沙箱参数上限", 422, "deterministic_verification_too_large")
    return encoded


def _parser_bundle(root: Path, item: deterministic_contract.DeterministicContract) -> tuple[str, str]:
    entry = f"{item.key}_parser.py"
    files = {entry: item.parser}
    for path in deterministic_contract._parser_dependencies(root, item.parser):  # pylint: disable=protected-access
        files[path.name] = path.read_bytes()
    return _bundle(files), entry


def _provider_stub(fixtures: list[dict]) -> bytes:
    operation_arguments = {
        name: sorted(arguments)
        for name, arguments in deterministic_contract.PROVIDER_OPERATION_ARGUMENTS.items()
    }
    source = '''"""合成 Provider 验证替身；仅存在于一次性沙箱包中。"""
import json

_FIXTURES = json.loads({fixtures!r})
_OPERATION_ARGUMENTS = json.loads({operation_arguments!r})


def _json_equal(left, right):
    pending = [(left, right)]
    while pending:
        first, second = pending.pop()
        if isinstance(first, bool) or isinstance(second, bool):
            if not isinstance(first, bool) or not isinstance(second, bool) or first is not second:
                return False
            continue
        first_number = isinstance(first, (int, float)) and not isinstance(first, bool)
        second_number = isinstance(second, (int, float)) and not isinstance(second, bool)
        if first_number or second_number:
            if not first_number or not second_number or first != second:
                return False
            continue
        if type(first) is not type(second):
            return False
        if isinstance(first, dict):
            if set(first) != set(second):
                return False
            pending.extend((first[key], second[key]) for key in first)
        elif isinstance(first, list):
            if len(first) != len(second):
                return False
            pending.extend(zip(first, second))
        elif first != second:
            return False
    return True


class DeterministicProviderCallError(RuntimeError):
    def __init__(self, code, message):
        self.code = str(code or "provider_call_failed")
        super().__init__(str(message or "Deterministic provider call failed."))


class DeterministicProviderClient:
    def __init__(self, *args, **kwargs):
        pass

    def execute(self, operation, arguments=None):
        arguments = dict(arguments or {{}})
        allowed = _OPERATION_ARGUMENTS.get(operation)
        if allowed is None or set(arguments) - set(allowed):
            raise DeterministicProviderCallError(
                "provider_arguments_invalid", "Deterministic provider arguments are invalid."
            )
        for item in _FIXTURES:
            if item["operation"] == operation and _json_equal(item["arguments"], arguments):
                return item["result"]
        raise DeterministicProviderCallError(
            "provider_query_failed", "Synthetic deterministic provider fixture is missing."
        )
'''.format(
        fixtures=json.dumps(fixtures, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        operation_arguments=json.dumps(operation_arguments, sort_keys=True, separators=(",", ":")),
    )
    return source.encode("utf-8")


def _handler_bundle(
    root: Path, item: deterministic_contract.DeterministicContract, fixtures: list[dict],
) -> tuple[str, str]:
    files = {path.name: path.read_bytes() for path in deterministic_contract._handler_dependencies(root, item.handler)}  # pylint: disable=protected-access
    entry = f"{item.handler}.py"
    if item.data_access == "workspace_provider":
        files["shared_capabilities/__init__.py"] = b""
        files["shared_capabilities/ontology_script_sdk/__init__.py"] = _provider_stub(fixtures)
        files["shared_capabilities/ontology_script_sdk/deterministic_provider.py"] = _provider_stub(fixtures)
    return _bundle(files), entry


def _json_output(result: Mapping[str, Any], *, role: str) -> tuple[Any | None, str]:
    if result.get("timed_out"):
        return None, f"{role} 执行超时"
    if result.get("exit_code") != 0:
        return None, f"{role} 退出码为 {result.get('exit_code')}：{str(result.get('stderr') or '')[-300:]}"
    if not result.get("stdout_is_json"):
        return None, f"{role} stdout 不是单个 JSON"
    try:
        return deterministic_contract._strict_json(  # pylint: disable=protected-access
            str(result.get("stdout") or ""), f"{role} stdout",
        ), ""
    except ValueError:
        return None, f"{role} stdout 不是严格 JSON（不允许重复字段、NaN/Infinity 或超出有限范围的数值）"


def _parser_result(value: Any) -> str:
    if not isinstance(value, Mapping) or set(value) != {"matched", "arguments", "issues"}:
        return "Parser 输出必须且只能包含 matched、arguments、issues"
    if not isinstance(value.get("matched"), bool) or not isinstance(value.get("arguments"), Mapping):
        return "Parser 的 matched 必须是 boolean，arguments 必须是 object"
    if not isinstance(value.get("issues"), list) or not all(isinstance(issue, Mapping) for issue in value["issues"]):
        return "Parser 的 issues 必须是 object 数组"
    runtime_fields = sorted(
        (
            str(field) for field in value["arguments"]
            if str(field).replace("-", "_")
            in deterministic_contract._CONSUMER_RUNTIME_ARGUMENT_FIELDS  # pylint: disable=protected-access
        ),
        key=lambda item: item.encode("utf-8"),
    )
    for issue in value["issues"]:
        field = issue.get("field")
        if not isinstance(field, str) or not field.strip():
            return "Parser issue.field 必须是非空字符串"
        if field.replace("-", "_") in deterministic_contract._CONSUMER_RUNTIME_ARGUMENT_FIELDS:  # pylint: disable=protected-access
            runtime_fields.append(field)
    if runtime_fields:
        return "Parser 不能返回 Consumer 保留的运行时参数：" + "、".join(sorted(set(runtime_fields)))
    return ""


def _parameter_keys(parameter: Mapping[str, Any]) -> tuple[str, ...]:
    """返回 Consumer 从发布态脚本描述符接受的参数名变体。"""
    values: list[str] = []

    def add(value: Any, *, strip_flag: bool = False) -> None:
        text = str(value or "").strip()
        if not text:
            return
        candidates = [text]
        if strip_flag and text.startswith("-"):
            candidates.append(text.lstrip("-"))
        for candidate in list(candidates):
            candidates.extend((candidate.replace("-", "_"), candidate.replace("_", "-")))
        for candidate in candidates:
            if candidate and candidate not in values:
                values.append(candidate)

    add(parameter.get("name"))
    add(parameter.get("flag"), strip_flag=True)
    for alias in parameter.get("aliases") or ():
        add(alias)
    add(parameter.get("desc") or parameter.get("description"))
    return tuple(values)


def _canonical_cli_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )


def _convert_cli_value(value: Any, parameter: Mapping[str, Any]) -> tuple[str, str]:
    """按 Consumer 确定性执行的结构化参数规则生成一个 CLI 值。"""
    kind = str(parameter.get("type") or "string").strip().lower()
    if kind == "string" and isinstance(value, (list, tuple, set, dict)):
        serializable = list(value) if isinstance(value, (tuple, set)) else value
        try:
            return _canonical_cli_json(serializable), ""
        except (TypeError, ValueError):
            return "", "应为字符串或可编码为 JSON 的结构化值"
    if kind in {"array", "list"} or kind.endswith("[]"):
        parsed = value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return "", "应为数组"
        if isinstance(parsed, (tuple, set)):
            parsed = list(parsed)
        if not isinstance(parsed, list):
            return "", "应为数组"
        try:
            return _canonical_cli_json(parsed), ""
        except (TypeError, ValueError):
            return "", "应为数组"
    if kind in {"object", "dict"}:
        parsed = value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return "", "应为对象"
        if not isinstance(parsed, dict):
            return "", "应为对象"
        try:
            return _canonical_cli_json(parsed), ""
        except (TypeError, ValueError):
            return "", "应为对象"
    if kind == "json":
        parsed = value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return "", "应为 JSON"
        try:
            return _canonical_cli_json(parsed), ""
        except (TypeError, ValueError):
            return "", "应为 JSON"
    if kind in {"int", "integer"}:
        try:
            return str(int(value)), ""
        except (TypeError, ValueError, OverflowError):
            return "", "应为整数"
    if kind in {"float", "number"}:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return "", "应为数字"
        if not isinstance(value, bool) and number.is_integer():
            return (str(value) if isinstance(value, int) else str(int(number))), ""
        return str(number), ""
    if kind in {"bool", "boolean"}:
        if isinstance(value, bool):
            return str(value).lower(), ""
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return "true", ""
        if normalized in {"false", "0", "no", "n"}:
            return "false", ""
        return "", "应为布尔值"
    return str(value), ""


def _append_cli_value(argv: list[str], parameter: Mapping[str, Any], flag: str, value: str) -> None:
    kind = str(parameter.get("type") or "string").strip().lower()
    action = str(parameter.get("action") or "").strip().lower()
    if kind in {"bool", "boolean"} and flag:
        if action == "store_true":
            if value == "true":
                argv.append(flag)
            return
        if action == "store_false":
            if value == "false":
                argv.append(flag)
            return
    if flag:
        argv.append(flag)
    argv.append(value)


def _handler_argv(source: str, arguments: Mapping[str, Any]) -> tuple[list[str], str]:
    try:
        contract = _script_contract.parse_script(source)
    except ValueError as exc:
        return [], f"Handler 参数契约无效：{exc}"
    argv, consumed = [], set()
    for parameter in contract.params:
        name = str(parameter.get("name") or "")
        flag = str(parameter.get("flag") or "").strip()
        candidates = _parameter_keys(parameter)
        matched = next((candidate for candidate in candidates if candidate in arguments), None)
        required = str(parameter.get("required") or "否") == "是"
        value = arguments.get(matched) if matched is not None else None
        if matched is not None:
            consumed.add(matched)
        if value is None:
            if required:
                return [], f"Handler 必填参数 {name} 缺失"
            continue
        multiple = str(parameter.get("multiple") or "否") == "是"
        values = [part for part in value if part is not None] if multiple and isinstance(value, list) else [value]
        if multiple and not values:
            if required:
                return [], f"Handler 必填参数 {name} 缺失"
            continue
        kind = str(parameter.get("type") or "string").strip().lower()
        if not multiple and isinstance(value, (list, tuple, set, dict)) and kind not in {
            "string", "array", "list", "object", "dict", "json",
        } and not kind.endswith("[]"):
            return [], f"Handler 参数 {name} 不接受多个值"
        for part in values:
            converted, reason = _convert_cli_value(part, parameter)
            if reason:
                return [], f"Handler 参数 {name}{reason}"
            _append_cli_value(argv, parameter, flag, converted)
    unknown = sorted(key for key, value in arguments.items() if value is not None and key not in consumed)
    if unknown:
        return [], "Handler 未声明参数：" + "、".join(unknown)
    return argv, ""


def verify(root: Path, key: str, expected_revision: str) -> tuple[str, dict]:
    check = deterministic_contract.check_at(root)
    if check.findings:
        raise WorkspaceError("确定性执行契约未通过静态检查", 422, "deterministic_contract_rejected")
    item = next((value for value in check.contracts if value.key == key and value.enabled), None)
    if item is None:
        raise WorkspaceError("确定性执行契约不存在或已停用", 404, "deterministic_contract_not_found")
    if expected_revision != check.revision:
        raise WorkspaceError("确定性执行契约已变化，请刷新后重试", 409, "deterministic_contract_revision_conflict")
    vectors, findings = deterministic_contract._test_vectors(item)  # pylint: disable=protected-access
    if findings:
        raise WorkspaceError(findings[0]["message"], 422, findings[0]["code"])

    source_revision = deterministic_contract.source_revision(root, item)
    input_schema = json.loads(item.input_schema)
    output_schema = json.loads(item.output_schema)
    parser_payloads = [
        json.dumps({
            "text": vector["text"],
            "known_arguments": dict(vector.get("known_arguments") or {}),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for vector in vectors
    ]
    parser_bundle, parser_entry = _parser_bundle(root, item)
    try:
        parser_runs = _sandbox.run_in_gateway(
            parser_bundle, parser_entry,
            [["--request-json", payload] for payload in parser_payloads],
        )
    except (_sandbox.SandboxUnavailable, _sandbox.SandboxError, _sandbox.BundleError) as exc:
        raise WorkspaceError(
            f"JiuwenBox 验证不可用：{exc}", 503, "deterministic_verification_unavailable",
        ) from exc

    case_reports, handler_cases, handler_indexes, fixture_sets = [], [], [], []
    for index, (vector, run) in enumerate(zip(vectors, parser_runs)):
        parsed, reason = _json_output(run, role="Parser")
        reason = reason or _parser_result(parsed)
        if not reason and parsed.get("matched") != vector.get("expected_matched", True):
            reason = "Parser matched 与预期不一致"
        if not reason and not _json_equal(
            dict(parsed.get("arguments") or {}), dict(vector.get("expected_arguments") or {}),
        ):
            reason = "Parser arguments 与预期不一致"
        if not reason and not _json_equal(
            list(parsed.get("issues") or []), list(vector.get("expected_issues") or []),
        ):
            reason = "Parser issues 与预期不一致"
        report = {"name": vector["name"], "ok": not reason, "stage": "parser", "reason": reason}
        case_reports.append(report)
        if reason or parsed.get("matched") is not True:
            continue
        known_arguments = dict(vector.get("known_arguments") or {})
        unresolved_issues = [
            issue for issue in parsed.get("issues") or []
            if not isinstance(issue, Mapping)
            or str(issue.get("field") or "").strip() not in known_arguments
        ]
        if unresolved_issues:
            continue
        arguments = {**dict(parsed["arguments"]), **known_arguments}
        try:
            errors = sorted(
                Draft202012Validator(input_schema, format_checker=FormatChecker()).iter_errors(arguments),
                key=lambda value: list(value.path),
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            report.update(
                ok=False,
                stage="input_schema",
                reason=f"输入 Schema 无法安全求值：{type(exc).__name__}",
            )
            continue
        if errors:
            report.update(ok=False, stage="input_schema", reason="输入参数不符合 Schema：" + errors[0].message)
            continue
        handler_source = (root / "ontology" / "scripts" / f"{item.handler}.py").read_text(encoding="utf-8")
        argv, reason = _handler_argv(handler_source, arguments)
        if reason:
            report.update(ok=False, stage="handler_arguments", reason=reason)
            continue
        handler_indexes.append(index)
        # 同一个冻结闭包与输入连续执行两次。仅验证“在当前沙箱和合成 Provider 数据下结果一致”，
        # 不把 workspace_provider 的真实业务数据宣称为不可变快照。
        handler_cases.extend((argv, list(argv)))
        fixture_sets.append([dict(value) for value in vector.get("provider_fixtures") or []])

    if handler_cases:
        handler_runs = []
        try:
            if item.data_access == "workspace_provider":
                # Provider fixture 按用例隔离：不让一个用例声明的白名单返回值被另一个用例意外复用。
                for position, fixtures in enumerate(fixture_sets):
                    handler_bundle, handler_entry = _handler_bundle(root, item, fixtures)
                    handler_runs.extend(_sandbox.run_in_gateway(
                        handler_bundle, handler_entry,
                        handler_cases[position * 2:position * 2 + 2],
                    ))
            else:
                handler_bundle, handler_entry = _handler_bundle(root, item, [])
                # 网关每批最多 16 次执行；每条向量要运行两次，16 条向量共 32 次。
                # 保留相邻重复执行对及结果顺序，不扩大网关的资源上限。
                for offset in range(0, len(handler_cases), 16):
                    handler_runs.extend(_sandbox.run_in_gateway(
                        handler_bundle, handler_entry, handler_cases[offset:offset + 16],
                    ))
        except (_sandbox.SandboxUnavailable, _sandbox.SandboxError, _sandbox.BundleError) as exc:
            raise WorkspaceError(
                f"JiuwenBox 验证不可用：{exc}", 503, "deterministic_verification_unavailable",
            ) from exc
        for position, index in enumerate(handler_indexes):
            vector, report = vectors[index], case_reports[index]
            first, second = handler_runs[position * 2:position * 2 + 2]
            output, reason = _json_output(first, role="Handler")
            repeated, repeated_reason = _json_output(second, role="Handler 重复执行")
            reason = reason or repeated_reason
            if not reason and not _json_equal(output, repeated):
                reason = "Handler 在相同输入下重复执行结果不一致"
            if not reason:
                try:
                    first_errors = sorted(
                        Draft202012Validator(output_schema, format_checker=FormatChecker()).iter_errors(output),
                        key=lambda value: list(value.path),
                    )
                    repeated_errors = sorted(
                        Draft202012Validator(output_schema, format_checker=FormatChecker()).iter_errors(repeated),
                        key=lambda value: list(value.path),
                    )
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    first_errors = []
                    repeated_errors = []
                    reason = f"输出 Schema 无法安全求值：{type(exc).__name__}"
                if first_errors:
                    reason = "Handler 输出不符合 Schema：" + first_errors[0].message
                elif repeated_errors:
                    reason = "Handler 重复执行输出不符合 Schema：" + repeated_errors[0].message
            if not reason and not _json_equal(output, vector.get("expected_output")):
                reason = "Handler 输出与预期不一致"
            report.update(ok=not reason, stage="handler", reason=reason)

    failed = sum(1 for case in case_reports if not case["ok"])
    report = {
        "status": "failed" if failed else "passed",
        "runtime": "jiuwenbox",
        "passed": len(case_reports) - failed,
        "failed": failed,
        "cases": case_reports,
    }
    return source_revision, report
