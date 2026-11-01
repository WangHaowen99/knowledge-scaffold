"""两轮对话之间由宿主注入给模型的上下文（后端改造方案 B4，ADR-0022）。

Claude 引擎靠工具箱的两个 hook 拿到这些内容：SessionStart 的 `session_start_hook.sh`
（阶段、进行中的命令、最近报告）和 UserPromptSubmit 的 `knowledge_signal_probe.sh`
（业务知识信号词）。Jiuwen 引擎只执行 PreToolUse hook，两者都收不到。这里把它们移植成
与引擎无关的同一个入口 `build_context`：现在由 Jiuwen 引擎在每条消息送达模型前调用，
P2 两个引擎都改走它（引擎协议 `send(text, context)`），届时两个 hook 退役。

- 阶段提示：引擎实例的第一条消息必带；之后内容不变不重复，会话常驻期间阶段变了就再带；
- 业务知识信号：每条消息单独判断，探测的是用户原文，不是斜杠命令展开后的全文。

注入内容只进发给模型的消息，不进用户可见的会话历史。
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ... import config
from ..script_env import minimal_env

# 与 Jiuwen 守卫 hook 的超时一致；超时按 hook 超时处理，本条不带阶段提示
_STAGE_TIMEOUT_SECONDS = 15

# 两类信号词与 knowledge_signal_probe.sh（即 BUSINESS_KNOWLEDGE_SNIPPET.md）保持一致
CORRECTION_SIGNALS = ("不对", "你弄错了", "应该是", "其实", "实际上")
CONVENTION_SIGNALS = ("我们公司", "我们这边", "规则是", "通常", "必须", "行业里", "标准做法")


@dataclass(frozen=True)
class InjectionResult:
    """一条用户消息要附加给模型的上下文。"""

    text: str  # 拼好的注入文本；空串 = 本条不注入
    digest: str | None  # 模型已看到的阶段提示摘要，下一条消息作为 last_injected 传回
    stage_hint: str = ""  # 本条带上的阶段提示，没带为空
    knowledge_hint: str = ""  # 本条命中的业务知识信号提示，没命中为空


def build_context(
    project_root: Path,
    *,
    user_text: str,
    session_start: bool,
    last_injected: str | None,
) -> InjectionResult:
    """引擎无关的注入入口：一条用户消息送达模型前调用一次（同步，会起子进程）。

    session_start 为真表示这是引擎实例（新建或续接）收到的第一条消息，阶段提示必带；
    last_injected 是上一条消息结果里的 digest。阶段读不出来（不是本体项目工作区、
    脚本超时）时本条不带阶段提示，digest 原样传回。
    """
    hint = stage_hint(project_root)
    digest = last_injected
    stage_part = ""
    if hint is not None:
        digest = _digest(hint)
        if session_start or digest != last_injected:
            stage_part = hint
    knowledge = knowledge_signal_hint(user_text)
    text = "\n\n".join(part for part in (stage_part, knowledge) if part)
    return InjectionResult(text=text, digest=digest, stage_hint=stage_part, knowledge_hint=knowledge)


def compose_query(user_text: str, context: str) -> str:
    """把注入文本放在发给模型的消息前面；没有要注入的内容时原样返回。"""
    if not context:
        return user_text
    return (
        "<system-reminder>\n以下是平台附加的上下文，不是用户的原话：\n"
        f"{context}\n</system-reminder>\n\n{user_text}"
    )


# ---- 阶段提示（session_start_hook.sh）----


def stage_hint(project_root: Path) -> str | None:
    """与 session_start_hook.sh 的 additionalContext 逐字一致。

    工作区没有 `.claude/scripts`（不是本体项目工作区）或阶段读不出来时返回 None。
    """
    root = Path(project_root)
    if not (root / ".claude" / "scripts").is_dir():
        return None
    stage = detect_stage(root)
    if stage is None:
        return None
    parts = [f"本体项目当前阶段：{stage or '未开始'}。"]
    commands = running_commands(root)
    if commands:
        parts.append(f"进行中的命令：{' '.join(commands)}。")
    report = latest_report(root)
    if report:
        name, pending = report
        parts.append(f"最近报告：{name}（{pending} 个 [ ] 待处理）" if pending > 0 else f"最近报告：{name}")
    return " ".join(parts)


def _state_helper() -> Path:
    # 跑资产目录里的脚本，不跑工作区 .claude/ 下可被改写的那份
    return Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts" / "state_helper.py"


def detect_stage(project_root: Path) -> str | None:
    """`state_helper.py detect-stage` 的输出，去掉全部空白。

    超时或起不来返回 None；脚本出错、没有输出时返回空串，调用方与 hook 一样按「未开始」处理。
    """
    try:
        proc = subprocess.run(
            ["python3", str(_state_helper()), "detect-stage"],
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_STAGE_TIMEOUT_SECONDS,
            env=minimal_env(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return "".join((proc.stdout or "").split())


def running_commands(project_root: Path) -> list[str]:
    """`.ontology-state/lock-<命令>.json` 里的命令名，按名字排序。"""
    state_dir = Path(project_root) / ".ontology-state"
    try:
        locks = sorted(state_dir.glob("lock-*.json"))
    except OSError:
        return []
    return [path.name[len("lock-"):-len(".json")] for path in locks if path.is_file()]


def latest_report(project_root: Path) -> tuple[str, int] | None:
    """`reports/*.md` 里修改时间最新的一份（同时间取文件名靠前的，同 `ls -t`）及其
    `- [ ]` 开头的行数。"""
    reports_dir = Path(project_root) / "reports"
    try:
        candidates = [
            (path.stat().st_mtime_ns, path)
            for path in reports_dir.glob("*.md")
            if not path.name.startswith(".") and path.is_file()
        ]
    except OSError:
        return None
    if not candidates:
        return None
    _, latest = min(candidates, key=lambda item: (-item[0], item[1].name))
    try:
        text = latest.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return latest.name, 0
    return latest.name, sum(1 for line in text.split("\n") if line.startswith("- [ ]"))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---- 业务知识信号（knowledge_signal_probe.sh）----


def knowledge_signal_hint(user_text: str) -> str:
    """用户消息命中信号词时给模型的提示，与 knowledge_signal_probe.sh 的
    additionalContext 逐字一致；没命中返回空串。"""
    prompt = (user_text or "").strip()
    if not prompt:
        return ""
    hit_correction = [word for word in CORRECTION_SIGNALS if word in prompt]
    hit_convention = [word for word in CONVENTION_SIGNALS if word in prompt]
    if not hit_correction and not hit_convention:
        return ""
    parts = []
    if hit_correction:
        parts.append(f"纠错信号：{'、'.join(hit_correction)}")
    if hit_convention:
        parts.append(f"业务常识/规范信号：{'、'.join(hit_convention)}")
    return (
        "【业务知识信号词探针】检测到 — " + "；".join(parts) + "。\n"
        "请对照 .claude/BUSINESS_KNOWLEDGE_SNIPPET.md 评估是否需要沉淀为业务知识："
        "若是，按 .claude/INTERACTION_CONVENTIONS.md 的「业务知识确认类」模板向用户确认；"
        "若用户已用「记一下：…」「记下这条规则」等明确指令，则直接调用 knowledge_helper.py add 落盘。"
    )
