"""V 阶段真实模型下自主构建的发现（栈 b，内测引擎）。"""
from __future__ import annotations

from tests.autobuild_helpers import ab  # noqa: F401 - 自主构建用例的项目夹具


def test_ledger_anchor_without_scheme():
    """真实模型按对话里的出处写法写依据（`resource/资料.docx#p:6`，不带 `file:`），台账里原来一个出处都没有。"""
    from app.services.autobuild import decide

    text = "置信度 0.9 · 依据：resource/others/合成制度.docx#p:6，另见 resource/others/合成细则.md#L3。"
    assert decide.anchors(text) == [
        {"n": 1, "anchor": "file:resource/others/合成制度.docx#p:6", "label": "合成制度.docx §6"},
        {"n": 2, "anchor": "file:resource/others/合成细则.md#L3", "label": "合成细则.md §L3"},
    ]
    # 带 scheme 的照旧；同一处写了两种写法只记一次
    assert decide.anchors("file:resource/a.docx#p:1 与 resource/a.docx#p:1") == [
        {"n": 1, "anchor": "file:resource/a.docx#p:1", "label": "a.docx §1"}]
    # 不是资料区的路径、不是出处的文字不算
    assert decide.anchors("依据：research_checker status=passed；见 ontology/object/报修单.md") == []


def test_hint_says_how_to_write_anchor():
    """注入里说清出处怎么写、写在哪（header 只放置信度：稳定引擎的 header 是短标签）。"""
    from app.services.autobuild import hint

    assert "置信度 0.xx" in hint.INSTRUCTIONS and "resource/" in hint.INSTRUCTIONS


def test_wait_turn_does_not_count_old_error_while_reconnecting(monkeypatch):
    """真实模型一次流式超时（这一轮出错）之后，运行器发续跑指令：会话还停在 error、正在重连，原来立刻又记一次出错，
    几毫秒里连记三次就按「引擎出错」整次运行停下，而续跑的那一轮照样在跑（运行停了它还改了 56 分钟）。"""
    import asyncio

    from app.services.autobuild import runner as runner_module

    class Session:
        conversation_id = "c1"
        state = "error"
        sending = 0
        finishing = False
        pending_prompts = ["继续"]
        turns_in_flight = 0
        deferred = []

        def __init__(self):
            self.connect_task = object()

        def is_connecting(self):
            return self.connect_task is not None

    session = Session()
    signal = asyncio.Event()

    class Manager:
        def signal(self, project_id):
            return signal

        def get(self, project_id):
            return session

        def queue_position(self, project_id):
            return None

    monkeypatch.setattr(runner_module, "_manager", lambda: Manager())

    async def accounted(self, project_id, run_id):
        return None

    monkeypatch.setattr(runner_module.AutobuildRunner, "_account", accounted)

    async def scenario():
        waiting = asyncio.create_task(runner_module.AutobuildRunner()._wait_turn("p1", "run-1", "c1"))
        await asyncio.sleep(0.05)
        assert not waiting.done()  # 重连中：不算出错，接着等
        # 连上、续跑的一轮跑完
        session.connect_task, session.pending_prompts, session.state = None, [], "idle"
        signal.set()
        return await asyncio.wait_for(waiting, 2)

    assert asyncio.run(scenario()) == "idle"
    # 连不上（重连结束仍是 error）：照常记出错
    session.state = "error"
    assert asyncio.run(runner_module.AutobuildRunner()._wait_turn("p1", "run-1", "c1")) == "error"


def test_matrix_does_not_mark_steps_left_out_as_done(ab):
    """策略关了「生成场景」：这一片合入后，运行详情的流水线把没做的「场景」也标成「自主完成」（流程里这一步还是 ready）。"""
    from tests.autobuild_helpers import start, wait_state

    client, _workspaces, project_id, _root = ab
    run = start(client, project_id, scope={"until_node": "core.model", "generate_scenarios": False,
                                            "mode": "selected", "slice_ids": ["s1"]},
                budget={"max_tokens": 500_000})
    done = wait_state(client, project_id, run["run_id"], "completed")
    cells = done["matrix"][0]["cells"]
    flow = client.get(f"/api/v1/projects/{project_id}/flow").json()
    stages = next(item for item in flow["slices"] if item["id"] == "s1")["stages"]
    assert "scenario" in cells and stages["scenario"] not in ("auto", "confirmed")
    assert cells["scenario"] == "skipped"
    assert cells["object"] == "auto"


def test_ledger_anchor_label_for_ranges():
    """真实模型写区间出处 `#p:10~p:11`，台账出处标签原来是「§10~p:11」。"""
    from app.services.autobuild import decide

    found = decide.anchors("依据：resource/others/合成制度.docx#p:10~p:11、resource/excel/合成清单.xlsx#sheet:备件")
    assert [item["label"] for item in found] == ["合成制度.docx §10~11", "合成清单.xlsx §备件"]
    assert found[0]["anchor"] == "file:resource/others/合成制度.docx#p:10~p:11"


def test_suspension_reason_says_why():
    """挂起的「原因」原来直接放问题的 header（真实运行里是「置信度 0.85」），看不出为什么挂起。"""
    from app.services.autobuild import decide
    from app.services.session_host import timeline

    def outcome(header: str, advanced: dict, labels=("确认（推荐）", "修改")) -> dict:
        tool_input = {"questions": [{"question": "合成规则的产物是否确认", "header": header,
                                     "options": [{"label": label} for label in labels]}]}
        question = timeline.build_questions("r1", tool_input, conversation_id="c")[0]
        return decide.decide(question, tool_input["questions"][0],
                             {"confidence_threshold": 0.8, "below_threshold": "conservative_assumption",
                              "must_suspend_kinds": [], **advanced})

    assert outcome("置信度 0.85", {"must_suspend_kinds": ["规则"]})["reason"] == "涉及「规则」，按策略一律挂起"
    assert outcome("置信度 0.6", {"below_threshold": "suspend"})["reason"] == "置信度 0.60，低于阈值 0.80"
    assert outcome("置信度 0.9", {}, labels=())["reason"] == "没有可选的选项"


def test_inbox_rejudge_of_an_autobuild_decision(ab):
    """待处理里对自主构建的决定点「改判为选中的选项」（IN-03 action=rejudge）原来走到流程节点的「重做这一步」，
    回 404 node_not_found（「这一步不存在」）。"""
    from tests.autobuild_helpers import AutoEngine, ask, start, wait_state, wait_until

    client, _workspaces, project_id, _root = ab
    AutoEngine.hooks["/ontology-extract-object:s1"] = [ask([
        {"question": "报修单号几位", "header": "置信度 0.6", "options": [{"label": "八位（推荐）"}, {"label": "十位（保守）"}]}])]
    run = start(client, project_id, scope={"mode": "selected", "slice_ids": ["s1"], "until_node": "core.model"},
                budget={"max_tokens": 500_000})
    wait_state(client, project_id, run["run_id"], "completed")
    items = client.get(f"/api/v1/projects/{project_id}/inbox", params={"filter": "autobuild_review"}).json()["items"]
    item = next(entry for entry in items if entry["kind"] == "review" and entry["title"].endswith("十位"))
    detail = client.get(f"/api/v1/projects/{project_id}/inbox/{item['id']}").json()["detail"]
    other = next(option for option in detail["options"] if option["label"].startswith("八位"))
    done = client.post(f"/api/v1/projects/{project_id}/inbox/{item['id']}/actions",
                       json={"action": "rejudge", "option_id": other["id"]})
    assert done.status_code == 200, done.text
    assert done.json()["result"]["entry"]["review_state"] == "rejudged"
    wait_until(lambda: any(text.startswith("/ontology-extract-object slice=s1\n\n改判：「报修单号几位」") for text in AutoEngine.sent))
