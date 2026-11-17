"""两轮对话之间由宿主注入给模型的上下文（详设 4.2 目标 4、6.10；方案 B4；ADR-0022、ADR-0034）。

两个引擎都由宿主注入，不再靠 SessionStart、UserPromptSubmit 两个 hook（Jiuwen 只执行 PreToolUse，
原来收不到；Claude 的两个 hook 已退役）。注入三样东西：

- 阶段提示：当前阶段、进行中的命令、最近报告（原 `session_start_hook.sh` 的内容，逐字一致）；
- 统一「下一步」：与界面建议条（FL-02）同一份结果，助手回复末尾的「下一步建议」按它给。计算由
  登记的提供方给（`register_next_step_provider`：在助手工作目录里跑 `state_helper suggest`，ADR-0048），
  没登记时用 FL-02；
- 包的提示规则（详设 5.6；ADR-0040、0045）：生效中的装配里各包声明的 `prompts`，按用户原话匹配，
  每条消息单独判断，探测的是用户原文，不是斜杠命令展开后的全文；没启用声明规则的包的项目不提示。规则由登记的数据源算
  （`register_prompt_hint_provider`，启动时在 `app/wiring.py` 登记；Jiuwen worker 进程自己登记一份）。

- 自主构建（方案 B12）：运行中给这条对话注入台账摘要与用户补充，每条都带（`register_autobuild_hint_provider`）。
- 交接说明：自主构建结束后，运行开始前就有的对话带一段「运行改了主线什么、之前说的以主线为准」，和阶段提示一起
  按摘要去重（`register_handover_provider`）。
- 资料文本版：建模命令开始时仍没有文本版的资料（`material_wait.note`），点名按未读处理，每条建模命令都带。

宿主给每个引擎实例建一个 `Injector`（经 EngineContext 交给引擎）：引擎在消息真正送到模型的那一刻
调 `context_for(原文)`——Claude 在 send() 时，Jiuwen 在轮内积压的消息开始跑时——阶段与下一步按
那一刻算。实例的第一条消息必带阶段与下一步，之后内容不变不重复；这一轮被打断（模型不一定看到了）
时 `forget()`，下一条重新带上。注入内容只进发给模型的消息，不进用户可见的会话历史。
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ... import config
from ..script_env import minimal_env

_log = logging.getLogger(__name__)

# 与 Jiuwen 守卫 hook 的超时一致；超时按 hook 超时处理，本条不带阶段提示
_STAGE_TIMEOUT_SECONDS = 15


_next_step_provider: "Callable[[Path], dict | None] | None" = None


def register_next_step_provider(provider: "Callable[[Path], dict | None] | None") -> None:
    """统一下一步的数据源（详设 6.10）：项目根目录 → Suggestions（契约 7.8）。B4b 的 suggest 接这里。"""
    global _next_step_provider
    _next_step_provider = provider


def _default_next_steps(project_root: Path) -> dict | None:
    from ...kernel.changesets import worktree
    from ..flow import view

    return view.build(worktree.main_root(Path(project_root))).get("suggestions")


def next_step_hint(project_root: Path) -> str:
    """把 Suggestions 写成给模型看的一行；不是本体项目工作区、没有建议时返回空串。"""
    root = Path(project_root)
    if not (root / ".claude" / "scripts").is_dir():
        return ""
    provider = _next_step_provider or _default_next_steps
    try:
        suggestions = provider(root) or {}
    except Exception:  # noqa: BLE001 - 算不出下一步不挡消息，本条不带
        return ""
    items = [item for item in [suggestions.get("primary"), *(suggestions.get("secondary") or [])] if item]
    if not items:
        return ""
    parts = []
    for item in items[:3]:
        action = item.get("action") or {}
        command = str(action.get("text") or action.get("command") or "").strip()
        target = f"（{command}）" if command.startswith("/") else ""
        parts.append(f"{item.get('label')}{target}")
    return (
        "下一步建议（与界面上的建议按钮同一份；回复末尾给「下一步建议」时按这个顺序，最多 3 条）："
        + "；".join(parts) + "。"
    )


@dataclass(frozen=True)
class InjectionResult:
    """一条用户消息要附加给模型的上下文。"""

    text: str  # 拼好的注入文本；空串 = 本条不注入
    digest: str | None  # 模型已看到的阶段提示摘要，下一条消息作为 last_injected 传回
    stage_hint: str = ""  # 本条带上的阶段提示，没带为空
    prompt_hint: str = ""  # 本条命中的包提示规则（如领域包扩展的「结束本轮」），没命中为空


def goal_hint(project_root: Path) -> str:
    """建项目时用户写的建模目标（F4b）：蓝图「要解决的问题」以它为准。从主线工作区的项目信息读，
    助手的副本里没有这份文件，也改不了它；读不到时返回空串。"""
    import json

    from ...kernel.changesets import worktree
    from ..workspace.layout import META_FILE

    try:
        meta = json.loads((worktree.main_root(Path(project_root)) / META_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    goal = str((meta or {}).get("goal") or "").strip() if isinstance(meta, dict) else ""
    if not goal:
        return ""
    return f"建模目标（建项目时用户写的，以它为准；不要改写，也不要另拟一份）：{goal}"


def terms_hint(project_root: Path) -> str:
    """资料分析定下的统一叫法（F8：不分段以后由这里带给建模命令，原来在段说明里）。读助手所在的工作区
    （副本里刚写的蓝图也算）；没有蓝图、没有定下的叫法、项目锁定的旧 core 没有这个函数时返回空串。"""
    from ...kernel.changesets import worktree
    from ..flow import blueprint

    root = Path(project_root)
    try:
        module = blueprint.script(worktree.main_root(root))
        line = module.terms_line(module.load(root)) if hasattr(module, "terms_line") else ""
    except Exception:  # noqa: BLE001 - 蓝图读不出来不挡消息，本条不带
        return ""
    return f"{line}（资料分析定的，条目命名和正文照用）" if line else ""


def build_context(
    project_root: Path,
    *,
    user_text: str,
    session_start: bool,
    last_injected: str | None,
    conversation_id: str | None = None,
) -> InjectionResult:
    """引擎无关的注入入口：一条用户消息送达模型前调用一次（同步，会起子进程）。

    session_start 为真表示这是引擎实例（新建或续接）收到的第一条消息，阶段提示必带；
    last_injected 是上一条消息结果里的 digest。阶段读不出来（不是本体项目工作区、
    脚本超时）时本条不带阶段提示，digest 原样传回。

    插件对话（ADR-0087）只带草稿状态一段，不带阶段、下一步、目标、包提示、自主构建、资料。
    """
    plugin = _plugin_hint(project_root, conversation_id)
    if plugin is not None:
        digest = _digest(plugin)
        text = plugin if session_start or digest != last_injected else ""
        return InjectionResult(text=text, digest=digest, stage_hint=text)
    hint = stage_hint(project_root)
    goal = "\n".join(part for part in (goal_hint(project_root), terms_hint(project_root),
                                       handover_hint(project_root, conversation_id)) if part)
    digest = last_injected
    stage_part = ""
    if hint is not None or goal:
        parts = [hint] if hint is not None else []
        if hint is not None:
            next_steps = next_step_hint(project_root)
            if next_steps:
                parts.append(next_steps)
        if goal:
            parts.append(goal)
        hint = "\n".join(parts)
        digest = _digest(hint)
        if session_start or digest != last_injected:
            stage_part = hint
    rules = prompt_hint(project_root, user_text)
    autobuild = autobuild_hint(project_root, conversation_id)
    materials = material_hint(project_root, user_text)
    text = "\n\n".join(part for part in (stage_part, autobuild, materials, rules) if part)
    return InjectionResult(text=text, digest=digest, stage_hint=stage_part, prompt_hint=rules)


def _plugin_hint(project_root: Path, conversation_id: str | None) -> str | None:
    """插件对话的草稿状态；不是插件对话时 None。读不出时给一句兜底，不退回建模的注入。"""
    if not conversation_id or conversation_id == "default":  # 缺省对话总是建模对话
        return None
    from ...kernel.changesets import worktree
    from .. import plugin_mode

    project_id = worktree.project_id_of(Path(project_root))
    try:
        if not plugin_mode.is_plugin(project_id, conversation_id):
            return None
    except Exception:  # noqa: BLE001 - 对话索引读不出按建模对话处理
        return None
    try:
        return plugin_mode.injection_text(project_id, conversation_id)
    except Exception:  # noqa: BLE001 - 算不出草稿状态不挡消息
        _log.warning("项目 %s 插件草稿状态算不出", project_id, exc_info=True)
        return f"插件开发：只改 {plugin_mode.DRAFTS_DIR}/ 下这条对话的草稿，本体只读。"


ASSEMBLY_CHANGED_NOTE = (
    "建模能力有更新（插件或流程换了版本）：这条对话里之前读过的命令说明、子代理说明、流程说明都作废，"
    "执行命令前重新读 `.claude/commands/`、`.claude/agents/` 下现在的文件，不按记忆里的旧版本做；"
    "`plugins/` 下是还在开发的插件草稿，不是生效的版本，不要照它做。"
)


class Injector:
    """一个引擎实例的注入状态（宿主建、交给引擎）：第一条必带，之后内容不变不重复。"""

    def __init__(self, project_root: Path, conversation_id: str | None = None, *, assembly_changed: bool = False):
        self.project_root = Path(project_root)
        self.conversation_id = conversation_id
        self.started = False
        self.digest: str | None = None
        # 装配（插件、流程）换了版本后续接的实例：模型上下文里还留着旧版命令说明，第一条消息提醒它作废
        self.assembly_changed = assembly_changed
        self._noted = False

    def context_for(self, user_text: str) -> str:
        """消息送到模型前调一次（同步，会起子进程）；返回要放在消息前面的注入文本。"""
        result = build_context(self.project_root, user_text=user_text, session_start=not self.started,
                               last_injected=self.digest, conversation_id=self.conversation_id)
        self.started = True
        self.digest = result.digest
        self._noted = self.assembly_changed
        self.assembly_changed = False
        return "\n\n".join(part for part in (ASSEMBLY_CHANGED_NOTE if self._noted else "", result.text) if part)

    def forget(self) -> None:
        """这一轮被打断，模型不一定看到了这次的注入：下一条重新带上。"""
        self.digest = None
        self.assembly_changed = self.assembly_changed or self._noted


def compose_query(user_text: str, context: str) -> str:
    """把注入文本放在发给模型的消息前面；没有要注入的内容时原样返回。"""
    if not context:
        return user_text
    return (
        "<system-reminder>\n以下是平台附加的上下文，不是用户的原话：\n"
        f"{context}\n</system-reminder>\n\n{user_text}"
    )


# ---- 阶段提示（原 session_start_hook.sh）----


def stage_hint(project_root: Path) -> str | None:
    """阶段提示，与已退役的 session_start_hook.sh 的 additionalContext 逐字一致。

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


def _state_helper(project_root: Path) -> Path:
    # 跑装配目录里哈希核对过的那份（本项目锁定的 core 版本），否则跑镜像里的；不跑工作区 .claude/ 下可被改写的副本
    from ..packs.assembler import trusted_script

    return trusted_script(project_root, "state_helper.py", Path(config.ASSETS_DIR))


def detect_stage(project_root: Path) -> str | None:
    """`state_helper.py detect-stage` 的输出，去掉全部空白。

    超时或起不来返回 None；脚本出错、没有输出时返回空串，调用方与 hook 一样按「未开始」处理。
    """
    try:
        proc = subprocess.run(  # NOSONAR(S4721) 固定参数列表跑受信的工作区脚本，不经 shell
            ["python3", str(_state_helper(project_root)), "detect-stage"],
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


# ---- 包的提示规则 ----

_prompt_hint_provider: "Callable[[Path, str], list[str]] | None" = None


def register_prompt_hint_provider(provider: "Callable[[Path, str], list[str]] | None") -> None:
    """包提示规则的数据源：(项目根目录, 用户原文) → 命中的提示文本（packs/registry.prompt_hints）。"""
    global _prompt_hint_provider
    _prompt_hint_provider = provider


def prompt_hint(project_root: Path, user_text: str) -> str:
    """用户原文命中的包提示规则，多条之间空一行；没登记数据源、没命中、算不出来时返回空串（不挡消息）。"""
    provider = _prompt_hint_provider
    if provider is None or not (user_text or "").strip():
        return ""
    try:
        hints = provider(Path(project_root), user_text) or []
    except Exception:  # noqa: BLE001 - 提示规则是增强信息
        _log.warning("项目 %s 的提示规则算不出来，本条不带", Path(project_root).name, exc_info=True)
        return ""
    return "\n\n".join(hint for hint in hints if hint)


# ---- 自主构建（方案 B12）----

_autobuild_hint_provider: "Callable[[Path, str | None], str] | None" = None
_handover_provider: "Callable[[Path, str | None], str] | None" = None


def register_autobuild_hint_provider(provider: "Callable[[Path, str | None], str] | None") -> None:
    """自主构建的注入：(助手工作目录, 对话) → 段说明与台账摘要；不在运行中返回空串。"""
    global _autobuild_hint_provider
    _autobuild_hint_provider = provider


def register_handover_provider(provider: "Callable[[Path, str | None], str] | None") -> None:
    """登记切回陪伴建模后的交接说明：(助手工作目录, 对话) → 这条对话之后自主构建改了主线什么；没有时返回空串。

    和阶段提示一起按摘要去重（引擎实例第一条必带，内容不变不重复）。"""
    global _handover_provider
    _handover_provider = provider


def handover_hint(project_root: Path, conversation_id: str | None) -> str:
    provider = _handover_provider
    if provider is None:
        return ""
    try:
        return provider(Path(project_root), conversation_id) or ""
    except Exception:  # noqa: BLE001 - 注入是增强信息
        _log.warning("项目 %s 的交接说明算不出来，本条不带", Path(project_root).name, exc_info=True)
        return ""


def autobuild_hint(project_root: Path, conversation_id: str | None) -> str:
    provider = _autobuild_hint_provider
    if provider is None:
        return ""
    try:
        return provider(Path(project_root), conversation_id) or ""
    except Exception:  # noqa: BLE001 - 注入是增强信息
        _log.warning("项目 %s 的自主构建注入算不出来，本条不带", Path(project_root).name, exc_info=True)
        return ""


# ---- 资料文本版（数据 schema 自动处理）----


def material_hint(project_root: Path, user_text: str) -> str:
    from . import material_wait

    try:
        return material_wait.note(Path(project_root), user_text)
    except Exception:  # noqa: BLE001 - 注入是增强信息
        _log.warning("项目 %s 的资料文本版说明算不出来，本条不带", Path(project_root).name, exc_info=True)
        return ""
