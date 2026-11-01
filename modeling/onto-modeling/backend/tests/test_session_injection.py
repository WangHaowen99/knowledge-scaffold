"""宿主注入（session_host/injection.py）与 Jiuwen 接入的测试。

对照用例直接跑资产里的两个 hook 脚本本体（Claude 引擎仍靠它们），保证移植后的
阶段提示与业务知识信号逐字一致；bash、python3、git 不可用时跳过对照用例。
测试数据全部为合成内容。
"""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config
from app.services.engines.base import EngineContext
from app.services.engines.jiuwen import JiuwenEngine
from app.services.script_env import minimal_env
from app.services.session_host import injection

ASSETS_DIR = Path(config.ASSETS_DIR)
SCRIPTS_DIR = ASSETS_DIR / "workspace-toolkit" / "scripts"

needs_shell = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("python3") and shutil.which("git")),
    reason="对照用例需要 bash、python3 与 git",
)


def make_workspace(root: Path) -> Path:
    """与真实项目工作区同构：git 仓库，.claude 符号链接到资产里的工具箱。"""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=minimal_env())
    (root / ".claude").symlink_to(ASSETS_DIR / "workspace-toolkit")
    return root


def write(path: Path, text: str, mtime: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def run_hook(script: str, cwd: Path, stdin: str = "") -> str:
    """跑 hook 脚本本体，返回 additionalContext（没有输出为空串）。"""
    proc = subprocess.run(
        ["bash", str(SCRIPTS_DIR / script)],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env=minimal_env(),
    )
    out = proc.stdout.strip()
    if not out:
        return ""
    return json.loads(out.splitlines()[-1])["hookSpecificOutput"]["additionalContext"]


# ---- 业务知识信号 ----


PROBE_CASES = [
    "帮我看看订单对象的属性",
    "不对，配送时长应该是从接单开始算",
    "我们公司规则是满 100 元免配送费",
    "其实通常由站长审核，行业里也是这样，必须留痕",
    "实际上你弄错了，标准做法是先校验再派单",
    "   ",
    "",
    "/ontology-research 示例领域",
]


def test_knowledge_hint_lists_hits_by_category_in_fixed_order():
    hint = injection.knowledge_signal_hint("必须先建对象。其实我们这边通常不这样")
    assert hint.startswith(
        "【业务知识信号词探针】检测到 — 纠错信号：其实；业务常识/规范信号：我们这边、通常、必须。\n"
    )
    assert "BUSINESS_KNOWLEDGE_SNIPPET.md" in hint
    assert "knowledge_helper.py add" in hint


def test_knowledge_hint_empty_without_signal():
    assert injection.knowledge_signal_hint("帮我画一张流程图") == ""
    assert injection.knowledge_signal_hint("  \n ") == ""
    assert injection.knowledge_signal_hint("") == ""


@needs_shell
@pytest.mark.parametrize("prompt", PROBE_CASES)
def test_knowledge_hint_matches_probe_script(tmp_path, prompt):
    expected = run_hook(
        "knowledge_signal_probe.sh",
        tmp_path,
        json.dumps({"prompt": prompt}, ensure_ascii=False),
    )
    assert injection.knowledge_signal_hint(prompt) == expected


# ---- 阶段提示 ----


def test_stage_hint_none_outside_ontology_workspace(tmp_path):
    assert injection.stage_hint(tmp_path) is None


@needs_shell
def test_stage_hint_reports_stage_locks_and_latest_report(tmp_path):
    ws = make_workspace(tmp_path / "ws")
    write(ws / "ontology" / "object" / "示例订单.md", "# 示例订单\n")
    write(ws / ".ontology-state" / "lock-validate.json", "{}")
    write(ws / ".ontology-state" / "lock-extract-operation.json", "{}")
    write(ws / "reports" / "check-01.md", "- [ ] 旧问题\n", mtime=1_700_000_000)
    write(ws / "reports" / "check-02.md", "- [ ] 待补关系\n- [x] 已修\n- [ ] 待补属性\n", mtime=1_700_000_100)
    assert injection.stage_hint(ws) == (
        "本体项目当前阶段：对象已提取。 进行中的命令：extract-operation validate。"
        " 最近报告：check-02.md（2 个 [ ] 待处理）"
    )


@needs_shell
def test_stage_hint_runs_assets_script_not_workspace_copy(tmp_path):
    # 工作区里的 .claude/scripts 可被改写：阶段只能来自资产目录里的 state_helper
    ws = tmp_path / "ws"
    write(ws / ".claude" / "scripts" / "state_helper.py", "print('被改写的阶段')\n")
    assert injection.stage_hint(ws) == "本体项目当前阶段：未开始。"


def test_stage_hint_falls_back_to_not_started_when_script_fails(tmp_path, monkeypatch):
    # 与 hook 一样：脚本出错没有输出时按「未开始」
    ws = tmp_path / "ws"
    (ws / ".claude" / "scripts").mkdir(parents=True)
    monkeypatch.setattr(config, "ASSETS_DIR", tmp_path / "no-assets")
    assert injection.stage_hint(ws) == "本体项目当前阶段：未开始。"


def test_stage_hint_skipped_when_stage_detection_times_out(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / ".claude" / "scripts").mkdir(parents=True)

    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr(injection.subprocess, "run", slow)
    assert injection.stage_hint(ws) is None
    result = injection.build_context(ws, user_text="继续", session_start=True, last_injected="abc")
    assert result.text == "" and result.digest == "abc"


@needs_shell
@pytest.mark.parametrize("scenario", ["empty", "full", "clean-report", "research-pending"])
def test_stage_hint_matches_session_start_hook(tmp_path, scenario):
    ws = make_workspace(tmp_path / "ws")
    if scenario == "full":
        write(ws / "ontology" / "object" / "示例订单.md", "# 示例订单\n")
        write(ws / "ontology" / "operation" / "示例派单.md", "# 示例派单\n")
        write(ws / ".ontology-state" / "lock-validate.json", "{}")
        write(ws / ".ontology-state" / "lock-mock.json", "{}")
        write(ws / "reports" / "校验报告-示例-1.md", "- [ ] a\n", mtime=1_700_000_000)
        write(ws / "reports" / "校验报告-示例-2.md", "- [ ] a\n  - [ ] 缩进不算\n- [ ] b\n", mtime=1_700_000_200)
    elif scenario == "clean-report":
        write(ws / "ontology" / "diagrams" / "示例流程.drawio", "<mxfile/>")
        write(ws / "reports" / "overview.md", "- [x] 已处理\n")
    elif scenario == "research-pending":
        write(ws / "research" / "示例资料分析.md", "# 示例资料分析\n")
    expected = run_hook("session_start_hook.sh", ws)
    assert expected, "hook 应有输出"
    assert injection.stage_hint(ws) == expected


# ---- 入口：去重与拼接 ----


def test_build_context_dedupes_unchanged_stage_and_probes_each_message(tmp_path, monkeypatch):
    stage = {"text": "本体项目当前阶段：未开始。"}
    monkeypatch.setattr(injection, "stage_hint", lambda root: stage["text"])

    first = injection.build_context(tmp_path, user_text="开始建模", session_start=True, last_injected=None)
    assert first.text == "本体项目当前阶段：未开始。"
    assert first.stage_hint and not first.knowledge_hint and first.digest

    # 阶段没变：不重复；知识信号按本条消息判断
    second = injection.build_context(
        tmp_path, user_text="其实这里应该是两个对象", session_start=False, last_injected=first.digest
    )
    assert second.stage_hint == "" and second.digest == first.digest
    assert second.text == second.knowledge_hint
    assert "纠错信号：应该是、其实" in second.text

    third = injection.build_context(tmp_path, user_text="好的", session_start=False, last_injected=second.digest)
    assert third.text == ""

    # 会话常驻期间阶段变了：再带上
    stage["text"] = "本体项目当前阶段：对象已提取。"
    fourth = injection.build_context(
        tmp_path, user_text="我们公司通常按周盘点", session_start=False, last_injected=third.digest
    )
    assert fourth.digest != third.digest
    assert fourth.text == fourth.stage_hint + "\n\n" + fourth.knowledge_hint

    # 新引擎实例（重连、续接）的第一条：内容没变也要带
    fifth = injection.build_context(tmp_path, user_text="继续", session_start=True, last_injected=fourth.digest)
    assert fifth.text == "本体项目当前阶段：对象已提取。"


def test_compose_query_prefixes_context_and_keeps_user_text():
    assert injection.compose_query("原话", "") == "原话"
    query = injection.compose_query("原话", "本体项目当前阶段：未开始。")
    assert query.startswith("<system-reminder>\n")
    assert "本体项目当前阶段：未开始。\n</system-reminder>" in query
    assert query.endswith("\n\n原话")


# ---- Jiuwen 接入 ----


def make_ctx(cwd: Path, errors: list | None = None) -> EngineContext:
    async def permission(tool_name, tool_input, title, description):
        return {"behavior": "allow", "updated_input": None}

    return EngineContext(
        project_id="p-demo",
        cwd=cwd,
        request_permission=permission,
        load_resume_token=lambda: None,
        save_resume_token=lambda token: None,
        clear_resume_token=lambda: None,
        log_error=lambda kind, text: (errors if errors is not None else []).append((kind, text)),
        load_history=lambda: [],
    )


def drain(engine: JiuwenEngine) -> list[dict]:
    events = []
    while not engine._queue.empty():
        events.append(engine._queue.get_nowait())
    return events


def answer(text):
    async def gen():
        yield SimpleNamespace(type="llm_output", payload={"content": text})

    return gen()


@needs_shell
def test_jiuwen_first_message_carries_stage_and_later_only_on_change(tmp_path, monkeypatch):
    from openjiuwen.core.runner import Runner

    ws = make_workspace(tmp_path / "ws")
    queries = []

    def fake_stream(agent, inputs, session=None):
        queries.append(inputs["query"])
        return answer("收到")

    monkeypatch.setattr(Runner, "run_agent_streaming", staticmethod(fake_stream))
    engine = JiuwenEngine(make_ctx(ws))
    engine.client = object()

    async def scenario():
        await engine._run_turn("开始建模")
        await engine._run_turn("好的，继续")
        # 两轮之间阶段变了（模型或专家落了对象文档）
        write(ws / "ontology" / "object" / "示例订单.md", "# 示例订单\n")
        await engine._run_turn("其实订单还要拆出明细")
        return drain(engine)

    events = asyncio.run(scenario())
    first, second, third = queries
    assert first == injection.compose_query("开始建模", "本体项目当前阶段：未开始。")
    assert second == "好的，继续"
    assert third.startswith("<system-reminder>\n")
    assert "本体项目当前阶段：对象已提取。" in third
    assert "纠错信号：其实" in third
    assert third.endswith("\n\n其实订单还要拆出明细")
    # 注入只进发给模型的 query，引擎产出的事件里没有
    assert "本体项目当前阶段" not in json.dumps(events, ensure_ascii=False)


def test_jiuwen_buffered_messages_get_injection_when_delivered(tmp_path, monkeypatch):
    from openjiuwen.core.runner import Runner

    stage = {"text": "本体项目当前阶段：未开始。"}
    monkeypatch.setattr(injection, "stage_hint", lambda root: stage["text"])
    queries = []
    gate = {}

    def fake_stream(agent, inputs, session=None):
        queries.append(inputs["query"])
        if len(queries) > 1:
            return answer("收到")

        async def gen():
            gate["running"].set()
            await gate["release"].wait()
            yield SimpleNamespace(type="llm_output", payload={"content": "第一轮"})

        return gen()

    monkeypatch.setattr(Runner, "run_agent_streaming", staticmethod(fake_stream))
    engine = JiuwenEngine(make_ctx(tmp_path))
    engine.client = object()

    async def scenario():
        gate["running"], gate["release"] = asyncio.Event(), asyncio.Event()
        await engine.send("第一条")
        await asyncio.wait_for(gate["running"].wait(), timeout=2)
        # 轮内追加：积压在引擎里，记的是原文
        await engine.send("我们这边规则是先审后派")
        await engine.send("/ontology-research 示例领域")
        assert engine._buffered == ["我们这边规则是先审后派", "/ontology-research 示例领域"]
        stage["text"] = "本体项目当前阶段：资料分析待核验。"
        gate["release"].set()
        await asyncio.wait_for(engine._turn_task, timeout=5)

    asyncio.run(scenario())
    first, second, third = queries
    assert first == injection.compose_query("第一条", "本体项目当前阶段：未开始。")
    # 积压的消息送达时才算阶段：带上新阶段与本条的知识信号
    assert "本体项目当前阶段：资料分析待核验。" in second
    assert "业务常识/规范信号：我们这边、规则是" in second
    assert second.endswith("\n\n我们这边规则是先审后派")
    # 斜杠命令照常展开；信号词只看用户原文，命令文档里的「必须」等不算
    assert "示例领域" in third and "state_helper.py" in third
    assert any(word in third for word in injection.CORRECTION_SIGNALS + injection.CONVENTION_SIGNALS)
    assert not third.startswith("<system-reminder>")


def test_jiuwen_interrupt_reinjects_stage_next_message(tmp_path, monkeypatch):
    from openjiuwen.core.runner import Runner

    monkeypatch.setattr(injection, "stage_hint", lambda root: "本体项目当前阶段：未开始。")
    queries = []
    started = {}

    def fake_stream(agent, inputs, session=None):
        queries.append(inputs["query"])
        if len(queries) > 1:
            return answer("收到")

        async def gen():
            started["event"].set()
            await asyncio.Event().wait()
            yield

        return gen()

    monkeypatch.setattr(Runner, "run_agent_streaming", staticmethod(fake_stream))
    engine = JiuwenEngine(make_ctx(tmp_path))
    engine.client = object()

    async def scenario():
        started["event"] = asyncio.Event()
        running = asyncio.create_task(engine._run_turn("第一条"))
        await asyncio.wait_for(started["event"].wait(), timeout=2)
        running.cancel()
        try:
            await running
        except asyncio.CancelledError:
            pass
        await engine._run_turn("第二条")

    asyncio.run(scenario())
    # 被打断的那轮未必进了存档：下一条重新带上阶段提示
    assert queries[1] == injection.compose_query("第二条", "本体项目当前阶段：未开始。")


def test_jiuwen_injection_failure_does_not_block_message(tmp_path, monkeypatch):
    from openjiuwen.core.runner import Runner

    def broken(*args, **kwargs):
        raise RuntimeError("模拟注入失败")

    monkeypatch.setattr(injection, "build_context", broken)
    queries = []

    def fake_stream(agent, inputs, session=None):
        queries.append(inputs["query"])
        return answer("收到")

    monkeypatch.setattr(Runner, "run_agent_streaming", staticmethod(fake_stream))
    errors = []
    engine = JiuwenEngine(make_ctx(tmp_path, errors))
    engine.client = object()

    asyncio.run(engine._run_turn("其实可以先做对象"))
    assert queries == ["其实可以先做对象"]
    assert errors == [("injection", "模拟注入失败")]
    assert drain(engine)[-1]["subtype"] == "success"
