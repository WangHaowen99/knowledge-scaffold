"""Jiuwen 引擎的资产装载层（Phase 3，方案 §3.4）。

原则：**提示词资产单一事实源**——不把 `assets/workspace-toolkit` 转存成第二套格式，
而是按 Claude Code 的约定就地装载：

- 工作区规则（workspace-rules.md，即挂载给 Claude 的 CLAUDE.md）→ system prompt；
- 20 条命令（commands/*.md，`$ARGUMENTS` 占位）→ 斜杠命令展开：用户消息以
  `/名称` 开头时后端展开为完整指令再交给模型（与 Claude CLI 的行为等价）；
- 18 个子代理 prompt（agents/*.md）不需要预注册：命令的既有写法就是
  "Read .claude/agents/x.md 后把内容作为 Task 派遣的 prompt"，jiuwen 侧由
  general-purpose 子代理承接同一模式；
- settings.json 的 PreToolUse 守卫 hooks → ClaudeHooksRail 原样执行 shell
  脚本本体（同一 matcher、同一 stdin JSON 协议），deny 输出转 rail 拒绝。

工作区 `.claude/`、`CLAUDE.md` 是指向资产目录的符号链接；jiuwen 文件沙箱
（sandbox_root）须包含资产目录才能读穿链接——写保护由 toolset_write_guard
（经 ClaudeHooksRail 执行）承担。
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import Any

_HOOK_TIMEOUT_SECONDS = 15

# jiuwen 工具名 → Claude hook 协议里的工具名（matcher 与 tool_input 键两边同形）
_HOOK_TOOL_NAME = {
    "bash": "Bash",
    "powershell": "Bash",
    "write_file": "Write",
    "edit_file": "Edit",
}


class JiuwenAssetLoader:
    """按 Claude Code 目录约定装载 workspace-toolkit 资产。"""

    def __init__(self, assets_dir: Path):
        self.assets_dir = Path(assets_dir)
        self.toolkit = self.assets_dir / "workspace-toolkit"
        self.rules_file = self.assets_dir / "workspace-rules.md"
        self.commands_dir = self.toolkit / "commands"
        self.settings_file = self.toolkit / "settings.json"

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
            "- 需要专家决策时使用 ask_user 工具（对应文档中的 AskUserQuestion）。\n"
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

    协议：stdin 收 `{"tool_name", "tool_input"}` JSON；deny 时 stdout 输出
    `hookSpecificOutput.permissionDecision == "deny"`（或旧式 `decision: block`）。
    脚本静默退出（无输出）即放行；脚本异常/超时按放行处理（守卫失效不阻断建模，
    与 Claude CLI 的 hook 失败语义一致）。
    """
    mapped = _HOOK_TOOL_NAME.get(tool_name)
    if mapped is None:
        return None
    payload = json.dumps(
        {"tool_name": mapped, "tool_input": tool_input}, ensure_ascii=False
    )
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
                )
            except (subprocess.TimeoutExpired, OSError):
                continue
            out = (proc.stdout or "").strip()
            if not out:
                continue
            try:
                decision = json.loads(out.splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                continue
            specific = decision.get("hookSpecificOutput") or {}
            if specific.get("permissionDecision") == "deny":
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
