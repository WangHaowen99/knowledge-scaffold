"""脚本与可代码化候选、沙箱验证（契约 3.18 SC-01～SC-03；详设 5.9.8「代码生成与求解」页）。

脚本属于五元组，内核本来就能读：契约检查与描述符同步按本项目锁定版本的共享契约库算（与提交检查、
agent 跑的是同一份）；候选来自最近一份代码生成扫描报告（代码生成与求解包的 /ontology-codegen-scan）；
沙箱验证结果是 script_verify 记下的通过记录（源码哈希对不上就是没验证过）。扫描、生成、修复、重新生成
都只发命令（S-20），不在这里。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request

from ..api import deps
from ..api.errors import ApiError

router = APIRouter(tags=["scripts"])

SCRIPTS_DIR = "ontology/scripts"
VERIFY_RECORDS = ".ontology-state/script-verifications.json"
SCAN_GLOB = "*-codegen-candidates.md"


def _entry(kind: str, key: str) -> dict:
    return {"kind": kind, "id": f"{kind}:{key}", "key": key, "name": key}


def _sandbox_summary() -> dict:
    from ..services import sandbox_status

    status = sandbox_status.status()
    return {"available": bool(status.get("connected")), "network": "denied", "python": status.get("python") or None,
            "checked_at": deps.iso(datetime.now().timestamp())}


def _latest_scan(root: Path) -> tuple[Path | None, dict]:
    from ..services import report_items

    folder = root / "reports"
    reports = [path for path in sorted(folder.glob(SCAN_GLOB)) if not path.is_symlink()] \
        if folder.is_dir() and not folder.is_symlink() else []
    if not reports:
        return None, {}
    latest = reports[-1]
    try:
        return latest, report_items.parse_codegen(latest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return latest, {}


def _candidates(root: Path, existing: set[str]) -> dict:
    path, parsed = _latest_scan(root)
    items = []
    for item in parsed.get("candidates") or []:
        suitable = "yes" if item.get("verdict") == "适合" else "partial"
        items.append({"operation": _entry("operation", item["operation"]),
                      "reason": item.get("note") or item.get("type") or "", "suitable": suitable,
                      "selected_default": suitable == "yes" and item["operation"] not in existing})
    return {
        "scanned_at": deps.iso(path.stat().st_mtime) if path else None,
        "items": items,
        "unsuitable": [{"operation": _entry("operation", item["operation"]), "reason": item.get("reason") or ""}
                       for item in parsed.get("unsuitable") or []],
    }


def _verify_records(root: Path) -> dict:
    try:
        data = json.loads((root / VERIFY_RECORDS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return scripts if isinstance(scripts, dict) else {}


def _verified(root: Path, rel: str, record: dict | None) -> bool:
    """通过记录还有效：脚本与它依赖的模块没再改过（与提交检查 script_verify check 同口径）。"""
    from ..services.packs import contracts

    if not record:
        return False
    try:
        return contracts.module_for(root, "jiuwenbox_client").source_hash(root / rel) == record.get("source_hash")
    except Exception:  # noqa: BLE001 - 算不出哈希按没验证过显示
        return False


def _rows(root: Path) -> list[dict]:
    from ..services.packs import contracts

    scripts_dir = root / SCRIPTS_DIR
    if not scripts_dir.is_dir():
        return []
    descriptor = contracts.module_for(root, "script_descriptor")
    script_contract = contracts.module_for(root, "script_contract")
    try:
        issues = descriptor.descriptor_issues(root / "ontology")
    except Exception as exc:  # noqa: BLE001 - 契约库出错时整页不该 500，逐行显示「契约检查失败」
        issues = [("scripts", "错误", f"契约检查没能运行：{type(exc).__name__}")]
    records = _verify_records(root)
    rows = []
    for path in sorted(scripts_dir.glob("*.py")) if not scripts_dir.is_symlink() else []:
        if path.name.startswith("_") or path.is_symlink():
            continue
        name = path.stem
        rel = path.relative_to(root).as_posix()
        mine = [item for item in issues if item[0] in (f"scripts/{name}.py", "scripts")]
        findings = [{"code": "script_contract", "level": "error" if severity == "错误" else "warning",
                     "message": message, "path": rel} for _, severity, message in mine]
        doc = [item for item in issues if item[0] == f"scripts/{name}.md"]
        try:
            operation = script_contract.script_bound_operation(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            operation = ""
        if not operation:
            state = "none"
        elif any("缺少" in message for _, _, message in doc):
            state = "none"
        elif doc:
            state = "stale"
        else:
            state = "synced"
        record = records.get(rel)
        verified = _verified(root, rel, record)
        failed = any(item["level"] == "error" for item in findings)
        actions = [action for action, ok in (("fix", failed), ("regenerate_descriptor", state == "stale" or (operation and state == "none")),
                                             ("regenerate", True)) if ok]
        rows.append({
            "script": _entry("script", name),
            "operation": _entry("operation", operation) if operation else None,
            "contract": {"status": "failed" if failed else "passed", "findings": findings},
            "descriptor": state,
            "sandbox": {"status": "passed" if verified else "not_run", "duration_ms": None,
                        "ran_at": deps.iso((record or {}).get("verified_at")) if verified else None},
            "actions": actions,
        })
    return rows


@router.get("/api/v1/sandbox/status")
async def sandbox_summary(request: Request):
    """SC-03：脚本验证沙箱可不可用（摘要；管理员全量见 SY-10）。"""
    return await asyncio.to_thread(_sandbox_summary)


@router.get("/api/v1/projects/{project_id}/scripts")
async def list_scripts(project_id: str, request: Request):
    """SC-01：脚本、契约检查、描述符、沙箱验证，与最近一次扫描的可代码化候选。"""
    root = deps.require_owner(project_id, request)

    def build() -> dict:
        rows = _rows(root)
        bound = {row["operation"]["key"] for row in rows if row["operation"]}
        return {"sandbox": _sandbox_summary(), "candidates": _candidates(root, bound), "items": rows}

    return await asyncio.to_thread(build)


@router.get("/api/v1/projects/{project_id}/scripts/{key}/verification")
async def script_verification(project_id: str, key: str, request: Request):
    """SC-02：一个脚本最近一次通过的沙箱验证（只记通过的；之后改过脚本就是 not_run）。"""
    root = deps.require_owner(project_id, request)
    rel = f"{SCRIPTS_DIR}/{key}.py"
    if "/" in key or not (root / rel).is_file():
        raise ApiError("not_found", "脚本不存在", 404)
    record = _verify_records(root).get(rel)
    verified = await asyncio.to_thread(_verified, root, rel, record)
    return {
        "script": _entry("script", key),
        "ran_at": deps.iso(record.get("verified_at")) if verified else None,
        "status": "passed" if verified else "not_run",
        "duration_ms": None,
        "network_access": False,
        "cases": [{"args": str(case.get("name") or ""), "output": str(case.get("expect") or ""), "passed": True}
                  for case in (record or {}).get("cases") or []] if verified else [],
    }
