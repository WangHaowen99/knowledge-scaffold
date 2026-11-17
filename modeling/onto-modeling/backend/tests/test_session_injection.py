"""宿主注入（session_host/injection.py）与两个引擎接入的测试。

两个提示 hook（session_start_hook.sh、knowledge_signal_probe.sh）已退役（ADR-0034）。阶段提示只剩
injection.py 一份实现，下面的期望文本是退役前用 hook 脚本本体对同一组场景跑出的输出，保证移植后逐字一致；
包的提示规则以增量构建包的「结束本轮」为例（ADR-0045）。
bash、python3、git 不可用时跳过要起工作区的用例。测试数据全部为合成内容。
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app import config
from app.services.engines.base import EngineContext
from app.services.engines.jiuwen import JiuwenEngine
from app.services.script_env import minimal_env
from app.services.session_host import injection
from tests.proc import run as run_cmd

ASSETS_DIR = Path(config.ASSETS_DIR)
SCRIPTS_DIR = ASSETS_DIR / "workspace-toolkit" / "scripts"
needs_shell = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("python3") and shutil.which("git")),
    reason="要起 git 工作区并跑 state_helper",
)


def make_workspace(root: Path) -> Path:
    """与真实项目工作区同构：git 仓库，.claude 符号链接到资产里的工具箱。"""
    root.mkdir(parents=True, exist_ok=True)
    run_cmd(["git", "init", "-q"], cwd=root, check=True, env=minimal_env())
    (root / ".claude").symlink_to(ASSETS_DIR / "workspace-toolkit")
    return root


def write(path: Path, text: str, mtime: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


# ---- 包的提示规则（以增量构建包为例）----


def pack_rules() -> list[dict]:
    """增量构建包清单里声明的提示规则（官方包原文）。"""
    manifest = yaml.safe_load((ASSETS_DIR / "packs" / "incremental" / "manifest.yaml").read_text(encoding="utf-8"))
    return manifest["contributes"]["prompts"]


def pack_provider(root, user_text):
    """不起包仓库时的提示规则数据源：只按增量构建包的规则匹配（等同于项目启用了这个包）。"""
    return [rule["text"] for rule in pack_rules()
            if any(re.search(pattern, user_text) for pattern in rule["match"])]


@pytest.fixture()
def rule_on(monkeypatch):
    monkeypatch.setattr(injection, "_prompt_hint_provider", pack_provider)
    return pack_rules()[0]["text"]


# 用户原话 → 有没有提示
PROMPT_CASES = [
    ("帮我看看订单对象的属性", False),
    ("这一轮就到这，结束本轮", True),
    ("结束本轮吧", True),
    ("   ", False),
    ("", False),
    ("/ontology-research 示例领域", False),
]


@pytest.mark.parametrize("prompt,hit", PROMPT_CASES)
def test_pack_rule_fires_only_on_matching_messages(rule_on, tmp_path, prompt, hit):
    assert injection.prompt_hint(tmp_path, prompt) == (rule_on if hit else "")


def test_prompt_hint_empty_without_provider_or_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(injection, "_prompt_hint_provider", None)
    assert injection.prompt_hint(tmp_path, "其实应该是按箱") == ""

    def broken(root, text):
        raise RuntimeError("模拟包仓库读不出")

    monkeypatch.setattr(injection, "_prompt_hint_provider", broken)
    assert injection.prompt_hint(tmp_path, "其实应该是按箱") == ""
    monkeypatch.setattr(injection, "_prompt_hint_provider", lambda root, text: ["甲", "", "乙"])
    assert injection.prompt_hint(tmp_path, "随便") == "甲\n\n乙"


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


# 场景 → 退役前 session_start_hook.sh 的 additionalContext
STAGE_CASES = {
    "empty": "本体项目当前阶段：未开始。",
    "full": "本体项目当前阶段：校验待审阅。 进行中的命令：mock validate。 最近报告：校验报告-示例-2.md（2 个 [ ] 待处理）",
    "clean-report": "本体项目当前阶段：流程图就绪。 最近报告：overview.md",
    "research-pending": "本体项目当前阶段：资料分析待核验。",
}


@needs_shell
@pytest.mark.parametrize("scenario", list(STAGE_CASES))
def test_stage_hint_matches_retired_session_start_hook(tmp_path, scenario):
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
    assert injection.stage_hint(ws) == STAGE_CASES[scenario]


def test_retired_prompt_hooks_are_not_in_kernel_guards():
    """两个提示 hook 退役（ADR-0034）：不再随内核守卫发布，hooks 基线里也没有对应的事件。"""
    from app import kernel_guards

    names = {path.name for path in kernel_guards.scripts()}
    assert not names & {"session_start_hook.sh", "knowledge_signal_probe.sh"}
    hooks = kernel_guards.settings_base()["hooks"]
    assert "SessionStart" not in hooks and "UserPromptSubmit" not in hooks


# ---- 入口：去重与拼接 ----


def test_build_context_dedupes_unchanged_stage_and_probes_each_message(tmp_path, monkeypatch, rule_on):
    stage = {"text": "本体项目当前阶段：未开始。"}
    monkeypatch.setattr(injection, "stage_hint", lambda root: stage["text"])

    first = injection.build_context(tmp_path, user_text="开始建模", session_start=True, last_injected=None)
    assert first.text == "本体项目当前阶段：未开始。"
    assert first.stage_hint and not first.prompt_hint and first.digest

    # 阶段没变：不重复；提示规则按本条消息判断
    second = injection.build_context(
        tmp_path, user_text="这个对象改完了，结束本轮", session_start=False, last_injected=first.digest
    )
    assert second.stage_hint == "" and second.digest == first.digest
    assert second.text == second.prompt_hint == rule_on

    third = injection.build_context(tmp_path, user_text="好的", session_start=False, last_injected=second.digest)
    assert third.text == ""

    # 会话常驻期间阶段变了：再带上
    stage["text"] = "本体项目当前阶段：对象已提取。"
    fourth = injection.build_context(
        tmp_path, user_text="可以结束本轮了", session_start=False, last_injected=third.digest
    )
    assert fourth.digest != third.digest
    assert fourth.text == fourth.stage_hint + "\n\n" + fourth.prompt_hint

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
def test_jiuwen_first_message_carries_stage_and_later_only_on_change(tmp_path, monkeypatch, rule_on):
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
        await engine._run_turn("订单拆完了，结束本轮")
        return drain(engine)

    next_steps = injection.next_step_hint(ws)
    events = asyncio.run(scenario())
    first, second, third = queries
    # 第一条带阶段与统一下一步（与界面建议条同一份，方案 B4）
    assert next_steps.startswith("下一步建议") and "（/ontology-research" in next_steps
    assert first == injection.compose_query("开始建模", "本体项目当前阶段：未开始。\n" + next_steps)
    assert second == "好的，继续"
    assert third.startswith("<system-reminder>\n")
    assert "本体项目当前阶段：对象已提取。" in third
    assert rule_on in third
    assert third.endswith("\n\n订单拆完了，结束本轮")
    # 注入只进发给模型的 query，引擎产出的事件里没有
    assert "本体项目当前阶段" not in json.dumps(events, ensure_ascii=False)


def test_jiuwen_buffered_messages_get_injection_when_delivered(tmp_path, monkeypatch, rule_on):
    from openjiuwen.core.runner import Runner

    stage = {"text": "本体项目当前阶段：未开始。"}
    monkeypatch.setattr(injection, "stage_hint", lambda root: stage["text"])
    seen = []

    def recording(root, user_text):
        seen.append(user_text)
        return pack_provider(root, user_text)

    monkeypatch.setattr(injection, "_prompt_hint_provider", recording)
    queries = []
    gate = SimpleNamespace()

    def fake_stream(agent, inputs, session=None):
        queries.append(inputs["query"])
        if len(queries) > 1:
            return answer("收到")

        async def gen():
            gate.running.set()
            await gate.release.wait()
            yield SimpleNamespace(type="llm_output", payload={"content": "第一轮"})

        return gen()

    monkeypatch.setattr(Runner, "run_agent_streaming", staticmethod(fake_stream))
    engine = JiuwenEngine(make_ctx(tmp_path))
    engine.client = object()

    async def scenario():
        gate.running, gate.release = asyncio.Event(), asyncio.Event()
        await engine.send("第一条")
        await asyncio.wait_for(gate.running.wait(), timeout=2)
        # 轮内追加：按插话积压在引擎里，记的是原文；这一轮没有模型调用可插，结束后逐条开下一轮
        await engine.send("先结束本轮")
        await engine.send("/ontology-research 示例领域")
        assert [text for _, text in engine._steers] == ["先结束本轮", "/ontology-research 示例领域"]
        stage["text"] = "本体项目当前阶段：资料分析待核验。"
        gate.release.set()
        await asyncio.wait_for(engine._turn_task, timeout=5)

    asyncio.run(scenario())
    first, second, third = queries
    assert first == injection.compose_query("第一条", "本体项目当前阶段：未开始。")
    # 积压的消息送达时才算阶段：带上新阶段与本条命中的提示
    assert "本体项目当前阶段：资料分析待核验。" in second
    assert rule_on in second
    assert second.endswith("\n\n先结束本轮")
    # 斜杠命令照常展开；提示规则只看用户原文，不看展开后的命令文档
    assert "示例领域" in third and "state_helper.py" in third
    assert seen and not any("state_helper.py" in text for text in seen)
    assert not third.startswith("<system-reminder>")


def test_jiuwen_interrupt_reinjects_stage_next_message(tmp_path, monkeypatch):
    from openjiuwen.core.runner import Runner

    monkeypatch.setattr(injection, "stage_hint", lambda root: "本体项目当前阶段：未开始。")
    queries = []
    started = SimpleNamespace()

    def fake_stream(agent, inputs, session=None):
        queries.append(inputs["query"])
        if len(queries) > 1:
            return answer("收到")

        async def gen():
            started.event.set()
            await asyncio.Event().wait()
            yield

        return gen()

    monkeypatch.setattr(Runner, "run_agent_streaming", staticmethod(fake_stream))
    engine = JiuwenEngine(make_ctx(tmp_path))
    engine.client = object()

    async def scenario():
        started.event = asyncio.Event()
        running = asyncio.create_task(engine._run_turn("第一条"))
        await asyncio.wait_for(started.event.wait(), timeout=2)
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


# ---- 装配换了版本后续接：旧版命令说明作废的提醒 ----


def test_assembly_changed_note_is_sent_once_and_rearmed_after_interrupt(tmp_path, monkeypatch):
    monkeypatch.setattr(injection, "build_context", lambda *a, **k: injection.InjectionResult(text="阶段", digest="d"))
    fresh = injection.Injector(tmp_path, "c1")
    assert fresh.context_for("合成") == "阶段"
    changed = injection.Injector(tmp_path, "c1", assembly_changed=True)
    first = changed.context_for("再跑一下复核")
    assert first.startswith(injection.ASSEMBLY_CHANGED_NOTE) and first.endswith("阶段")
    assert injection.ASSEMBLY_CHANGED_NOTE not in changed.context_for("合成")
    # 带着提醒的那一轮被打断：模型不一定看到了，下一条重新带
    again = injection.Injector(tmp_path, "c1", assembly_changed=True)
    again.context_for("合成")
    again.forget()
    assert injection.ASSEMBLY_CHANGED_NOTE in again.context_for("合成")


def test_assembly_moved_compares_with_the_last_connect_of_the_conversation(tmp_path, monkeypatch):
    from app.services import agent_sessions

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    keys = iter(["a1:f1", "a1:f1", "a2:f1", "a2:f1"])
    monkeypatch.setattr(agent_sessions, "_assembly_key", lambda project_id: next(keys))
    assert agent_sessions.assembly_moved("p-demo", "default") is False  # 第一次连：没有旧上下文
    assert agent_sessions.assembly_moved("p-demo", "default") is False  # 没变
    assert agent_sessions.assembly_moved("p-demo", "default") is True   # 装了新版本插件后续接
    assert agent_sessions.assembly_moved("p-demo", "default") is False  # 提醒过一次，记下新版本
