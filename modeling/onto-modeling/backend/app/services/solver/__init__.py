"""求解契约（方案 A10）：工作区侧的检查、视图与保存在 service；契约格式与回执仍在
services/solver_contract.py、services/solver_validation.py（P1 随写入路径一起搬进本包）。"""

from .service import (  # noqa: F401
    check_solver_contract_draft,
    ensure_publishable,
    release_status_at,
    save_solver_contract,
    solver_check,
    solver_check_at,
    solver_contract_view,
    solver_receipt_targets,
    solver_release_status,
)
