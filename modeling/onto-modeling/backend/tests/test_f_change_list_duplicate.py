"""F 第一批第 11 条（V 增量更新）：按已确认的清单执行那一轮，助手又写了一次 update-plan.json。

平台多收了一份内容相同的清单。这条对话里有清单正在执行（confirmed，还没 executed）时，执行时写的计划不另收。
"""
from __future__ import annotations

import json

from app.kernel.changesets import service
from app.services.flow import change_lists
from tests.test_flow_states import env  # noqa: F401

PLAN = [{"file": "ontology/object/门店.md", "operation": "modify", "change_summary": "加字段", "reason": "修订"}]


def _write_plan(copy, plan) -> None:
    (copy / ".ontology-state").mkdir(exist_ok=True)
    (copy / ".ontology-state" / "update-plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")


def test_executing_turn_does_not_capture_another_list(env):  # noqa: F811
    _client, workspaces, _manager, project_id, root = env
    copy = service.copy_for_turn(workspaces, project_id, root)
    _write_plan(copy, PLAN)
    listed = change_lists.capture(root, copy, conversation_id="default", changeset_id=None)
    change_lists.accept(root, listed["id"], point_ids=["p1"])
    change_lists.mark(root, listed["id"], state="confirmed")  # INC-04 发出执行
    # 执行这一轮助手按命令文档第 4 步把计划连同 base_commit 再写一遍
    _write_plan(copy, {"base_commit": "0" * 40, "plan": PLAN})
    assert change_lists.capture(root, copy, conversation_id="default", changeset_id=None) is None
    assert [item["id"] for item in change_lists.all_lists(root)] == [listed["id"]]


def test_other_conversation_still_captures(env):  # noqa: F811
    _client, workspaces, _manager, project_id, root = env
    copy = service.copy_for_turn(workspaces, project_id, root)
    _write_plan(copy, PLAN)
    listed = change_lists.capture(root, copy, conversation_id="default", changeset_id=None)
    change_lists.mark(root, listed["id"], state="confirmed")
    _write_plan(copy, [{**PLAN[0], "change_summary": "另一处修订"}])
    assert change_lists.capture(root, copy, conversation_id="c-2", changeset_id=None) is not None
