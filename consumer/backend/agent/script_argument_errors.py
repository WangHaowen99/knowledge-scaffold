"""Bounded public diagnostics for script argument evidence validation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from utils.security.redaction import redact_sensitive_text


SCRIPT_ARGUMENT_VALIDATION_FAILED = "SCRIPT_ARGUMENT_VALIDATION_FAILED"
_ISSUE_CODES = frozenset({
    "ungrounded_identity",
    "invalid_generated_identifier",
    "request_field_mismatch",
    "request_item_order",
    "request_record_mismatch",
    "evidence_record_mismatch",
})
_MESSAGE = "Script arguments failed validation; the script did not run."
_PATH_PATTERN = re.compile(r"^arguments(?:\.[\w-]{1,64}|\[\d{1,6}\])*$")


def safe_script_argument_error(value: Any) -> dict[str, Any] | None:
    """Allow only our error category, field paths and correction flag across boundaries."""
    if not isinstance(value, Mapping):
        return None
    if value.get("code") != SCRIPT_ARGUMENT_VALIDATION_FAILED:
        return None
    raw_issues = value.get("issues")
    if not isinstance(raw_issues, list):
        return None
    issues = []
    for issue in raw_issues[:20]:
        if not isinstance(issue, Mapping):
            continue
        code = issue.get("code")
        if not isinstance(code, str) or code not in _ISSUE_CODES:
            continue
        path = str(issue.get("path") or "")
        if (
            len(path) > 240
            or not _PATH_PATTERN.fullmatch(path)
            or redact_sensitive_text(path) != path
        ):
            path = "arguments"
        issues.append({"code": issue["code"], "path": path})
    if not issues:
        return None
    issues = [
        {"code": code, "path": path}
        for code, path in sorted({(issue["code"], issue["path"]) for issue in issues})
    ]
    return {
        "code": SCRIPT_ARGUMENT_VALIDATION_FAILED,
        "message": _MESSAGE,
        "correctable": True,
        "issues": issues,
    }


def script_argument_error_content(issues: list[dict[str, str]]) -> str:
    error = safe_script_argument_error({
        "code": SCRIPT_ARGUMENT_VALIDATION_FAILED,
        "issues": issues,
    })
    return json.dumps({
        "ok": False,
        "error": error,
        "guidance": (
            "Correct the indicated fields using the current request or successful "
            "tool results. Preserve record associations and item order. Only "
            "declared generated identifiers may use new UUID4 values. Do not "
            "claim execution or calculated results until the script succeeds."
        ),
    }, ensure_ascii=False)
