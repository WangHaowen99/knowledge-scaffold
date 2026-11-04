"""B4d 合进交付分支后的衔接（gaps B4d 第 1～7 条；decisions-log「B4d 合并」）。

B4d（建模蓝图、按段的主流程）与 B5c（节点包：可补做、已停用、熔断、人工节点、节点钩子、包的发布门禁）都改了流程
运行时的同一条计算链，这里证明两边的行为同时成立：

- 节点包的关卡挡住建模（做了一部分也显示被挡住）、熔断解除关卡；F8 起不分段，F8 之前按段建的项目也只算「全部」一段；
- 可补做照样成立：不计进度分母、不抢主线下一步；
- 待处理里人工节点的决策卡和建模蓝图的待答问题并存，各自回答各自消失；
- 发布页的节点（发布）开始时回「在页面里操作」，人工节点照常出卡；
- 发布门禁链：平台门禁在前，流程关卡（包的关卡节点；F8 起全局一致性是旁支，不再是关卡）在后。

节点包、蓝图全部合成；假引擎不起真实 CLI。
"""

from __future__ import annotations

import copy

from app.governance import ops_state
from app.services.flow import model as flow_model
from app.services.flow import view
from app.services.packs import contracts
from tests.pack_helpers import NODE_PACK_FLOW
from tests.test_changesets import main_write, obj
from tests.test_node_pack_lifecycle import (  # noqa: F401 - 共用 fixture 与工具
    HUMAN_NODE,
    enable_and_apply,
    env,
    install,
    kinds_flow,
    nodes_of,
)

DRAFT = {"slices": [{"name": "借书", "objects": ["读者"], "operations": ["借书"]},
                    {"name": "还书", "depends_on": ["借书"], "objects": ["图书"], "operations": ["还书"]}],
         "questions": [{"title": "逾期要不要单独建对象？", "background": "合成", "options": ["要", "不要"]}]}


def _blueprint(root, *, confirmed: bool = True) -> None:
    """F8 之前写下的两段蓝图（现在的脚本不再写多段，按老格式拼出来）。"""
    module = contracts.default().load("blueprint")
    doc = module.from_draft(DRAFT)
    base = doc["slices"][0]
    doc["slices"] = [{**base, "id": f"s{n}", "name": item["name"], "objects": item["objects"],
                      "operations": item["operations"], "rules": [], "depends_on": [f"s{n - 1}"] if n > 1 else []}
                     for n, item in enumerate(DRAFT["slices"], start=1)]
    doc["next_ids"]["slice"] = 3
    main_write(root, "research/blueprint.json", module.dumps(module.confirm(doc) if confirmed else doc),
               "research: 合成蓝图")


def _research(root) -> None:
    main_write(root, "research/20260101-合成.md", "# 合成资料分析\n")


def _entries_done(root, obj_name: str, op_name: str) -> None:
    main_write(root, f"ontology/object/{obj_name}.md", obj(obj_name, "合成"))
    main_write(root, f"ontology/operation/{op_name}.md", f"# {op_name}\n\n合成操作 [[{obj_name}]]\n")
    main_write(root, f"ontology/testcase/{op_name}.md", f"# 测试场景：{op_name}\n\n## 场景 1：合成\n")


def _gate_before_model(**fields) -> dict:
    flow = copy.deepcopy(NODE_PACK_FLOW)
    flow["nodes"][0].update({"placement": "gate", "after": "core.research", "blocks": ["core.model"], **fields})
    return flow


def _step_after_research() -> dict:
    flow = copy.deepcopy(NODE_PACK_FLOW)
    flow["nodes"][0]["after"] = "core.research"
    return flow


def test_pack_gate_and_breaker_with_an_old_sliced_blueprint(env):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    _research(root)
    _blueprint(root)
    main_write(root, "ontology/object/读者.md", obj("读者", "合成"))
    install(manager, tmp_path, flow=_gate_before_model())
    enable_and_apply(client, manager, project_id)
    built = view.build(root)
    nodes = {node["id"]: node for node in built["nodes"]}
    assert nodes["core.model"]["state"] == "blocked" and nodes["naming.check"]["state"] == "ready"
    assert [item["id"] for item in built["slices"]] == ["all"]  # 老的两段蓝图也只算「全部」
    assert built["slices"][0]["stages"]["object"] == "confirmed"
    ops_state.set_breaker("naming", "0.1.0", "open", "ops")
    try:
        nodes = nodes_of(root)
        assert nodes["naming.check"]["state"] == "skipped" and nodes["naming.check"]["actions"] == []
        assert nodes["core.model"]["state"] == "running"  # 关卡解除：对象做了、操作还没做
        assert nodes["core.model"]["progress"] is None
    finally:
        ops_state.set_breaker("naming", "0.1.0", "close", "ops")
        ops_state.set_breaker("naming", "0.1.0", "close", "ops")


def test_backfill_keeps_out_of_the_way_of_the_mainline_next_step(env):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    _research(root)
    _blueprint(root)
    _entries_done(root, "读者", "借书")
    before = view.build(root)
    total_before = flow_model.progress(root)["total"]
    # 建模已经做完（老蓝图第二段没做也算：不分段）：串在资料分析之后的命名检查是新插进来的、被越过了
    install(manager, tmp_path, flow=_step_after_research())
    enable_and_apply(client, manager, project_id)
    built = view.build(root)
    check = next(node for node in built["nodes"] if node["id"] == "naming.check")
    assert check["flags"]["backfill"] is True and check["counts_in_progress"] is False
    assert built["progress"] == before["progress"] and flow_model.progress(root)["total"] == total_before
    model = next(node for node in built["nodes"] if node["id"] == "core.model")
    assert model["state"] == "confirmed" and model["progress"] is None
    # 主推仍是主线下一步：校验
    primary = built["suggestions"]["primary"]
    assert primary["action"]["command"] == "/ontology-validate" and not primary["id"].startswith("backfill:")
    every = [primary, *built["suggestions"]["secondary"]]
    assert all(item["priority"] <= 5 for item in every)


def test_progress_matches_flow_with_an_old_sliced_blueprint_and_backfill(env):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    _research(root)
    _blueprint(root)
    _entries_done(root, "读者", "借书")
    install(manager, tmp_path, flow=_step_after_research())
    enable_and_apply(client, manager, project_id)
    info = flow_model.progress(root)
    assert info["hits"]["core.model"] is True  # 不分段：老蓝图第二段没做，建模也算做完
    built = view.build(root)
    assert info["done"] == built["progress"]["done"] and info["total"] == built["progress"]["total"]
    _entries_done(root, "图书", "还书")
    info = flow_model.progress(root)
    assert info["hits"]["core.model"] is True and info["done"] == view.build(root)["progress"]["done"]


def test_human_node_card_and_blueprint_questions_share_the_decision_group(env):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    _research(root)
    _blueprint(root, confirmed=False)
    main_write(root, "ontology/object/读者.md", obj("读者", "合成"))
    install(manager, tmp_path, flow=kinds_flow({**HUMAN_NODE, "after": "core.research", "blocks": ["core.model"]},
                                                probes={"naming_signoff": {"files": ["reports"],
                                                                           "glob": "signoff-naming.md"}}))
    enable_and_apply(client, manager, project_id)
    started = client.post(f"/api/v1/projects/{project_id}/flow/nodes/naming.signoff/start", json={})
    assert started.status_code == 200, started.text
    node_question = started.json()["question"]["question_id"]
    base = f"/api/v1/projects/{project_id}/inbox"
    listed = client.get(base, params={"filter": "decision"}).json()
    ids = {item["id"]: item for item in listed["items"]}
    assert {f"decision:{node_question}", "decision:bq-q1"} <= set(ids)
    assert ids[f"decision:{node_question}"]["source"] == "plugin" and ids["decision:bq-q1"]["source"] == "assistant"
    assert listed["counts"]["by_filter"]["decision"] == 2
    assert client.post(f"{base}/decision:bq-q1/actions", json={"action": "answer", "option_id": "A"}).status_code == 200
    assert client.post(f"{base}/decision:{node_question}/actions",
                       json={"action": "answer", "option_id": "pass"}).status_code == 200
    remaining = [item["id"] for item in client.get(base, params={"filter": "decision"}).json()["items"]]
    assert remaining == []
    assert client.get(f"/api/v1/projects/{project_id}/blueprint").json()["questions"][0]["answer"] == {"option_id": "A"}


def test_publish_node_is_a_page_and_flow_gates_follow_platform_gates(env):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    started = client.post(f"/api/v1/projects/{project_id}/flow/nodes/core.publish/start", json={})
    assert started.status_code == 400 and started.json()["details"]["page"] == "publish"
    _blueprint(root)
    main_write(root, "ontology/object/读者.md", obj("读者", "合成"))
    gate = {**NODE_PACK_FLOW["nodes"][0], "placement": "gate", "after": "core.consistency", "before": "core.publish",
            "blocks": ["core.publish"]}
    install(manager, tmp_path, flow={**NODE_PACK_FLOW, "nodes": [gate]})
    enable_and_apply(client, manager, project_id)
    gates = client.get(f"/api/v1/projects/{project_id}/publish/check").json()["gates"]
    kinds = [item["source"]["kind"] for item in gates]
    flow_gates = [item for item in gates if item["source"]["kind"] == "flow"]
    assert [item["id"] for item in flow_gates] == ["flow:naming.check"]  # F8：全局一致性不再是发布关卡
    # 关卡来源写包清单里的名字（F 第 10 条：原来写包名）
    assert flow_gates[0]["source"] == {"kind": "flow", "label": "naming 包", "pack": "naming"}
    assert kinds.index("flow") == len(kinds) - len(flow_gates)  # 流程关卡排在最后
    assert [item["order"] for item in gates] == list(range(1, len(gates) + 1))
    assert nodes_of(root)["core.publish"]["state"] == "blocked"  # 包的流程关卡没过
