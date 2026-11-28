"""求解契约（方案 A10）：工作区侧的检查、视图与保存在 service；契约格式在 services/solver_contract.py。

端到端验证在发布后的独立评测里做（ADR-0098），本地不再收验证回执。
"""

from .service import (  # noqa: F401
    check_solver_contract_draft,
    ensure_publishable,
    legacy_validation_cases,
    release_status_at,
    save_solver_contract,
    solver_check,
    solver_check_at,
    solver_contract_view,
    solver_release_status,
)
