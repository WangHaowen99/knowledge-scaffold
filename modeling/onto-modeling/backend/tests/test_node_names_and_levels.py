"""节点模型重构 2b：名字统一、全部命令按节点分组、推荐五级。

- 名字表（flow/names.py）：同一个按钮在导航栏数据（FL-01 节点动作）、全部命令（S-18）、建议条、对话气泡、排队条、
  执行记录 command_label 里叫同一个名字；主命令叫节点名；
- S-18 每个导航栏节点一组，不设「其他」组；场景模拟的子文档、整体视图这类不在节点上的命令不进菜单，S-20 照样能发；
- 推荐五级（flow_state.suggest）表驱动：每级命中就停；第 2 级在条件节点和过期必需节点之间取靠前的那个；ADR-0090；
  第 3 级节点在跑时为空；第 4 级只在必需都做完后出现；done 为空的节点不推；次选最多 2 个；核验排第一；
- 三个出口（后端 FL-01、助手副本 state_helper suggest、注入给模型的那一行）一致；
- 生成脚本不带目标时先扫描候选。

项目、条目、包都是合成的。
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest
import yaml

from app import config
from app.routers import sessions as session_routes
from app.services.flow import assemble, model, names, need, view
from app.services.session_host import command_tasks, exec_records, injection, next_steps
from tests.pack_helpers import ROUND_PACK, ROUND_PACK_FLOW
from tests.test_changesets import git, main_write, obj
from tests.test_commands_api import api, wait  # noqa: F401 - 夹具
from tests.test_f7_next_step import idle
from tests.test_flow_states import env  # noqa: F401 - 夹具
from tests.test_session_host import FakeEngine, lock

P = "/api/v1/projects"
ASSETS = Path(config.ASSETS_DIR)
RESEARCH_CHECK = "运行 python3 .claude/scripts/research_checker.py 核验资料分析报告，逐条列出错误项与来源位置"


def _load_flow_state():
    spec = importlib.util.spec_from_file_location("flow_state_levels", ASSETS / "workspace-toolkit" / "scripts" / "flow_state.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FS = _load_flow_state()


def _decl(rel: str) -> dict:
    return yaml.safe_load((ASSETS / rel).read_text(encoding="utf-8"))


def _flow(*extra: tuple) -> dict:
    """core 加默认启用的三个官方包（图示、场景模拟、代码生成），条件节点没去掉（测试按需要放进 states）。"""
    packs = [("diagram", "t", _decl("packs/diagram/flow.yaml")), ("trace", "t", _decl("packs/trace/flow.yaml")),
             ("codegen", "t", _decl("packs/codegen/flow.yaml"))]
    return assemble.assemble([("core", "t", _decl("workspace-toolkit/flow.yaml")), *packs, *extra],
                             trusted={name for name, _version, _flow in packs})


FLOW = _flow()


def _states(flow: dict, absent=("core.update",), **overrides) -> dict:
    """默认全部已确认；core_object="stale" 这样改单个节点（下划线换成点），值也可以是 (状态, 标记)。
    absent 里的节点不在 states 里（条件不成立、没出现）。"""
    out = {node["id"]: {"state": "confirmed", "flags": {}} for node in flow["nodes"] if node["id"] not in absent}
    for key, value in overrides.items():
        state, flags = value if isinstance(value, tuple) else (value, {})
        out[key.replace("_", ".")] = {"state": state, "flags": dict(flags)}
    return out


def _items(sug: dict) -> list[dict]:
    return [item for item in [sug.get("primary"), *(sug.get("secondary") or [])] if item]


def _labels(sug: dict) -> list[str]:
    return [item["label"] for item in _items(sug)]


FRESH = dict(core_research="ready", core_object="not_ready", core_operation="not_ready", core_validate="not_ready",
             trace_trace="not_ready", core_publish="not_ready", diagram_flowchart="not_ready", core_metric="not_ready",
             codegen_codegen="not_ready", codegen_solver="not_ready")
OPTIONALS_READY = dict(diagram_flowchart="ready", core_metric="ready", codegen_codegen="ready", codegen_solver="ready")


# ---- 名字表 ----


def test_name_table_takes_names_in_order():
    for node in FLOW["nodes"]:
        primary = need.primary_action(node)
        assert primary is None or primary["label"] == node["title"], node["id"]  # 节点名就是主命令名
    cases = [
        ("/ontology-research 仅基于本地文档 ", "资料分析"),  # 1 动作完全匹配
        ("/ontology-research 追加", "追加资料分析"),
        ("/ontology-research 追加 补充门店口径", "追加资料分析"),  # 动作文本后面带参数
        ("/ontology-trace 交互 下单", "交互模拟"),
        ("/ontology-flowchart 全量刷新", "全量刷新流程图"),
        ("/ontology-validate 门店 --since 0123456789ab", "校验"),
        ("/ontology-codegen", "生成脚本"),
        ("/ontology-codegen 订单审核", "生成脚本"),
        ("/ontology-codegen-scan", "扫描脚本候选"),
        (RESEARCH_CHECK, "核验资料分析报告"),
        ("/ontology-research\n\n仅基于本地文档", "资料分析"),  # 2 节点的主命令（只看第一行）
        ("/ontology-trace 订单", "场景模拟"),
        ("/ontology-mock 订单", "生成示例数据"),  # 3 变体
        ("/ontology-trace-batch scope=all", "场景模拟"),  # 4 命令表里的节点
        ("/ontology-trace-interactive scenario=s-01", "场景模拟"),
        ("/ontology-update 按新资料更新", "增量更新"),  # 条件过滤之前的流程：没出现的节点也有名字
    ]
    for text, name in cases:
        assert names.name_for(FLOW, text) == name, text
    assert names.lookup(FLOW, "/ontology-overview") is None
    assert names.name_for(FLOW, "/ontology-overview", "整体视图") == "整体视图"  # 5 认不出才用页面给的叫法
    assert names.name_for(FLOW, "/ontology-overview") == "ontology-overview"  # 再没有就是命令名
    assert names.name_for(FLOW, "/ontology-validate", "运行质量检查") == "校验"  # 认得出时不用页面的叫法
    assert names.lookup(FLOW, "运行一下校验") is None  # 普通消息只认整行相同


def test_one_button_has_one_name_everywhere(api):  # noqa: F811
    """导航栏数据、S-18、建议条、对话气泡、任务标题、排队条、执行记录：同一个按钮同一个名字。"""
    client, manager, project_id, root = api
    main_write(root, "research/合成主题.md", "# 合成资料分析\n")  # 有了报告，「追加资料分析」才出现
    main_write(root, "ontology/diagrams/补货.drawio", "<mxfile/>\n")  # 有了流程图，全量刷新、校验流程图才出现
    flow_view = client.get(f"{P}/{project_id}/flow").json()
    node_actions = {(node["id"], action["target"].strip()): action["label"]
                    for node in flow_view["nodes"] for action in node["actions"]
                    if action.get("kind") == "start" and str(action.get("target") or "").startswith("/")}
    catalog = client.get(f"{P}/{project_id}/commands/catalog").json()
    items = [item for group in catalog["groups"] for item in group["items"]]
    assert {item["label"] for item in items} >= {"资料分析", "追加资料分析", "场景模拟", "生成脚本", "扫描脚本候选"}
    for item in items:
        assert node_actions[(item["node_id"], item["target"])] == item["label"], item
    # 建议条：同一个节点的条目，名字、顺序和 S-18 一样（主推叫节点名）
    flow = model.load_for(root)
    sug = model.flow_state(root).suggest(flow, _states(flow, trace_trace="ready", core_publish="not_ready"), {})
    assert _labels(sug) == [item["label"] for item in items if item["node_id"] == "trace.trace"]
    # 照全部命令菜单发（前端 catalogCommand 的请求体）：气泡、任务标题、执行记录都叫这个名字
    for item in items:
        words = item["target"].split()
        body = {"command": words[0], "text": " ".join(words[1:]), "display_label": item["label"],
                "origin": {"page": "wb-l2", "label": "全部命令"}, "conversation_id": "default"}
        response = client.post(f"{P}/{project_id}/commands", json=body)
        assert response.status_code == 202, response.text
        task = response.json()["task"]
        assert task["title"] == f"助手正在执行「{item['label']}」"
        done = wait(lambda: (command_tasks.get(project_id, task["task_id"]) or {}).get("exec_id")
                    and command_tasks.get(project_id, task["task_id"]))
        assert exec_records.get(project_id, done["exec_id"])["command_label"] == item["label"], item
        messages = client.get(f"{P}/{project_id}/conversations/default/messages").json()["items"]
        bubble = [entry["data"] for entry in messages if entry["type"] == "message.user"][-1]
        assert bubble["text"] == item["label"]
        idle(manager, project_id)
    # 排队条：助手在跑时发的命令先代存，排队条上的名字同一个；手打的命令也一样
    FakeEngine.scripts = [[lock("validate", "start")]]
    first = client.post(f"{P}/{project_id}/commands", json={"command": "/ontology-validate", "origin": {"page": "reports"},
                                                             "conversation_id": "default"})
    assert first.status_code == 202
    wait(lambda: manager.get(project_id) and manager.get(project_id).state == "running")
    held = client.post(f"{P}/{project_id}/commands", json={
        "command": "/ontology-research", "text": "追加", "display_label": "追加资料分析",
        "origin": {"page": "wb-l2", "label": "全部命令"}, "conversation_id": "default"})
    assert held.status_code == 202 and held.json()["next"]["label"] == "追加资料分析"
    typed = client.post(f"{P}/{project_id}/conversations/default/messages",
                        json={"text": "/ontology-flowchart 全量刷新", "replace": True})
    assert typed.status_code == 202 and typed.json()["next"]["label"] == "全量刷新流程图"
    client.post(f"{P}/{project_id}/conversations/default/interrupt")


# ---- S-18 ----


def test_catalog_groups_by_navigation_node_without_other_group(api):  # noqa: F811
    client, _manager, project_id, root = api
    catalog = client.get(f"{P}/{project_id}/commands/catalog").json()
    groups = [(group["id"], group["label"], [(item["label"], item["target"], item["primary"]) for item in group["items"]])
              for group in catalog["groups"]]
    assert groups == [
        ("core.research", "资料分析", [("资料分析", "/ontology-research 仅基于本地文档", True),
                                    ("绘制流程图", "/ontology-flowchart", True)]),
        ("core.object", "提取对象", [("提取对象", "/ontology-extract-object", True)]),
        ("core.operation", "提取操作", [("提取操作", "/ontology-extract-operation", True),
                                    ("提取规则与指标", "/ontology-extract-rule-metric", True)]),
        ("core.validate", "校验", [("校验", "/ontology-validate", True)]),
        ("trace.trace", "场景模拟", [("场景模拟", "/ontology-trace 全部", True), ("交互模拟", "/ontology-trace 交互", False),
                                  ("生成示例数据", "/ontology-mock", False), ("生成脚本", "/ontology-codegen", True),
                                  ("扫描脚本候选", "/ontology-codegen-scan", False)]),
    ]  # 「追加资料分析」要有报告、全量刷新和校验流程图要有流程图（动作的 when）；发布是跳页面的按钮，不列
    assert catalog["total"] == sum(len(items) for _id, _label, items in groups)
    item = catalog["groups"][0]["items"][1]
    assert set(item) == {"id", "label", "description", "command", "target", "pack", "node_id", "primary", "args_schema",
                         "available", "unavailable_reason"}
    assert (item["command"], item["node_id"], item["pack"]) == ("/ontology-flowchart", "diagram.flowchart", "diagram")
    assert len({entry["id"] for group in catalog["groups"] for entry in group["items"]}) == catalog["total"]
    main_write(root, "research/合成主题.md", "# 合成资料分析\n")
    main_write(root, "ontology/diagrams/补货.drawio", "<mxfile/>\n")
    research = client.get(f"{P}/{project_id}/commands/catalog").json()["groups"][0]["items"]
    assert [entry["label"] for entry in research] == ["资料分析", "追加资料分析", "绘制流程图", "全量刷新流程图", "校验流程图"]


def test_sub_documents_stay_out_of_the_menu_but_can_be_sent(api):  # noqa: F811
    client, manager, project_id, root = api
    catalog = client.get(f"{P}/{project_id}/commands/catalog").json()
    listed = {item["command"] for group in catalog["groups"] for item in group["items"]}
    off_menu = {"/ontology-trace-batch", "/ontology-trace-single", "/ontology-trace-interactive", "/ontology-overview",
                "/ontology-impact", "/ontology-query", "/version", "/ontology"}
    assert not listed & off_menu
    assert all(group["id"] != "other" and group["label"] != "其他" for group in catalog["groups"])
    assert off_menu <= session_routes.installed_commands(root)
    # 页面直接发子文档：命令表认得出，叫节点名；页面给的按钮叫法只在认不出时用
    sent = [({"command": "/ontology-trace-batch", "args": {"scope": "all"}, "text": "批量推演 3 个场景",
              "display_label": "场景页按钮"}, "场景模拟"),
            ({"command": "/ontology-overview", "display_label": "整体视图"}, "整体视图"),
            ({"command": "/ontology-overview"}, "ontology-overview")]
    for body, name in sent:
        response = client.post(f"{P}/{project_id}/commands", json={
            **body, "origin": {"page": "scenarios", "label": "场景模拟"}, "conversation_id": "default"})
        assert response.status_code == 202, response.text
        assert response.json()["task"]["title"] == f"助手正在执行「{name}」"
        idle(manager, project_id)
    # 增量更新没出现（条件不成立），手打照样有名字
    assert "core.update" not in {node["id"] for node in model.load_for(root)["nodes"]}
    assert names.name_at(root, "/ontology-update 按新资料更新") == "增量更新"


# ---- 推荐五级 ----

LEVEL_CASES = [
    # 第 1 级：对话状态
    ("等你回答", FRESH, {"question": {"id": "q1", "kind": "question"}}, "waiting_decision", ["去回答"], None),
    ("等你回答，目标问题同级", FRESH, {"question": {"id": "q1"}, "blueprint": {"goal_pending": True}},
     "waiting_decision", ["去回答", "回答目标问题"], None),
    ("只有目标问题：停在这里，不带主线下一步", FRESH, {"blueprint": {"goal_pending": True}}, "normal", ["回答目标问题"], None),
    ("出错", FRESH, {"failed": {"reason": "模型服务暂时不稳定"}}, "failed", ["重试这一轮"], "模型服务暂时不稳定"),
    ("被打断", FRESH, {"interrupted": {"id": "ex-1", "label": "提取操作", "mode": "resume", "phase": "写入操作"}},
     "interrupted", ["继续「提取操作」", "放弃这一步"], "从「写入操作」接着做"),
    ("没收尾", FRESH, {"unfinished": {"id": "ex-1", "label": "校验"}}, "normal", ["继续「校验」", "放弃这一步"], "这一步还没收尾"),
    ("合入冲突：命中就停，不再补主线", FRESH, {"changeset": {"id": "cs-1", "state": "conflict"}}, "normal", ["处理冲突"], None),
    ("命令等你打字回复", FRESH, {"awaiting_reply": {"id": "ex-1", "label": "校验"}}, "normal", [], "「校验」在等你回复上面的问题"),
    ("自主构建在跑", FRESH, {"autobuild": True}, "autobuild_controls", ["暂停", "在安全点停下", "转为陪伴"], None),
    ("这条对话在跑", FRESH, {"running": True}, "running", [], None),
    # 第 2 级：过期的必需节点（按钮叫节点名，哪类变了写在说明行）
    ("过期的必需节点", dict(core_operation="stale", core_validate="ready", trace_trace="not_ready", core_publish="not_ready"),
     {"stale_upstream": {"core.operation": "对象"}}, "normal", ["提取操作"], "对象有变化"),
    ("过期，说不清哪类变了", dict(core_validate="stale", trace_trace="not_ready", core_publish="not_ready"), {},
     "normal", ["校验"], "前面的内容有变化"),
    ("ADR-0090：校验已确认，过期的提取操作交给它", dict(core_operation="stale", trace_trace="ready", core_publish="not_ready"),
     {"stale_upstream": {"core.operation": "对象"}}, "normal", ["场景模拟", "交互模拟", "生成示例数据"], None),
    ("范围限定生效时不推过期的", dict(core_operation="stale", core_validate="ready", trace_trace="not_ready",
                                      core_publish="not_ready"), {"confine_active": True}, "normal", ["校验"], None),
    # 第 3 级：第一个没做完的必需节点
    ("新建", FRESH, {"probes": {"research_report": False}}, "normal", ["资料分析"], None),
    ("有报告：追加资料分析出现", FRESH, {"probes": {"research_report": True}}, "normal", ["资料分析", "追加资料分析"], None),
    ("已有产出、没完成：核验排第一", FRESH, {"probes": {"research_report": True}, "produced": ["core.research"]},
     "normal", ["核验资料分析报告", "资料分析", "追加资料分析"], None),
    ("有产出但不是报告：核验动作不出现", FRESH, {"probes": {"research_report": False}, "produced": ["core.research"]},
     "normal", ["资料分析"], None),
    ("对象已提取", dict(core_operation="ready", core_validate="not_ready", trace_trace="not_ready", core_publish="not_ready"),
     {}, "normal", ["提取操作"], None),
    ("在跑：建议条为空", dict(core_operation="running", core_validate="not_ready", trace_trace="not_ready",
                             core_publish="not_ready", core_metric="ready"), {}, "normal", [], None),
    ("待确认：建议条为空", dict(core_operation="pending_confirm", core_validate="not_ready", trace_trace="not_ready",
                               core_publish="not_ready"), {}, "normal", [], None),
    ("建模完成；报告待处理时也是「校验」", dict(core_validate="ready", trace_trace="not_ready", core_publish="not_ready"),
     {"stage": "校验待审阅", "report_pending": True}, "normal", ["校验"], None),
    ("校验完成：次选最多 2 个", dict(trace_trace="ready", core_publish="not_ready"), {}, "normal",
     ["场景模拟", "交互模拟", "生成示例数据"], None),
    ("只剩发布：可选节点等必需的都做完", dict(core_publish="ready", **OPTIONALS_READY), {}, "normal", ["发布"], None),
    ("可补做的必需节点不挡第 3 级", dict(core_research=("ready", {"backfill": True}), core_publish="ready"), {},
     "normal", ["发布"], None),
    # 第 4 级：必需节点都做完
    ("全部必需完成：第一个可选节点", OPTIONALS_READY, {}, "normal", ["绘制流程图", "全量刷新流程图", "校验流程图"], None),
    ("跳过的可选节点不推", dict(OPTIONALS_READY, diagram_flowchart="skipped"), {}, "normal", ["提取规则与指标"], None),
    ("再往后：生成脚本", dict(OPTIONALS_READY, diagram_flowchart="skipped", core_metric="confirmed"), {}, "normal",
     ["生成脚本", "扫描脚本候选"], None),
    ("过期的可选节点交给校验（ADR-0090）", dict(OPTIONALS_READY, diagram_flowchart="confirmed", core_metric="stale"),
     {"stale_upstream": {"core.metric": "对象、操作"}}, "normal", ["生成脚本", "扫描脚本候选"], None),
    ("过期的可选节点没人接手", dict(diagram_flowchart="stale"), {"stale_upstream": {"diagram.flowchart": "资料"}}, "normal",
     ["绘制流程图", "全量刷新流程图", "校验流程图"], "资料有变化"),
    ("可补做的必需节点", dict(core_research=("ready", {"backfill": True})), {"probes": {"research_report": False}},
     "normal", ["资料分析"], None),
    # 第 5 级
    ("没有可推的：不显示（求解适配没有对话入口）", dict(codegen_solver="ready"), {}, "normal", [], None),
]


@pytest.mark.parametrize("case,overrides,context,mode,labels,note", LEVEL_CASES, ids=[case[0] for case in LEVEL_CASES])
def test_suggest_levels(case, overrides, context, mode, labels, note):
    sug = FS.suggest(FLOW, _states(FLOW, **overrides), copy.deepcopy(context))
    assert (sug["mode"], _labels(sug), sug["note"]) == (mode, labels, note)
    assert len(sug["secondary"]) <= 2


def test_suggest_level_details():
    """主推的动作、编号与说明；核验那条是普通消息（不走 S-20）。"""
    verify = FS.suggest(FLOW, _states(FLOW, **FRESH), {"probes": {"research_report": True}, "produced": ["core.research"]})
    assert verify["primary"]["action"] == {"type": "send", "text": RESEARCH_CHECK}
    assert verify["secondary"][0]["action"] == {"type": "send", "text": "/ontology-research 仅基于本地文档 ",
                                                "command": "/ontology-research"}
    stale = FS.suggest(FLOW, _states(FLOW, core_validate="stale", trace_trace="not_ready", core_publish="not_ready"),
                       {"stale_upstream": {"core.validate": "对象"},
                        "stale_scope": {"core.validate": {"command": "/ontology-validate 门店 --since 0123456789ab"}}})
    assert stale["primary"]["id"] == "refresh:core.validate" and stale["primary"]["reason"] == "对象有变化"
    assert stale["primary"]["action"]["text"] == "/ontology-validate 门店 --since 0123456789ab"
    publish = FS.suggest(FLOW, _states(FLOW, core_publish="ready"), {})
    assert publish["primary"]["action"] == {"type": "navigate", "page": "publish"}
    # 选中的节点此刻都不能执行：建议条为空，不越过它推后面的
    blocked = FS.suggest(FLOW, _states(FLOW, trace_trace="ready", core_publish="not_ready", **{"diagram_flowchart": "ready"}),
                         {}, check=lambda action: action.get("type") != "send")
    assert blocked["primary"] is None and blocked["secondary"] == []


def _round_flow() -> dict:
    return _flow((ROUND_PACK, "t", copy.deepcopy(ROUND_PACK_FLOW)))


def test_level_2_takes_the_earlier_of_condition_and_stale_nodes():
    flow = _round_flow()
    pending = dict(intake_scope="ready", intake_apply="not_ready", intake_check="not_ready", core_publish="blocked")
    earlier = FS.suggest(flow, _states(flow, core_object="stale", **pending), {"stale_upstream": {"core.object": "资料"}})
    assert (_labels(earlier), earlier["note"]) == (["提取对象"], "资料有变化")
    later = FS.suggest(flow, _states(flow, core_validate="stale", **pending), {})
    assert (_labels(later), later["note"], later["primary"]["reason"]) == (["圈定范围"], None, "上传了新资料")  # 包声明的触发事件
    running = FS.suggest(flow, _states(flow, **dict(pending, intake_scope="running")), {})
    assert running["primary"] is None
    nxt = FS.suggest(flow, _states(flow, **dict(pending, intake_scope="confirmed", intake_apply="ready")), {})
    assert _labels(nxt) == ["范围内修改"]
    gone = FS.suggest(flow, _states(flow, absent=("core.update", *ROUND_PACK_FLOW_IDS), trace_trace="ready",
                                    core_publish="not_ready"), {})
    assert _labels(gone)[:1] == ["场景模拟"]  # 条件节点处理完就不在流程里，接着推第 3 级


ROUND_PACK_FLOW_IDS = tuple(node["id"] for node in ROUND_PACK_FLOW["nodes"])


def test_condition_nodes_are_offered_and_empty_done_nodes_never():
    # 增量更新的 done 为空：条件节点做没做完由条件说了算，条件成立（出现了）就在第 2 级推
    update = FS.suggest(FLOW, _states(FLOW, absent=(), core_update="ready", **OPTIONALS_READY), {})
    assert (_labels(update), update["primary"]["reason"]) == (["增量更新"], "上传了新资料")
    probe = ("probe", "t", {"nodes": [
        {"id": "probe.note", "title": "记录备注", "kind": "agent", "need": "required", "after": "core.validate",
         "done": [], "actions": [{"label": "记录备注", "command": "/probe-note"}]},
        {"id": "probe.tidy", "title": "整理备注", "kind": "agent", "need": "optional", "after": "core.operation",
         "done": [], "actions": [{"label": "整理备注", "command": "/probe-tidy"}]}]})
    flow = _flow(probe)
    third = FS.suggest(flow, _states(flow, probe_note="ready", trace_trace="ready", core_publish="not_ready"), {})
    assert _labels(third)[:1] == ["场景模拟"]
    fourth = FS.suggest(flow, _states(flow, probe_note="ready", probe_tidy="ready", core_metric="ready"), {})
    assert _labels(fourth) == ["提取规则与指标"]
    nothing = FS.suggest(flow, _states(flow, probe_note="ready", probe_tidy="ready"), {})
    assert nothing["primary"] is None


# ---- 三个出口一致 ----


def _outlets(root: Path) -> tuple[dict, dict, str]:
    built = view.build(root)["suggestions"]
    helper = next_steps.suggest(root)
    return built, helper, injection.next_step_hint(root)


def test_backend_assistant_copy_and_injection_agree(env):  # noqa: F811
    """后端 FL-01、助手副本的 state_helper suggest、注入给模型的那一行：几个典型状态下给同一份。"""
    _client, _workspaces, _manager, _project_id, root = env
    research = "# 合成资料分析\n\n门店按日补货。\n"
    steps = [
        ("新建", lambda: None, ["资料分析"]),
        ("报告待核验：核验排第一", lambda: main_write(root, "research/合成主题.md", research),
         ["核验资料分析报告", "资料分析", "追加资料分析"]),
        ("对象已提取", lambda: main_write(root, "ontology/object/门店.md", obj("门店", "合成门店")), ["提取操作"]),
        ("建模完成", lambda: main_write(root, "ontology/operation/补货.md", "## 逻辑描述\n\n读取[[门店]]。\n"), ["校验"]),
        ("报告待处理", lambda: main_write(root, "reports/20261101-semantic.md", "# 语义检查\n\n- [ ] ❌ 合成问题\n"), ["校验"]),
        ("校验完成", lambda: main_write(root, "reports/20261101-semantic.md", "# 语义检查\n\n- [x] ❌ 合成问题\n"),
         ["场景模拟", "交互模拟", "生成示例数据"]),
        ("场景模拟完成", lambda: main_write(root, "ontology/testcase/补货-正常.md", "# 合成场景\n"), ["发布"]),
        ("全部完成：可选节点（还没有流程图，全量刷新、校验不出现）", lambda: git(root, "tag", "release/v0.1"), ["绘制流程图"]),
        ("对象有变化", lambda: main_write(root, "ontology/object/门店.md", obj("门店", "合成门店，改过描述")), ["提取操作"]),
    ]
    for name, change, labels in steps:
        change()
        built, helper, hint = _outlets(root)
        assert _labels(built) == labels, name
        assert helper == {key: value for key, value in built.items() if key != "rev"}, name
        assert hint.startswith("下一步建议（与界面上的建议按钮同一份，仅供判断，不要在回复里列出）："), name
        positions = [hint.index(label) for label in labels]
        assert positions == sorted(positions), name
    assert built["note"] == "对象有变化"


# ---- 生成脚本不带目标 ----


def test_codegen_without_target_scans_first_then_asks():
    node = next(node for node in FLOW["nodes"] if node["id"] == "codegen.codegen")
    assert need.primary_command(node) == "/ontology-codegen" and node["actions"][0]["command"] == "/ontology-codegen"
    text = (ASSETS / "packs" / "codegen" / "commands" / "ontology-codegen.md").read_text(encoding="utf-8")
    branch = text[text.index("**If $ARGUMENTS empty**"):text.index("## Step 3")]
    assert "ontology-codegen-scan.md" in branch and "确认卡" in branch and "acquire codegen confirm" in branch
    assert "拒绝" not in branch and branch.index("扫描") < branch.index("确认卡")
