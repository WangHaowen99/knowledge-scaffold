"""求解契约的工作区操作（方案 A10 的搬迁部分）：静态检查、面板视图、草稿检查、整体保存、
发布门禁与回执可绑定的修订。从 workspace.py 移出；保存的写盘与提交交给写入路径（C1）在 P1 做。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .. import solver_contract
from ..publish.layout import operation_stems, package_scripts
from ..workspace.errors import WorkspaceError

CHECK_LIMIT = 50


def solver_check(manager, project_id: str) -> solver_contract.SolverCheck:
    return solver_check_at(manager.project_root(project_id))

def solver_check_at(root: Path) -> solver_contract.SolverCheck:
    return solver_contract.check_workspace_solver(root, operation_stems(root), package_scripts(root))

def solver_release_status(manager, project_id: str) -> dict:
    """求解契约的静态检查结论与当前修订的验证门禁；普通包返回 enabled=False。"""
    root = manager.project_root(project_id)
    return release_status_at(root, root)

def release_status_at(source_root: Path, state_root: Path | None = None) -> dict:
    """source_root 是契约所在的树（发布时为提交导出的只读树），修订号从它算。

    端到端验证在发布后的独立评测里做（ADR-0098），不拦发布：requires_validation_after_publish
    提示发布时要确认求解能力尚未验证。state_root 只为旧调用方保留，不再使用。
    """
    check = solver_check_at(source_root)
    if not check.enabled:
        return {"enabled": False}
    limit = CHECK_LIMIT
    return {
        "enabled": True,
        "revision": check.revision,
        "digests": check.digests,
        "operations": [
            {
                "solver_key": item.solver_key,
                "operation": item.operation,
                "decision_model": item.decision_model,
                "enabled": item.enabled,
                "evaluator": check.evaluators.get(item.solver_key, ""),
            }
            for item in check.contracts
        ],
        "findings": check.findings[:limit],
        "blocking": len(check.findings),
        "requires_validation_after_publish": check.needs_validation,
        "truncated": len(check.findings) > limit,
    }

def solver_contract_view(manager, project_id: str) -> dict:
    """求解模型面板读取：当前修订、全部契约（binding 全量 + sidecar 原文）与检查结论。"""
    root = manager.project_root(project_id)
    check = solver_check_at(root)
    status = release_status_at(root, root)
    # 可选评估器与自动查找同一规则（旧式或声明式），辅助模块、Operation 脚本不列出
    scripts = {
        package_path: path
        for package_path, path in package_scripts(root).items()
        if solver_contract.is_evaluator_file(path)
    }
    return {
        "revision": check.revision,
        "digests": check.digests,
        "enabled": check.enabled,
        "contract": {
            "operations": [
                {
                    **solver_contract.contract_view(item),
                    "evaluator": check.evaluators.get(item.solver_key, ""),
                    "unit_options": solver_contract.evaluator_unit_options(
                        root / check.evaluators[item.solver_key]
                        if item.solver_key in check.evaluators else None
                    ),
                }
                for item in check.contracts
            ],
        },
        "findings": check.findings,
        "requires_validation_after_publish": bool(status.get("requires_validation_after_publish")),
        "available_operations": operation_stems(root),
        "evaluators": sorted(str(path.relative_to(root)) for path in scripts.values()),
        # 模板参数 evaluator_path 填包内路径（scripts/…），面板按工作区文件名展示
        "evaluator_options": [
            {"path": package_path, "file": str(path.relative_to(root))}
            for package_path, path in sorted(scripts.items())
        ],
        "supported_profiles": list(solver_contract.SUPPORTED_PROFILES),
        "problem_families": solver_contract.problem_family_catalog(),
        "template": solver_contract.NEW_CONTRACT_TEMPLATE,
    }

def check_solver_contract_draft(manager, project_id: str, operations: list) -> dict:
    """求解模型面板的即时检查：未保存的草稿按保存与发布体检的同一套规则检查，不落盘。

    rejected 是保存会被拒的结构问题，findings 是保存后发布检查仍会拦截的问题，
    rendered 按提交顺序给出模板契约将生成的 sidecar 原文（供预览）；
    验证门禁跟修订走，草稿保存后修订必然改变，这里不给出。
    """
    root = manager.project_root(project_id)
    stems, scripts = operation_stems(root), package_scripts(root)
    rejected, findings, rendered = solver_contract.check_draft(operations, stems, root, scripts)
    if not rejected:
        staged = solver_contract.stage_contracts(operations, stems)
        rejected += _deterministic_conflicts(
            root, solver_contract.staged_enabled_operations(staged, stems)
        )
    evaluators = solver_contract.draft_evaluators(operations, stems, root, scripts)
    first = next(iter(evaluators.values()), "")
    return {"rejected": rejected, "findings": findings, "rendered": rendered,
            "evaluator": solver_contract.evaluator_summary(root / first) if first else None}


def _deterministic_conflicts(root: Path, solver_operations: set[str]) -> list[dict]:
    """把共享执行模式互斥结论转换为 Solver 面板的稳定 finding。"""
    from .. import deterministic_contract

    findings = deterministic_contract.execution_mode_conflicts(
        deterministic_contract.enabled_operations(root), solver_operations,
    )
    return [
        solver_contract.finding(solver_contract.SolverContractError(
            solver_contract.CODE_EXECUTION_MODE_CONFLICT,
            item["message"],
            field="operation",
        ))
        for item in findings
    ]

def save_solver_contract(
    manager, project_id: str, operations: list, base_revision: str,
    author: str = "", summary: str = "",
) -> dict:
    """整体保存求解契约（受管目录全量替换），基线修订不一致返回 409。

    结构不合法抛 SolverContractRejected；语义问题（Profile、评估器等）不阻止保存，
    随返回的 findings 给出，发布时再由门禁拦截。
    """
    from ...kernel.writepath import ChangeTx, Origin

    root = manager.project_root(project_id)
    with ChangeTx(manager, project_id, Origin("web", author), summary="保存求解契约") as tx:
        current = solver_check_at(root).revision
        if (base_revision or "") != current:
            raise WorkspaceError(
                "求解契约已被他人或助手修改，请刷新对比后再保存", 409,
                code="solver_contract_revision_conflict",
            )
        stems = operation_stems(root)
        staged = solver_contract.stage_contracts(operations, stems)
        conflicts = _deterministic_conflicts(
            root, solver_contract.staged_enabled_operations(staged, stems),
        )
        if conflicts:
            raise solver_contract.SolverContractRejected([
                solver_contract.SolverContractError(
                    item["code"], item["message"], field=item.get("field", "operation"),
                )
                for item in conflicts
            ])
        base = root / solver_contract.SOLVER_DIR
        if base.is_dir():
            for directory in base.iterdir():
                if directory.name not in staged:
                    if directory.is_dir() and not directory.is_symlink():
                        shutil.rmtree(directory)
                    else:
                        directory.unlink()
        for solver_key, files in staged.items():
            directory = base / solver_key
            if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
                directory.unlink()
            directory.mkdir(parents=True, exist_ok=True)
            for path in directory.iterdir():
                if path.name not in files:
                    shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
            for filename, data in files.items():
                target = directory / filename
                existing = target.read_bytes() if target.is_file() else None
                if existing is not None and solver_contract.same_content(filename, existing, data):
                    # 导入时原样保存的 binding 不因一次无改动的保存被重排格式
                    continue
                manager._atomic_write(target, data)
        if base.is_dir() and not any(base.iterdir()):
            base.rmdir()
        # 受管目录整体提交，只含求解契约目录（方案 A10：保存的写盘和提交交给写入路径）
        tx.touch(solver_contract.SOLVER_DIR, snapshot=False)
        who = f" by {author}" if author else ""
        note = f"：{' '.join(summary.split())[:80]}" if summary.strip() else ""
        commit = tx.commit(f"web: 保存求解契约{note}{who}").commit
    return {**solver_contract_view(manager, project_id), "commit": commit}

def ensure_publishable(source_root: Path, state_root: Path | None = None) -> None:
    """发布只受求解契约静态检查约束；端到端验证在发布后的独立评测里做。"""
    blocking = release_status_at(source_root).get("findings") or []
    if blocking:
        raise WorkspaceError(
            "求解契约未通过发布检查：" + "；".join(item["message"] for item in blocking[:3]),
            code=blocking[0]["code"],
        )
