"""发布门禁链（详设 5.6、㉖；API 契约 PB-01 的 gates、7.14 GateResult）。

平台自带三道：数据绑定（可降级）、求解契约（静态检查）、知识包结构（脚本绑定、入参形态、
文档元数据、发布树安全）；包声明的门禁（on 含 publish，`services/packs/extensions.publish_gates`）按包排在
后面。证据出处与流程关卡随证据源、发布节点接进来。
"""

from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path

from ..workspace.errors import WorkspaceError
from .spi import ReleaseSource

# 渲染时才能发现的结构问题：与发布同口径，按降级发布渲染一次（数据绑定单独成一道门禁）
_RENDER_CODES = ("script_binding_conflict", "script_argument_shape")
_PLATFORM = {"kind": "platform", "label": "平台"}


def render_findings(src: ReleaseSource) -> list[dict]:
    from .ontology_package import ADAPTER

    with tempfile.TemporaryDirectory(prefix="onto-gate-") as scratch:
        try:
            ADAPTER.render(dataclasses.replace(src, allow_unbound=True, binding=None, draft=True), Path(scratch) / "check.zip")
        except WorkspaceError as exc:
            if exc.code.startswith("solver_"):
                return []  # 求解契约问题由求解门禁报
            code = exc.code if exc.code in _RENDER_CODES else "package_render_failed"
            return [{"code": code, "level": "error", "message": str(exc)}]
    return []


def _binding_gate(issues: list[dict]) -> dict:
    blocking = [item for item in issues if item["blocking"]]
    warnings = [item for item in issues if not item["blocking"]]
    findings = [
        {
            "code": item["kind"],
            "level": "error" if item["blocking"] else "warning",
            "message": item["message"],
            "target": {"kind": "object", "id": f"object:{item['object']}", "key": item["object"], "name": item["object"]},
            "path": f"ontology/object/{item['object']}.md",
        }
        for item in issues
    ]
    if blocking:
        status, summary = "fail", f"{len(blocking)} 个对象和数据表对不上"
    elif warnings:
        status, summary = "warn", f"{len(warnings)} 个对象的来源表无从核对，暂不绑定"
    else:
        status, summary = "pass", "对象都已绑定数据表或声明不落库"
    return {
        "id": "data_binding", "order": 1, "title": "数据绑定", "source": _PLATFORM,
        "status": status, "summary": summary, "blocking": bool(blocking), "degradable": True,
        "findings": findings, "action": None,
    }


def _solver_gate(solver: dict) -> dict | None:
    """求解契约静态检查；端到端验证在发布后的独立评测里做，发布时只要确认（ADR-0091）。"""
    if not solver.get("enabled"):
        return None
    findings = [{"level": "error", **item} for item in solver.get("findings") or []]
    if findings:
        status, summary = "fail", findings[0]["message"]
    else:
        status, summary = "pass", "求解契约静态检查通过"
    return {
        "id": "solver_contract", "order": 2, "title": "求解契约", "source": _PLATFORM,
        "status": status, "summary": summary, "blocking": bool(findings), "degradable": False,
        "findings": findings, "action": None,
        "requires_acknowledgement": bool(solver.get("requires_validation_after_publish")),
    }


def _structure_gate(findings: list[dict], order: int) -> dict:
    return {
        "id": "package_structure", "order": order, "title": "知识包结构", "source": _PLATFORM,
        "status": "fail" if findings else "pass",
        "summary": findings[0]["message"] if findings else "脚本绑定、入参形态、文档元数据与 consumer 导入规则都通过",
        "blocking": bool(findings), "degradable": False, "findings": findings, "action": None,
    }


def gate_chain(issues: list[dict], solver: dict, structure: list[dict], tree_error: dict | None = None,
               packs: list[dict] | None = None) -> list[dict]:
    chain = [_binding_gate(issues)]
    solver_gate = _solver_gate(solver)
    if solver_gate:
        chain.append(solver_gate)
    chain.append(_structure_gate([*([tree_error] if tree_error else []), *structure], len(chain) + 1))
    for gate in packs or []:
        chain.append({**gate, "order": len(chain) + 1})
    return chain


def blocking(chain: list[dict], degrade: bool = False) -> list[dict]:
    """不通过的门禁；降级发布只放宽数据绑定。"""
    return [gate for gate in chain if gate["blocking"] and not (degrade and gate["degradable"])]
