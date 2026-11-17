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
_RENDER_CODES = ("script_binding_conflict", "script_argument_shape", "relation_structure_invalid")
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
            problems = (exc.details or {}).get("problems") if code == "relation_structure_invalid" else None
            if problems:
                # 关系结构逐条：哪个对象、第几行、什么问题（界面按对象列，路径只用来打开文档）
                return [{
                    "code": code, "level": "error", "message": item["message"], "path": item["path"],
                    "line": item.get("line"),
                    "target": {"kind": "object", "id": f"object:{item['object']}",
                               "key": item["object"], "name": item["object"]},
                } for item in problems]
            return [{"code": code, "level": "error", "message": str(exc)}]
    return []


def _binding_gate(issues: list[dict], objects: dict | None = None) -> dict:
    blocking = [item for item in issues if item["blocking"]]
    warnings = [item for item in issues if not item["blocking"]]
    # 没绑表、也没声明不落库的对象（数据问答查不到它们）：不挡发布，但不能说成「都已绑定」
    unbound = [key for key, item in (objects or {}).items() if not item["table"] and not item.get("declared_none")]
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
    elif unbound:
        status, summary = "warn", f"{len(unbound)} 个对象没有绑定数据表，数据问答查不到它们"
    else:
        status, summary = "pass", "对象都已绑定数据表或声明不落库"
    return {
        "id": "data_binding", "order": 1, "title": "数据绑定", "source": _PLATFORM,
        "status": status, "summary": summary, "blocking": bool(blocking), "degradable": True,
        "findings": findings, "action": None,
    }


def _solver_gate(solver: dict) -> dict | None:
    """求解契约静态检查；端到端验证在发布后的独立评测里做，发布时只要确认（ADR-0098）。"""
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


def _structure_summary(findings: list[dict]) -> str:
    if not findings:
        return "脚本绑定、入参形态、文档元数据与 consumer 导入规则都通过"
    first = findings[0]
    name = (first.get("target") or {}).get("name")
    if len(findings) == 1:
        return f"「{name}」{first['message']}" if name else first["message"]
    if all(item.get("code") == "relation_structure_invalid" for item in findings):
        return f"{len(findings)} 处对象关系写法有误，consumer 导入会拒绝"
    return f"{len(findings)} 个问题，consumer 导入会拒绝"


def _structure_gate(findings: list[dict], order: int) -> dict:
    return {
        "id": "package_structure", "order": order, "title": "知识包结构", "source": _PLATFORM,
        "status": "fail" if findings else "pass",
        "summary": _structure_summary(findings),
        "blocking": bool(findings), "degradable": False, "findings": findings, "action": None,
    }


def _stages_gate(stages: dict | None) -> dict | None:
    """建模阶段（ADR-0098，与 develop 同规则）：缺阶段时才出现，挡发布、不能降级。"""
    if not stages or stages.get("ready", True):
        return None
    from .stages import CODE, message

    text = message(stages["missing_stages"])
    return {
        "id": "modeling_stages", "order": 1, "title": "建模阶段", "source": _PLATFORM,
        "status": "fail", "summary": text, "blocking": True, "degradable": False,
        "findings": [{"code": CODE, "level": "error", "message": text}],
        "action": None, "missing_stages": stages["missing_stages"],
    }


def gate_chain(issues: list[dict], solver: dict, structure: list[dict], tree_error: dict | None = None,
               packs: list[dict] | None = None, stages: dict | None = None, objects: dict | None = None) -> list[dict]:
    stage_gate = _stages_gate(stages)
    chain = [stage_gate] if stage_gate else []
    chain.append({**_binding_gate(issues, objects), "order": len(chain) + 1})
    solver_gate = _solver_gate(solver)
    if solver_gate:
        chain.append({**solver_gate, "order": len(chain) + 1})
    chain.append(_structure_gate([*([tree_error] if tree_error else []), *structure], len(chain) + 1))
    for gate in packs or []:
        chain.append({**gate, "order": len(chain) + 1})
    return chain


def blocking(chain: list[dict], degrade: bool = False) -> list[dict]:
    """不通过的门禁；降级发布只放宽数据绑定。"""
    return [gate for gate in chain if gate["blocking"] and not (degrade and gate["degradable"])]
