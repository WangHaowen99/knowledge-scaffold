"""I1：求解契约缺 Decision Model ID 时后端按 `<solver_key>_model` 补上（SV-02 保存、SV-03 检查；gaps UI 文案第 5 条）。"""

from __future__ import annotations

import yaml

from app.services import publish  # noqa: F401  （先于 solver 加载，避免循环导入）
from app.services import solver as solver_service
from app.services import solver_contract
from tests.test_solver_contract_check import _item, _project, manager  # noqa: F401  （夹具按名字注入）


def test_save_fills_missing_decision_model(manager):
    pid, root = _project(manager)
    item = _item()
    item["binding"].pop("decision_model")
    # Decision Model 文档没写 id：按 `<solver_key>_model`
    item["sidecars"]["entry"] = "".join(line for line in item["sidecars"]["entry"].splitlines(keepends=True)
                                        if not line.startswith("id:"))
    solver_service.save_solver_contract(manager, pid, [item], "")
    binding = yaml.safe_load((root / solver_contract.SOLVER_DIR / "plan_route" / "binding.yaml").read_text(encoding="utf-8"))
    assert binding["decision_model"] == "plan_route_model"


def test_check_does_not_reject_missing_decision_model(manager):
    pid, _root = _project(manager)
    item = _item(decision_model="")
    result = solver_service.check_solver_contract_draft(manager, pid, [item])
    assert not [problem for problem in result["rejected"] if problem.get("field") == "decision_model"]


def test_missing_decision_model_takes_the_model_document_id(manager):
    pid, root = _project(manager)
    item = _item()
    item["binding"].pop("decision_model")
    assert "id: route_plan" in item["sidecars"]["entry"]
    solver_service.save_solver_contract(manager, pid, [item], "")
    binding = yaml.safe_load((root / solver_contract.SOLVER_DIR / "plan_route" / "binding.yaml").read_text(encoding="utf-8"))
    assert binding["decision_model"] == "route_plan"
