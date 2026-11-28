"""Read Agent proposals without granting write access to managed contracts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from .workspace.errors import WorkspaceError

DRAFT_DIR = "ext/deterministic-execution/drafts"
MAX_DRAFT_BYTES = 1536 * 1024
_NON_EXECUTION_MARKER = re.compile(
    r"(?:不(?:想|要|再|应|需|必)?|无需|禁止|别|请勿|不得)(?:再)?(?:执行|运行|调用)"
    r"|\b(?:do\s+not|don't|dont|never)\s+(?:execute|run|invoke)\b",
    re.IGNORECASE,
)


def _routing_findings(contracts) -> list[dict]:
    """Detect overlapping declared routes using business examples, without executing code."""
    from .deterministic_contract import MAX_TESTS_BYTES, MAX_TEST_VECTORS, _normalize_token, _strict_json

    enabled = [contract for contract in contracts if contract.enabled]
    findings = []
    for contract in enabled:
        # Other imported contracts may still lack valid vectors. Keep their routes
        # in the comparison, but only read usable examples; saving remains strict.
        if len(contract.tests) > MAX_TESTS_BYTES:
            continue
        try:
            vectors = _strict_json(contract.tests, "确定性执行测试向量")
        except ValueError:
            continue
        if not isinstance(vectors, list) or len(vectors) > MAX_TEST_VECTORS:
            continue
        for index, vector in enumerate(vectors):
            if not isinstance(vector, dict) or not isinstance(vector.get("text"), str):
                continue
            text = _normalize_token(vector["text"])
            if not vector.get("expected_matched", True) or _NON_EXECUTION_MARKER.search(text):
                continue
            # Rejection examples do not promise a successful unique route. The
            # Consumer rejects ambiguous requests before running either parser.
            # Keep checking examples whose issues are resolved by known inputs.
            known = vector.get("known_arguments", {})
            issues = vector.get("expected_issues", [])
            if isinstance(known, dict) and isinstance(issues, list) and any(
                isinstance(issue, dict)
                and isinstance(issue.get("field"), str)
                and issue["field"].strip()
                and issue["field"].strip() not in known
                for issue in issues
            ):
                continue
            matches = []
            for candidate in enabled:
                all_terms = [_normalize_token(term) for term in candidate.routing_all_terms]
                any_terms = [_normalize_token(term) for term in candidate.routing_any_terms]
                if (all_terms or any_terms) and all(term in text for term in all_terms) and (
                    not any_terms or any(term in text for term in any_terms)
                ):
                    matches.append(candidate.operation)
            if len(matches) > 1:
                findings.append({
                    "code": "deterministic_draft_routing_ambiguous",
                    "field": f"test_vectors[{index}]",
                    "message": (
                        f"「{contract.operation}」示例「{vector.get('name', index)}」同时命中多个操作的触发条件："
                        + "、".join(matches)
                        + "。请收敛触发条件并保留原业务提问，不要只改示例绕过冲突。"
                    ),
                })
    return findings


def _expectation_findings(contracts) -> list[dict]:
    """Preflight expected inputs without executing any model-authored Python."""
    findings = []
    for contract in contracts:
        if not contract.enabled:
            continue
        validator = Draft202012Validator(
            json.loads(contract.input_schema), format_checker=FormatChecker(),
        )
        for index, vector in enumerate(json.loads(contract.tests)):
            known = vector.get("known_arguments", {})
            unresolved = [
                issue for issue in vector.get("expected_issues", [])
                if issue["field"].strip() not in known
            ]
            if not vector.get("expected_matched", True) or unresolved:
                continue
            arguments = {**vector.get("expected_arguments", {}), **known}
            for error in validator.iter_errors(arguments):
                findings.append({
                    "code": "deterministic_draft_expectation_invalid",
                    "field": f"test_vectors[{index}]",
                    "message": (
                        f"test_vectors[{index}]（{vector['name']}）：已确认参数覆盖解析参数后，"
                        f"预期输入不符合 Schema：{error.message}。同字段的解析提示会被已确认参数覆盖；"
                        "请核对测试预期，保留业务约束。"
                    ),
                })
    return findings


def _read(root: Path, relative: str, limit: int = MAX_DRAFT_BYTES) -> bytes:
    path = root
    for part in Path(relative).parts:
        if part in {"..", "."}:
            raise ValueError("草稿来源路径不合法")
        path = path / part
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("草稿和来源不得使用符号链接")
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError("草稿或来源不是普通文件，或超过大小上限")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("草稿来源在读取期间变化")
        value = stream.read(limit + 1)
        final = path.lstat()
        if (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) != (
            opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
        ):
            raise ValueError("草稿来源在读取期间变化")
    if len(value) > limit:
        raise ValueError("草稿或来源超过大小上限")
    return value


def read_proposal(root: Path, operation: str, handler: str, data_access: str) -> dict | None:
    """Load only an explicitly selected candidate; validate all files before import."""
    from . import deterministic_contract as contracts

    if not contracts.HANDLER_RE.fullmatch(handler):
        raise WorkspaceError("草稿 Handler 不合法", 422, "deterministic_draft_invalid")
    relative = f"{DRAFT_DIR}/{handler}.json"
    try:
        content = _read(root, relative)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise WorkspaceError("无法读取助手草稿，请让助手重新生成", 422, "deterministic_draft_invalid") from exc
    try:
        proposal = contracts._strict_json(content, "助手草稿")
        if not isinstance(proposal, dict) or proposal.get("schema_version") != "deterministic-draft.v1":
            raise ValueError("草稿格式必须为 deterministic-draft.v1")
        item = proposal.get("draft")
        if not isinstance(item, dict):
            raise ValueError("缺少 draft 对象")
        if (item.get("operation"), item.get("handler"), item.get("data_access")) != (
            operation, handler, data_access,
        ):
            raise ValueError("草稿与当前选择的 Operation、Handler 或数据访问方式不一致")
        operation_path = contracts._operation_path(root, operation)
        if operation_path is None:
            raise ValueError("Operation 已不存在")
        paths = [operation_path, *contracts._handler_dependencies(root, handler)]
        parser = item.get("parser_source")
        if not isinstance(parser, str):
            raise ValueError("草稿缺少 Parser")
        paths.extend(contracts._parser_dependencies(root, parser.encode("utf-8")))
        current = {
            path.relative_to(root).as_posix(): hashlib.sha256(
                _read(root, path.relative_to(root).as_posix())
            ).hexdigest()
            for path in paths
        }
        if proposal.get("sources") != current:
            raise ValueError("业务操作或脚本已变化，请让助手基于当前文件重新生成草稿")
        # Never accept a model-authored verification record as evidence.
        item = {key: value for key, value in item.items() if key != "verification"}
        existing, findings = contracts.load_contracts(root)
        if findings:
            raise ValueError("已有契约存在问题，请先修复")
        previous = next((value for value in existing if value.operation == operation), None)
        if previous and previous.key != item.get("key"):
            raise ValueError("更新草稿必须保留已有契约标识")
        others = [value for value in existing if value.operation != operation]
        if any(value.key == item.get("key") for value in others):
            raise ValueError("草稿与其他契约标识冲突")
        # A proposal is a read-only repair step, not a full workspace save.
        # Validate the selected draft completely and retain shared constraints.
        check = contracts.check_payload(root, [item])
        findings = list(check.findings)
        if not findings:
            combined = (*others, *check.contracts)
            if len(combined) > contracts.MAX_CONTRACTS:
                raise ValueError(f"确定性 Operation 最多 {contracts.MAX_CONTRACTS} 个")
            findings.extend(contracts.contract_identity_findings(root, combined))
            findings.extend(contracts.solver_conflicts(root, combined))
            findings.extend(_expectation_findings(check.contracts))
            findings.extend(_routing_findings(combined))
        if findings:
            raise WorkspaceError(
                f"助手草稿尚不可用，共 {len(findings)} 项问题，请让助手修复后重新读取",
                422, "deterministic_draft_invalid", {"findings": findings},
            )
        draft = contracts.public_contract(check.contracts[-1])
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise WorkspaceError(
            f"助手草稿尚不可用：{exc}", 422, "deterministic_draft_invalid",
        ) from exc
    return {
        **draft, "verification": {"status": "missing", "current": False},
    }
