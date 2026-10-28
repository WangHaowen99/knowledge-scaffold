# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Stage routing from operation frontmatter ``uses.operations``.

A stage without its own script borrows the script of its single nearest
scripted ancestor, so selecting the stage still runs the complete flow.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import agent.tools.execute_script as execute_script_module
import ontology.operation_execution as operation_execution_module
from agent.tools.execute_script import handle_execute_script
from ontology.ontology_catalog import (
    _annotate_coordinating_operations,
    _format_script_catalog,
    find_script_entry,
    load_operation_relations,
    load_script_catalog,
    public_script_catalog,
)

PARENT = "订单审核"
INVENTORY = "库存校验"
DISCOUNT = "折扣计算"


def _write_operation(ontology_dir: Path, key: str, children=None) -> None:
    operations_dir = ontology_dir / "operations"
    operations_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "resource: operation",
        "key: {}".format(json.dumps(key, ensure_ascii=False)),
        "title: {}".format(json.dumps(key, ensure_ascii=False)),
        "uses:",
        "  objects: []",
    ]
    if children is not None:
        lines.append("  operations: {}".format(json.dumps(children, ensure_ascii=False)))
    lines += ["---", "", "# 业务目标", "", "合成示例流程。", ""]
    (operations_dir / "{}.md".format(key)).write_text("\n".join(lines), encoding="utf-8")


def _write_script(ontology_dir: Path, script_key: str, bound_operation: str) -> Path:
    scripts_dir = ontology_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script_path = scripts_dir / "{}.py".format(script_key)
    script_path.write_text("print('{}')\n", encoding="utf-8")
    descriptor = [
        "---",
        "resource: script",
        "key: {}".format(script_key),
        "title: {}".format(script_key),
        "bound_operation: {}".format(json.dumps(bound_operation, ensure_ascii=False)),
        "runtime: python",
        "entrypoint: {}.py".format(script_key),
        "---",
        "",
        "# 输入参数",
        "",
        "| 参数 key | CLI 参数 | 类型 | 必填 | 默认值 | 说明 | 是否多值 | Action |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
        "| amount | --amount | number | 是 |  | 合成金额 | 否 |  |",
        "",
        "# 输出",
        "",
        "| 输出 key | 类型 | 说明 |",
        "| --- | --- | --- |",
        "| steps | object | 各步骤结论 |",
        "",
    ]
    (scripts_dir / "{}.md".format(script_key)).write_text("\n".join(descriptor), encoding="utf-8")
    return script_path


def _order_flow(ontology_dir: Path, *, discount_script: bool = False) -> None:
    _write_operation(ontology_dir, PARENT, [INVENTORY, DISCOUNT])
    _write_operation(ontology_dir, INVENTORY)
    _write_operation(ontology_dir, DISCOUNT)
    _write_script(ontology_dir, "order_review", PARENT)
    if discount_script:
        _write_script(ontology_dir, "discount_calc", DISCOUNT)


def _entry(catalog, operation_key):
    return next(entry for entry in catalog if entry["operation_key"] == operation_key)


def test_load_operation_relations_reads_declared_children(tmp_path):
    ontology_dir = tmp_path / "ontology"
    _write_operation(ontology_dir, PARENT, [INVENTORY, " 折扣计算 ", PARENT, INVENTORY, "", 5])
    _write_operation(ontology_dir, INVENTORY)
    (ontology_dir / "operations" / "index.md").write_text(
        "---\nuses:\n  operations: [\"库存校验\"]\n---\n",
        encoding="utf-8",
    )
    (ontology_dir / "operations" / "broken.md").write_text(
        "---\nuses: [unclosed\n---\n",
        encoding="utf-8",
    )

    assert load_operation_relations(ontology_dir) == {PARENT: (INVENTORY, DISCOUNT)}
    assert load_operation_relations(tmp_path / "missing") == {}


def test_stage_without_own_script_borrows_the_flow_script(tmp_path):
    ontology_dir = tmp_path / "ontology"
    _order_flow(ontology_dir)

    catalog = load_script_catalog(ontology_dir)

    assert [entry["operation_key"] for entry in catalog] == [PARENT, INVENTORY, DISCOUNT]
    owner = catalog[0]
    for stage in (INVENTORY, DISCOUNT):
        borrowed = _entry(catalog, stage)
        assert borrowed["_borrowed_from"] == PARENT
        assert borrowed["_is_bound_operation"] is True
        assert borrowed["_script_path"] == owner["_script_path"]
        assert borrowed["parameters"] == owner["parameters"]
        assert borrowed["parameters"] is not owner["parameters"]
        assert borrowed["title"] == (
            "Runs the full '订单审核' script; '{0}' is one of its stages - read steps['{0}'] for this stage"
        ).format(stage)
    assert "_borrowed_from" not in owner
    assert "_stage_of" not in owner
    assert "_borrowed_from" not in find_script_entry(catalog, PARENT, bound_only=True)
    assert find_script_entry(catalog, "order_review", bound_only=True)["operation_key"] == PARENT
    assert find_script_entry(catalog, INVENTORY, bound_only=True)["_borrowed_from"] == PARENT

    public = public_script_catalog(catalog)
    assert [entry["operation_key"] for entry in public] == [PARENT, INVENTORY, DISCOUNT]
    assert all("_borrowed_from" not in entry for entry in public)
    assert "- 库存校验 - Runs the full '订单审核' script" in "\n".join(_format_script_catalog(public))


def test_stage_with_own_script_keeps_it_and_records_its_flow(tmp_path):
    ontology_dir = tmp_path / "ontology"
    _order_flow(ontology_dir, discount_script=True)

    catalog = load_script_catalog(ontology_dir)

    discount = [entry for entry in catalog if entry["operation_key"] == DISCOUNT]
    assert len(discount) == 1
    assert "_borrowed_from" not in discount[0]
    assert discount[0]["_stage_of"] == (PARENT,)
    assert discount[0]["_stage_owners"] == (PARENT,)
    assert _entry(catalog, INVENTORY)["_borrowed_from"] == PARENT


def test_stage_is_not_borrowed_without_a_unique_scripted_ancestor(tmp_path):
    unscripted = tmp_path / "unscripted"
    _write_operation(unscripted, PARENT, [INVENTORY])
    _write_operation(unscripted, INVENTORY)
    _write_script(unscripted, "inventory_check", INVENTORY)

    catalog = load_script_catalog(unscripted)

    assert [entry["operation_key"] for entry in catalog] == [INVENTORY]
    assert catalog[0]["_stage_of"] == (PARENT,)
    assert catalog[0]["_stage_owners"] == ()

    shared = tmp_path / "shared"
    _write_operation(shared, PARENT, [INVENTORY])
    _write_operation(shared, "退货审核", [INVENTORY])
    _write_operation(shared, INVENTORY)
    _write_script(shared, "order_review", PARENT)
    _write_script(shared, "return_review", "退货审核")

    assert [entry["operation_key"] for entry in load_script_catalog(shared)] == [PARENT, "退货审核"]


def test_transitive_stage_borrows_the_nearest_scripted_ancestor(tmp_path):
    ontology_dir = tmp_path / "ontology"
    _write_operation(ontology_dir, PARENT, ["审核准备"])
    _write_operation(ontology_dir, "审核准备", [INVENTORY])
    _write_operation(ontology_dir, INVENTORY, [PARENT])  # a declared cycle must not loop
    _write_script(ontology_dir, "order_review", PARENT)

    catalog = load_script_catalog(ontology_dir)

    assert catalog[0]["operation_key"] == PARENT
    assert {entry["operation_key"]: entry["_borrowed_from"] for entry in catalog[1:]} == {
        "审核准备": PARENT,
        INVENTORY: PARENT,
    }


def test_deterministic_operations_are_never_borrowed(tmp_path, monkeypatch):
    ontology_dir = tmp_path / "ontology"
    _order_flow(ontology_dir)

    monkeypatch.setattr(operation_execution_module, "protected_operation_keys", lambda _dir: frozenset({INVENTORY}))
    assert [entry["operation_key"] for entry in load_script_catalog(ontology_dir)] == [PARENT, DISCOUNT]

    monkeypatch.setattr(operation_execution_module, "protected_operation_keys", lambda _dir: frozenset({PARENT}))
    catalog = load_script_catalog(ontology_dir)
    assert [entry["operation_key"] for entry in catalog] == [PARENT]
    assert catalog[0]["_is_deterministic_handler"] is True


def test_declared_relations_drive_operation_roles(tmp_path):
    ontology_dir = tmp_path / "ontology"
    _order_flow(ontology_dir, discount_script=True)
    _write_operation(ontology_dir, "人工复核", [PARENT])
    original_parent = (ontology_dir / "operations" / "订单审核.md").read_text(encoding="utf-8")
    catalog = [
        {"operation key": "人工复核", "输出": "复核结论"},
        {"operation key": PARENT, "输出": "审核结论"},
        {"operation key": INVENTORY, "输出": "库存结论"},
        {"operation key": DISCOUNT, "输出": "折扣金额"},
        {"operation key": "独立查询", "输出": "查询结果"},
    ]

    annotated = _annotate_coordinating_operations(
        catalog,
        ontology_dir=ontology_dir,
        script_catalog=load_script_catalog(ontology_dir),
    )

    assert annotated[0]["execution_role"] == "coordinator; load before referenced stages"
    assert annotated[0]["referenced_operation_count"] == 1
    assert annotated[1]["execution_role"] == (
        "coordinator; load before referenced stages; stage of '人工复核'; its own script covers only this stage"
    )
    assert annotated[1]["referenced_operation_count"] == 2
    assert annotated[2]["execution_role"] == "stage of '订单审核'; executing it runs the full '订单审核' script"
    assert "referenced_operation_count" not in annotated[2]
    assert annotated[3]["execution_role"] == (
        "stage of '订单审核'; its own script covers only this stage - run '订单审核' when the complete result"
        " is requested"
    )
    assert annotated[4] == catalog[4]
    assert catalog[1] == {"operation key": PARENT, "输出": "审核结论"}
    assert (ontology_dir / "operations" / "订单审核.md").read_text(encoding="utf-8") == original_parent


def test_stage_of_unscripted_flow_with_own_script_has_no_run_hint(tmp_path):
    ontology_dir = tmp_path / "ontology"
    _write_operation(ontology_dir, PARENT, [INVENTORY])
    _write_operation(ontology_dir, INVENTORY)
    _write_script(ontology_dir, "inventory_check", INVENTORY)
    catalog = [{"operation key": PARENT}, {"operation key": INVENTORY}]

    annotated = _annotate_coordinating_operations(
        catalog,
        ontology_dir=ontology_dir,
        script_catalog=load_script_catalog(ontology_dir),
    )

    assert annotated[1]["execution_role"] == "stage of '订单审核'; its own script covers only this stage"


def test_packages_without_declarations_keep_the_reference_heuristic(tmp_path):
    ontology_dir = tmp_path / "ontology"
    _write_operation(ontology_dir, PARENT)
    _write_script(ontology_dir, "order_review", PARENT)
    catalog = [{"operation key": PARENT}, {"operation key": INVENTORY}]

    script_catalog = load_script_catalog(ontology_dir)

    assert [entry["operation_key"] for entry in script_catalog] == [PARENT]
    assert "_stage_of" not in script_catalog[0]
    assert _annotate_coordinating_operations(
        catalog,
        ontology_dir=ontology_dir,
        script_catalog=script_catalog,
    ) == catalog


def _run_operation(monkeypatch, tmp_path, ontology_dir: Path, operation_key: str, output: str):
    executed = []
    monkeypatch.setattr(
        execute_script_module,
        "get_ontology_manifest",
        lambda *args, **kwargs: (SimpleNamespace(id="demo", ontology_dir=str(ontology_dir)), None),
    )
    monkeypatch.setattr(
        execute_script_module,
        "handle_execute_script_path",
        lambda **kwargs: executed.append(kwargs) or output,
    )
    result = handle_execute_script(
        ontology_id="demo",
        operation_key=operation_key,
        arguments={"amount": 2},
        base_path=tmp_path,
    )
    return result, executed


def test_borrowed_stage_runs_the_flow_script_and_explains_the_result(monkeypatch, tmp_path):
    ontology_dir = tmp_path / "ontology"
    _order_flow(ontology_dir)
    output = json.dumps({"conclusion": "通过", "steps": {INVENTORY: {"result": "充足"}}}, ensure_ascii=False)

    result, executed = _run_operation(monkeypatch, tmp_path, ontology_dir, INVENTORY, output)

    assert executed[0]["script_path"] == "ontology/scripts/order_review.py"
    assert executed[0]["args"] == ["--amount", "2"]  # number 取整数值时按整数文本传
    payload = json.loads(result)
    assert payload["conclusion"] == "通过"
    assert payload["steps"] == {INVENTORY: {"result": "充足"}}
    assert payload["_execution_note"] == (
        "Operation '库存校验' is a stage of '订单审核'; the full '订单审核' script ran. Use steps['库存校验'] for this"
        " stage, or the whole result when the full flow was requested."
    )
    assert execute_script_module._is_successful_script_output(result) is True


def test_own_stage_script_result_names_the_complete_flow(monkeypatch, tmp_path):
    ontology_dir = tmp_path / "ontology"
    _order_flow(ontology_dir, discount_script=True)

    result, executed = _run_operation(monkeypatch, tmp_path, ontology_dir, DISCOUNT, '{"discount": 3}')

    assert executed[0]["script_path"] == "ontology/scripts/discount_calc.py"
    assert json.loads(result) == {
        "discount": 3,
        "_execution_note": (
            "This result covers only the '折扣计算' stage of '订单审核'. If the user asked for the complete result of"
            " that flow, execute '订单审核' instead."
        ),
    }


def test_stage_script_of_unscripted_flow_points_to_remaining_stages(monkeypatch, tmp_path):
    ontology_dir = tmp_path / "ontology"
    _write_operation(ontology_dir, PARENT, [INVENTORY])
    _write_operation(ontology_dir, INVENTORY)
    _write_script(ontology_dir, "inventory_check", INVENTORY)

    result, _executed = _run_operation(monkeypatch, tmp_path, ontology_dir, INVENTORY, '{"result": "充足"}')

    assert json.loads(result)["_execution_note"] == (
        "This result covers only the '库存校验' stage of '订单审核'. When the complete '订单审核' result is"
        " requested, continue with the remaining documented stages of '订单审核'."
    )


def test_stage_note_leaves_other_results_untouched(monkeypatch, tmp_path):
    ontology_dir = tmp_path / "ontology"
    _order_flow(ontology_dir)

    plain, _ = _run_operation(monkeypatch, tmp_path, ontology_dir, INVENTORY, "plain text result")
    listing, _ = _run_operation(monkeypatch, tmp_path, ontology_dir, INVENTORY, '[{"result": "充足"}]')
    failed, _ = _run_operation(monkeypatch, tmp_path, ontology_dir, INVENTORY, "Error: Script exited with code 1.")
    owner, _ = _run_operation(monkeypatch, tmp_path, ontology_dir, PARENT, '{"conclusion": "通过"}')

    assert plain == "plain text result"
    assert listing == '[{"result": "充足"}]'
    assert failed == "Error: Script exited with code 1."
    assert owner == '{"conclusion": "通过"}'


def test_oversize_argument_loader_lets_scripts_import_sibling_modules(monkeypatch, tmp_path):
    scripts_dir = tmp_path / "package" / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "shared_steps.py").write_text("STEP = 'sibling'\n", encoding="utf-8")
    script = scripts_dir / "flow.py"
    script.write_text(
        "import json, sys\n"
        "import shared_steps\n"
        "print(json.dumps({'step': shared_steps.STEP, 'argv': sys.argv[1:]}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(execute_script_module, "SCRIPT_LIVE_LOG_ENABLED", False)
    # Env pruning is unrelated to import resolution and would strip the
    # variables a Windows interpreter needs to start.
    monkeypatch.setattr(execute_script_module, "_strip_child_environ", lambda env: dict(env))

    completed = execute_script_module._spawn_with_args_file(
        script,
        ["--records", "[]"],
        env=dict(os.environ),
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"step": "sibling", "argv": ["--records", "[]"]}
