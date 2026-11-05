"""Jiuwen 引擎的资产装载层（Phase 3，方案 §3.4；P1 起读项目的装配目录，方案 D6）。

原则：**提示词资产单一事实源**——不把能力包转存成第二套格式，而是按 Claude Code 的约定
就地装载项目 `.claude` 指向的装配目录（详设 8.1）：

- 装配出来的 CLAUDE.md（core 的工作区规则 + 各包规则片段）→ system prompt；
- 命令（commands/*.md，`$ARGUMENTS` 占位）→ 斜杠命令展开：用户消息以
  `/名称` 开头时后端展开为完整指令再交给模型（与 Claude CLI 的行为等价）；
- 子代理 prompt（agents/*.md）不需要预注册：命令的既有写法就是
  "Read .claude/agents/x.md 后把内容作为 Task 派遣的 prompt"，jiuwen 侧由
  general-purpose 子代理承接同一模式；
- 装配目录 settings.json 的 PreToolUse 守卫（内核注入 + 包追加的拒绝型）→ ClaudeHooksRail
  原样执行 shell 脚本本体（同一 matcher、同一 stdin JSON 协议），deny 输出转 rail 拒绝。

守卫出错即拒（方案 D9）：超时、非零退出、输出不是 JSON 一律拒绝；会写文件的未知工具默认拒绝。
工作区 `.claude/`、`CLAUDE.md` 是指向装配目录的符号链接；jiuwen 文件沙箱
（sandbox_root）须包含装配目录才能读穿链接——写保护由 toolset_write_guard
（经 ClaudeHooksRail 执行）承担。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from ..script_env import minimal_env

_HOOK_TIMEOUT_SECONDS = 15

# jiuwen 工具名 → Claude hook 协议里的工具名（matcher 与 tool_input 键两边同形）
_HOOK_TOOL_NAME = {
    "bash": "Bash",
    "powershell": "Bash",
    "write_file": "Write",
    "edit_file": "Edit",
    # 读文件也过守卫（F4c）：openjiuwen 的 read_file 遇到 PDF 会用 pdfplumber 在后端进程里解析，资料原件要拦下、
    # 改读数据接入转出的文本版
    "read_file": "Read",
}
# 只读或不碰文件的工具：不过守卫
_READ_ONLY_TOOLS = {
    "list_files", "glob", "grep", "task_tool", "todo", "web_search", "web_fetch",
    "ask_user", "list_skill", "load_skill", "read_memory", "search_memory",
}
# 名字像写文件的未知工具：默认拒绝（方案 D9）
_WRITE_LIKE_TOOL = re.compile(r"write|edit|patch|delete|remove|move|rename|create|mkdir|save|upload|append", re.I)
_DENY_ON_ERROR = "工作区守卫执行失败，按拒绝处理。请换一种写法；持续出现请反馈给维护者。"


class JiuwenAssetLoader:
    """按 Claude Code 目录约定装载能力包资产。

    `JiuwenAssetLoader.for_workspace(cwd)` 读项目 `.claude` 指向的装配目录；直接传资产根目录
    的旧写法只读镜像里的工具箱（没有内核守卫），只给没有装配目录的旧工作区兜底。
    """

    def __init__(self, assets_dir: Path | None = None, *, dotclaude: Path | None = None):
        if dotclaude is not None:
            self.toolkit = Path(dotclaude)
            self.rules_file = self.toolkit / "CLAUDE.md"
        else:
            self.toolkit = Path(assets_dir) / "workspace-toolkit"
            self.rules_file = Path(assets_dir) / "workspace-rules.md"
        # 文件沙箱要放行的资产根（.claude 链接解析后的真实目录）
        self.assets_dir = Path(os.path.realpath(self.toolkit)) if dotclaude is not None else Path(assets_dir)
        self.commands_dir = self.toolkit / "commands"
        self.settings_file = self.toolkit / "settings.json"

    @classmethod
    def for_workspace(cls, cwd: Path, fallback_assets: Path | None = None) -> "JiuwenAssetLoader":
        dotclaude = Path(cwd) / ".claude"
        if (dotclaude / "settings.json").is_file() or (dotclaude / "commands").is_dir():
            return cls(dotclaude=Path(os.path.realpath(dotclaude)))
        from ... import config

        return cls(Path(fallback_assets or config.ASSETS_DIR))

    # ---- system prompt ----

    def command_index(self) -> list[tuple[str, str]]:
        """(命令名, 一句话说明)：说明取命令文件首个非空行（截断）。"""
        index: list[tuple[str, str]] = []
        if not self.commands_dir.is_dir():
            return index
        for path in sorted(self.commands_dir.glob("*.md")):
            desc = ""
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        desc = line.replace("$ARGUMENTS", "<参数>")[:120]
                        break
            except OSError:
                continue
            index.append((path.stem, desc))
        return index

    def system_prompt(self) -> str:
        parts: list[str] = []
        try:
            parts.append(self.rules_file.read_text(encoding="utf-8").strip())
        except OSError:
            parts.append("你是本体建模助手，在专家的项目工作区内工作（当前目录即项目根）。")
        commands = self.command_index()
        if commands:
            lines = "\n".join(f"- /{name}：{desc}" for name, desc in commands)
            parts.append("# 可用命令\n\n" + lines)
        parts.append(
            "# 运行时适配说明\n\n"
            "- 上述规则中提到的 `.claude/` 目录与 `CLAUDE.md` 在当前工作区内可用（文件与 "
            "Shell 工具均可读取），其中的确定性脚本用 bash 执行（如 "
            "`python3 .claude/scripts/state_helper.py ...`）。\n"
            "- 命令文档里的 Task 派遣：读取对应 `.claude/agents/*.md` 后，把其内容作为"
            "子代理任务的完整 prompt 派发（task_tool），等待结果后继续主流程。\n"
            "- **凡需要用户选择或确认的问题**（命令文档中的 AskUserQuestion、"
            "\"请用户选择\"、模拟分支、来源选择等）**必须调用 ask_user 工具提出**，"
            "禁止在正文里列出选项等待回复——正文提问不会生成确认卡，用户可能看不到，"
            "流程会卡住。只问业务上的问题；「接下来做什么」「先建哪一段」不要问（界面的建议条会给），"
            "命令做完就收尾停下。\n"
            "- 当前会话**不提供联网检索工具**：执行资料分析（research）等命令时"
            "一律走\"仅本地资料\"路径，不要尝试联网检索或访问外部网址。\n"
            "- **命令文档的全部步骤必须执行完毕才算完成**：内容产出后仍须按文档执行"
            "收尾步骤（运行检查脚本、`state_helper.py release` 释放锁、"
            "`update-progress`、git 提交等）。收尾未完成前不得宣布任务完成或结束回合。\n"
            "- 工具调用之间写的正文用户都看得到：只说业务进展（读了哪些资料、找到了什么），不写命令文档的步骤名与编号"
            "（「前置检查」「Step 2.5」）、锁、脚本名、文件路径和变量。命令做完先释放锁，再两三句小结；不列「下一步建议」"
            "菜单（界面的建议条会给），也不说「已保存版本 N」、不让用户回复「回退」「撤回」（每一轮结束自动存成版本，"
            "撤回在界面上点）。\n"
            "- 对话里转述资料原文（数值、条件、流程）时，在句末标 `[^1]`、`[^2]`，消息末尾每个编号一行 "
            "`[^1]: resource/<文件>#<位置>`（位置写法同 `source_extract`：`L12`、`p:3`、`sheet:表!B3`）；"
            "界面据此画角标、悬停看原文，不写就没有出处。\n"
            "- 全程使用中文。"
        )
        return "\n\n".join(parts)

    # ---- 斜杠命令展开 ----

    def expand_command(self, text: str) -> str:
        """`/name 参数` → 命令文档全文（$ARGUMENTS 代入）；非命令原样返回。"""
        stripped = (text or "").strip()
        if not stripped.startswith("/"):
            return text
        match = re.match(r"^/([a-z0-9][a-z0-9-]*)\s*(.*)$", stripped, re.S)
        if not match:
            return text
        name, args = match.group(1), match.group(2).strip()
        path = self.commands_dir / f"{name}.md"
        if not path.is_file():
            return text  # 未知命令按普通消息处理（与 CLI 行为一致）
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return text
        return content.replace("$ARGUMENTS", args)

    # ---- hooks 配置 ----

    def pre_tool_use_hooks(self) -> list[tuple[re.Pattern, list[str]]]:
        """settings.json 的 PreToolUse 表：[(matcher 正则, [hook 命令串])]。"""
        try:
            data = json.loads(self.settings_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        entries = []
        for item in (data.get("hooks") or {}).get("PreToolUse") or []:
            matcher = item.get("matcher") or ""
            commands = [
                h.get("command", "")
                for h in item.get("hooks") or []
                if h.get("type") == "command" and h.get("command")
            ]
            if not commands:
                continue
            try:
                pattern = re.compile(f"^(?:{matcher})$")
            except re.error:
                continue
            entries.append((pattern, commands))
        return entries


def run_hooks_sync(
    entries: list[tuple[re.Pattern, list[str]]],
    tool_name: str,
    tool_input: dict,
    cwd: Path,
) -> str | None:
    """按 Claude hook 协议执行匹配的守卫脚本；返回 deny 理由（None = 放行）。

    协议：stdin 收 `{"tool_name", "tool_input", "cwd"}` JSON；deny 时 stdout 输出
    `hookSpecificOutput.permissionDecision == "deny"`（或旧式 `decision: block`）。
    脚本静默正常退出（无输出）即放行。出错即拒（方案 D9）：超时、起不来、非零退出、
    输出不是 JSON 一律拒绝；不认识但名字像写文件的工具也拒绝。
    """
    mapped = _HOOK_TOOL_NAME.get(tool_name)
    if mapped is None:
        if tool_name in _READ_ONLY_TOOLS or not _WRITE_LIKE_TOOL.search(tool_name or ""):
            return None
        return f"未知的写文件工具 {tool_name}，工作区守卫无法检查，按拒绝处理。请改用 write_file / edit_file / bash。"
    payload = json.dumps(
        {"tool_name": mapped, "tool_input": tool_input, "cwd": str(cwd)}, ensure_ascii=False
    )
    # 守卫脚本在工作区 .claude/ 下：不带后端进程的 Key 与凭据；项目根与 Claude CLI 一样经 CLAUDE_PROJECT_DIR 给
    from ...governance import agent_identity

    # 会话身份（方案 B7）：提交检查据此认出 agent 的提交、交给 git 提交钩子去查（方案 C11）
    env = {**minimal_env(), **agent_identity.identity_for_cwd(cwd), "CLAUDE_PROJECT_DIR": str(cwd)}
    for pattern, commands in entries:
        if not pattern.match(mapped):
            continue
        for command in commands:
            try:
                proc = subprocess.run(
                    ["bash", "-c", command],
                    input=payload,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=_HOOK_TIMEOUT_SECONDS,
                    env=env,
                )
            except (subprocess.TimeoutExpired, OSError):
                return _DENY_ON_ERROR
            if proc.returncode != 0:
                return _DENY_ON_ERROR
            out = (proc.stdout or "").strip()
            if not out:
                continue
            try:
                decision = json.loads(out.splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                return _DENY_ON_ERROR
            if not isinstance(decision, dict):
                return _DENY_ON_ERROR
            specific = decision.get("hookSpecificOutput") or {}
            if specific.get("permissionDecision") == "deny":
                from ...governance import telemetry

                telemetry.count("onto_guard_denies_total", engine="jiuwen")
                return str(specific.get("permissionDecisionReason") or "操作被工作区守卫规则拒绝")
            if decision.get("decision") == "block":
                return str(decision.get("reason") or "操作被工作区守卫规则拒绝")
    return None


def build_hooks_rail(loader: JiuwenAssetLoader, cwd: Path) -> Any:
    """构建执行 Claude 守卫 hooks 的 security rail。

    延迟 import openjiuwen（模块加载不应触发重依赖）；BaseSecurityRail 子类
    必须声明 supported_events，否则钩子静默不注册（PoC 实测坑）。
    """
    from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent
    from openjiuwen.harness.rails.security.base_security_rail import (
        BaseSecurityRail,
    )

    entries = loader.pre_tool_use_hooks()

    class ClaudeHooksRail(BaseSecurityRail):
        supported_events = {AgentCallbackEvent.BEFORE_TOOL_CALL}
        priority = 80  # 先于 UI 通知类 rail

        async def run_security_check(self, security_ctx):
            inputs = security_ctx.callback_ctx.inputs
            tool_name = str(getattr(inputs, "tool_name", "") or "")
            args = getattr(inputs, "tool_args", None)
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (ValueError, TypeError):
                    args = {"command": args} if tool_name in ("bash", "powershell") else {}
            if not isinstance(args, dict):
                args = {}
            reason = await asyncio.to_thread(
                run_hooks_sync, entries, tool_name, args, cwd
            )
            if reason:
                return self.reject(message=reason)
            return self.allow()

    return ClaudeHooksRail()
