"""专家插话即目标：指令寿命分层的运行时行为。

实测过的两类失败驱动这套机制：一次性动作（「把储罐利用率移出范围」）被
「不要自行改换目标」的禁令挡掉；长期约束（「单价不许写死」）只在恰好留在
窗口里时被遵守。修法不是再加一个权威声音，而是让插话**成为**那个唯一目标，
并按寿命分别落到约束登记表（撤销才失效）和待办（做完就失效）。
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from modeling_agent.agent import _system_prompt
from modeling_agent.app import create_app
from modeling_agent.errors import AgentRuntimeError
from modeling_agent.store import RuntimeStore

from conftest import FakeGateway, ScriptedModel, ToolTurn, service


GOAL_HEADING = "本轮目标（服务端指派）"


async def wait_for_status(runtime, run_id: str, statuses: set[str], timeout: float = 20) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        run = runtime.store.require_run(run_id).public()
        if run["status"] in statuses:
            return run
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} did not reach {statuses}")


def system_texts(model) -> list[str]:
    out = []
    for item in model.stream_inputs:
        messages = item.get("messages") if isinstance(item, dict) else item
        text = ""
        for message in messages or []:
            role = getattr(message, "role", None) or (
                message.get("role") if isinstance(message, dict) else None
            )
            if role != "system":
                continue
            content = getattr(message, "content", None) or (
                message.get("content") if isinstance(message, dict) else ""
            )
            text += str(content or "")
        out.append(text)
    return out


def seed_expert_interjection(store, project_id: str, text: str) -> int:
    steering_id = store.queue_steering(project_id, "seed_run", text, origin="expert")
    store.mark_steering_delivered(steering_id)
    return steering_id


# ---------------- store 层：生命周期与登记表 ----------------


def test_store_migrates_legacy_steering_schema(tmp_path) -> None:
    """存量库升级：旧版 steering_messages 没有处置列。

    流水线实测事故：SCHEMA 里的处置索引引用 origin 列，而存量库的表已存在、
    CREATE TABLE IF NOT EXISTS 不补列，executescript 阶段建索引直接
    no such column 崩掉服务。索引必须在迁移补列之后创建；本用例先按旧版
    结构建库，再初始化 RuntimeStore，守住这条升级路径。
    """
    import sqlite3

    path = tmp_path / "legacy.sqlite"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """CREATE TABLE steering_messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id TEXT NOT NULL,
          run_id TEXT NOT NULL,
          text TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued',
          created_at TEXT NOT NULL,
          delivered_at TEXT NOT NULL DEFAULT ''
        );"""
    )
    legacy.execute(
        """INSERT INTO steering_messages(project_id, run_id, text, status, created_at)
        VALUES ('p_legacy', 'r_legacy', '历史插话', 'delivered', '2020-01-01T00:00:00Z')""",
    )
    legacy.commit()
    legacy.close()

    store = RuntimeStore(path)
    # 历史插话按 system 来源迁移：它们早已随当时的对话处理完，
    # 不能在升级后复活成本轮目标
    assert store.pending_interjections("p_legacy") == []
    fresh = seed_expert_interjection(store, "p_legacy", "升级后的新插话")
    assert [item["id"] for item in store.pending_interjections("p_legacy")] == [fresh]


def test_store_interjection_and_constraint_lifecycle(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")

    # system 来源的插话不进处置队列：投递即完结，否则续跑提示会复活成目标
    system_id = store.queue_steering("p1", "r1", "服务端组装的续跑提示")
    store.mark_steering_delivered(system_id)
    assert store.pending_interjections("p1") == []

    expert_id = seed_expert_interjection(store, "p1", "单价不许写死")
    pending = store.pending_interjections("p1")
    assert [item["id"] for item in pending] == [expert_id]

    # 处置为约束：登记表出现生效条款，插话离开待处置队列
    store.dispose_interjection(
        "p1", expert_id, resolution="constraint", detail={"constraintId": 0},
    )
    assert store.pending_interjections("p1") == []
    constraint = store.register_constraint("p1", "单价必须从数据源读取", source_steering_id=expert_id)
    assert [item["text"] for item in store.active_constraints("p1")] == ["单价必须从数据源读取"]

    # 覆盖：旧条款置为 superseded，新条款生效——只进不出的规矩表会自相矛盾
    replaced = store.register_constraint(
        "p1", "单价上限从 100 改为 80", replaces_id=constraint["id"],
    )
    statuses = {item["id"]: item["status"] for item in store.list_constraints("p1")}
    assert statuses[constraint["id"]] == "superseded"
    assert statuses[replaced["id"]] == "active"

    # 撤销即失效，重复撤销被拒绝
    store.revoke_constraint("p1", replaced["id"], reason="专家取消")
    assert store.active_constraints("p1") == []
    with pytest.raises(AgentRuntimeError):
        store.revoke_constraint("p1", replaced["id"])

    # 重复处置同一条插话被拒绝：处置痕迹是一次性的
    with pytest.raises(AgentRuntimeError):
        store.dispose_interjection("p1", expert_id, resolution="rejected", detail={})


def test_store_task_disposition_survives_and_completes(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    interjection = seed_expert_interjection(store, "p2", "把储罐利用率移出范围")
    store.dispose_interjection(
        "p2", interjection, resolution="task", detail={"text": "把储罐利用率移出建模范围"},
    )
    tasks = store.open_interjection_tasks("p2")
    assert [item["text"] for item in tasks] == ["把储罐利用率移出建模范围"]

    store.complete_interjection_task("p2", interjection, evidence="goal.md scope 节已删除该条")
    assert store.open_interjection_tasks("p2") == []
    with pytest.raises(AgentRuntimeError):
        store.complete_interjection_task("p2", interjection, evidence="再次完成")


def test_store_escalation_reopens_after_answer(tmp_path) -> None:
    """升级不是终态：裁决卡被回答后插话必须回到待处置队列。

    否则一张无关的冲突卡恰好出现，就会把插话永久吞掉——错误升级最多
    延迟处置一次，不能变成静默丢弃的新通道。
    """
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    interjection = seed_expert_interjection(store, "p3", "顺手补一下基线里的输入定义")
    escalated = store.escalate_interjections("p3", "interaction_x")
    assert escalated == [interjection]
    assert store.pending_interjections("p3") == []

    reopened = store.reopen_escalated_interjections("interaction_x")
    assert reopened == [interjection]
    assert [item["id"] for item in store.pending_interjections("p3")] == [interjection]


# ---------------- 运行时：插话成为唯一目标 ----------------


@pytest.mark.asyncio
async def test_pending_interjection_becomes_the_turn_goal(settings) -> None:
    """有未处置插话时，插话即本轮目标；门禁项让位。

    这就是「判据从『是不是续跑』换成『有没有待处理指令』」：首轮也要指派，
    否则插话只能作为普通消息随窗口滚动被静默丢弃。
    """
    model = ScriptedModel(
        [[ToolTurn("update_work_notes", {"markdown": "# 进展"})], []],
        final="本轮已完成。",
    )
    gateway = FakeGateway({
        "gate_status": {
            "pass": False,
            "blockers": ["完成业务操作「demo-operation-alpha」的脚本"],
            "agentActionable": ["完成业务操作「demo-operation-alpha」的脚本"],
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        interjection = seed_expert_interjection(
            runtime.store, "project_interjection_goal", "单价不许写死",
        )
        started = await runtime.start_run(
            project_id="project_interjection_goal", token="token_1", message="继续",
        )
        await wait_for_status(runtime, started["id"], {"completed", "failed", "waiting", "suspended"})

        texts = system_texts(model)
        assert texts, "模型没有被调用"
        # 首轮就带插话目标——判据已经不是「是不是续跑」
        assert f"专家插话 #{interjection} 尚未处置" in texts[0]
        assert "单价不许写死" in texts[0]
        goal = texts[0][texts[0].find(GOAL_HEADING):]
        # 门禁项让位：插话是唯一目标，不是并列的第二个声音
        assert "demo-operation-alpha" not in goal
        # 目标文本必须带处置出口和撞墙升级指引
        assert "dispose_interjection" in goal
        assert "conflict_decision" in goal
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_disposed_interjection_stops_being_the_goal(settings) -> None:
    """处置即消费：登记为约束后，插话不再作为目标重复注入。

    没有 consumed 位的「插话即目标」正是被否掉的「逐轮重发原文」方案——
    无完成状态的重复注入，最终变成噪音。
    """
    model = ScriptedModel(
        [
            [ToolTurn("dispose_interjection", {
                "interjection_id": 0,  # 占位，启动前改写
                "resolution": "constraint",
                "text": "单价必须从数据源读取，不许写死",
            })],
            [ToolTurn("update_work_notes", {"markdown": "# 进展"})],
            [],
        ],
        final="本轮已完成。",
    )
    gateway = FakeGateway({
        "gate_status": {"pass": False, "blockers": ["demo blocker"], "agentActionable": ["demo blocker"]},
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        interjection = seed_expert_interjection(
            runtime.store, "project_interjection_consumed", "单价不许写死",
        )
        model.script[0][0].arguments["interjection_id"] = interjection
        started = await runtime.start_run(
            project_id="project_interjection_consumed", token="token_1", message="继续",
        )
        await wait_for_status(runtime, started["id"], {"completed", "failed", "waiting", "suspended"})

        assert runtime.store.pending_interjections("project_interjection_consumed") == []
        constraints = runtime.store.active_constraints("project_interjection_consumed")
        assert [item["text"] for item in constraints] == ["单价必须从数据源读取，不许写死"]
        # 处置后的续跑轮回到门禁目标，且不再出现插话目标——同一轮内的多次模型
        # 调用共享轮初构建的 prompt，所以按内容定位处置后的轮次，不按序号。
        texts = system_texts(model)
        gate_goal_texts = [
            text for text in texts
            if GOAL_HEADING in text and "demo blocker" in text[text.find(GOAL_HEADING):]
        ]
        assert gate_goal_texts, "处置后应有回到门禁目标的续跑轮"
        for text in gate_goal_texts:
            assert "专家插话 #" not in text[text.find(GOAL_HEADING):], (
                "已处置的插话仍被重复注入为目标"
            )
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_registered_constraint_is_injected_every_round(settings) -> None:
    """登记表条款每轮注入 system 段——这是「单价」类失败的唯一修法：
    长期约束当一轮目标注入，那一轮结束它就消失了。"""
    model = ScriptedModel([[ToolTurn("update_work_notes", {"markdown": "# 进展"})], []], final="完成。")
    runtime = service(settings, model, FakeGateway({"gate_status": {"pass": True, "blockers": []}}))
    await runtime.startup()
    try:
        constraint = runtime.store.register_constraint(
            "project_constraint_injection", "单价必须从数据源读取，不许写死",
        )
        started = await runtime.start_run(
            project_id="project_constraint_injection", token="token_1", message="继续",
        )
        await wait_for_status(runtime, started["id"], {"completed", "failed", "waiting", "suspended"})
        for text in system_texts(model):
            assert "专家约束（长期有效，直到专家撤销）" in text
            assert f"[约束 #{constraint['id']}] 单价必须从数据源读取，不许写死" in text
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_undisposed_interjection_is_made_visible_on_settle(settings) -> None:
    """模型拒绝处置时不能无限强制续跑，但沉默必须变成可见事件。"""
    model = ScriptedModel([[], [], [], []], final="我先做点别的。")
    runtime = service(settings, model, FakeGateway({"gate_status": {"pass": True, "blockers": []}}))
    await runtime.startup()
    try:
        seed_expert_interjection(runtime.store, "project_interjection_silent", "单价不许写死")
        started = await runtime.start_run(
            project_id="project_interjection_silent", token="token_1", message="继续",
        )
        await wait_for_status(runtime, started["id"], {"completed", "failed", "waiting", "suspended"})
        events = runtime.store._db.execute(  # noqa: SLF001 - 测试直接核对事件流
            "SELECT payload_json FROM agent_events WHERE project_id = ? AND kind = 'tool'",
            ("project_interjection_silent",),
        ).fetchall()
        assert any("interjection_undisposed" in str(row["payload_json"]) for row in events), (
            "插话未处置就收尾时必须发可见事件，沉默不能是默认状态"
        )
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_conflict_decision_card_escalates_and_answer_reopens(settings) -> None:
    """冲突裁决卡出现时服务端确定性绑定升级；专家回答后插话重新开放。"""
    question = {
        "question": "这条指令会破坏增量隔离，是否退出增量改走全量轮次？",
        "kind": "conflict_decision",
        "options": [
            {"label": "确认转换并执行", "description": "退出增量", "value": "convert"},
            {"label": "撤回指令", "description": "保持增量", "value": "withdraw"},
        ],
    }
    model = ScriptedModel([[ToolTurn("ask", question)], []], final="收到。")
    runtime = service(settings, model, FakeGateway({"gate_status": {"pass": True, "blockers": []}}))
    await runtime.startup()
    try:
        interjection = seed_expert_interjection(
            runtime.store, "project_escalation", "顺手补一下基线里的输入定义",
        )
        started = await runtime.start_run(
            project_id="project_escalation", token="token_1", message="继续",
        )
        waiting = await wait_for_status(runtime, started["id"], {"waiting"})

        assert runtime.store.pending_interjections("project_escalation") == []
        row = runtime.store._db.execute(  # noqa: SLF001
            "SELECT disposition, disposition_json FROM steering_messages WHERE id = ?",
            (interjection,),
        ).fetchone()
        assert row["disposition"] == "escalated"
        assert waiting["interactionId"] in str(row["disposition_json"])

        await runtime.answer_interaction(
            project_id="project_escalation",
            interaction_id=waiting["interactionId"],
            token="token_1",
            answer={"value": "withdraw"},
        )
        await wait_for_status(runtime, started["id"], {"completed", "failed", "waiting", "suspended"})
        assert [item["id"] for item in runtime.store.pending_interjections("project_escalation")] == [
            interjection
        ], "裁决卡回答后插话必须重新开放，由模型按裁决收尾"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_satisfied_disposition_requires_evidence(settings) -> None:
    """「已满足」是模型自报，天然是合理化逃生口：必须强制证据锚点。"""
    model = ScriptedModel(
        [
            [ToolTurn("dispose_interjection", {
                "interjection_id": 0, "resolution": "satisfied",
            })],
            [],
        ],
        final="完成。",
    )
    runtime = service(settings, model, FakeGateway({"gate_status": {"pass": True, "blockers": []}}))
    await runtime.startup()
    try:
        interjection = seed_expert_interjection(
            runtime.store, "project_evidence_required", "把储罐利用率移出范围",
        )
        model.script[0][0].arguments["interjection_id"] = interjection
        started = await runtime.start_run(
            project_id="project_evidence_required", token="token_1", message="继续",
        )
        await wait_for_status(runtime, started["id"], {"completed", "failed", "waiting", "suspended"})
        # 无证据的满足声明被拒绝，插话保持未处置
        assert [item["id"] for item in runtime.store.pending_interjections("project_evidence_required")] == [
            interjection
        ]
    finally:
        await runtime.shutdown()


# ---------------- API：登记表页面可见与专家改判 ----------------


def test_constraints_api_lists_and_revokes(settings) -> None:
    model = ScriptedModel([], final="完成。")
    runtime = service(settings, model, FakeGateway())
    with TestClient(create_app(runtime)) as client:
        constraint = runtime.store.register_constraint("project_api", "单价不许写死")

        unauthorized = client.get("/v1/projects/project_api/constraints")
        assert unauthorized.status_code == 401

        listed = client.get(
            "/v1/projects/project_api/constraints",
            headers={"Authorization": "Bearer token_1"},
        )
        assert listed.status_code == 200
        payload = listed.json()["constraints"]
        assert [item["text"] for item in payload] == ["单价不许写死"]
        assert payload[0]["status"] == "active"

        revoked = client.post(
            f"/v1/projects/project_api/constraints/{constraint['id']}/revoke",
            headers={"Authorization": "Bearer token_1"},
            json={"reason": "专家在页面上撤销"},
        )
        assert revoked.status_code == 200
        assert runtime.store.active_constraints("project_api") == []

        again = client.post(
            f"/v1/projects/project_api/constraints/{constraint['id']}/revoke",
            headers={"Authorization": "Bearer token_1"},
        )
        assert again.status_code == 409


def test_system_prompt_renders_constraint_slot() -> None:
    prompt = _system_prompt(
        "技能正文",
        {"validatorVersion": "v1"},
        constraints=({"id": 3, "text": "单价不许写死"},),
    )
    assert "专家约束（长期有效，直到专家撤销）" in prompt
    assert "[约束 #3] 单价不许写死" in prompt
    assert "revoke_constraint" in prompt

    without = _system_prompt("技能正文", {"validatorVersion": "v1"})
    assert "专家约束（长期有效，直到专家撤销）" not in without
