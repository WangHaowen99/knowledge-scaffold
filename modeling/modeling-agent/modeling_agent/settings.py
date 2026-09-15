from __future__ import annotations

import logging
import os
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    value = int(raw) if raw else default
    clamped = min(maximum, max(minimum, value))
    if raw and clamped != value:
        # 静默夹取会让「配了没生效」看起来像「改了没用」。窗口类参数尤其危险：
        # 运维在 .env 里写了 800、实际只生效上限值，排查时无从发现。
        logger.warning(
            "%s=%s out of range [%s, %s], clamped to %s", name, value, minimum, maximum, clamped,
        )
    return clamped


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, "").strip()
    value = float(raw) if raw else default
    return min(maximum, max(minimum, value))


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    core_base_url: str
    service_token: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout_seconds: float
    llm_max_tokens: int
    max_iterations: int
    host: str
    port: int
    skills_dir: Path
    llm_thinking_mode: str = "default"
    llm_reasoning_effort: str = ""
    llm_extra_body_json: str = "{}"
    llm_first_chunk_timeout_seconds: float = 120.0
    llm_idle_timeout_seconds: float = 60.0
    leader_output_tokens: int = 8_192
    analyst_output_tokens: int = 8_192
    script_output_tokens: int = 8_192
    # 目标和流程需要生成完整的结构化候选，不能复用短摘要预算。
    goal_output_tokens: int = 8_192
    summary_output_tokens: int = 1_024
    # 思维链与正文、工具参数共用同一个 max_tokens。上面按角色/阶段设定的预算
    # 只够正文和工具参数，推理会吃掉多少事先无法预估；不额外留余量时模型会把
    # 预算全用在推理上，正文和工具调用一个字都吐不出来。该余量只在思维链开启
    # 时生效，并始终受 llm_max_tokens 约束。
    reasoning_headroom_tokens: int = 16_384
    context_max_messages: int = 64
    context_window_messages: int = 48
    context_window_rounds: int = 10
    # requiredText 语义守卫支持分阶段上线：off 仅保留兼容行为，observe
    # 记录约束但不阻断，enforce 在模型未确认约束时重新提示并生成可操作卡片。
    required_text_guard_mode: str = "enforce"
    # 是否启用框架的上下文处理器预设链（卸载+摘要、对话/轮次压缩）。
    # 关闭时只有最基础的窗口滚动截断：超出窗口的消息被直接丢弃且无法找回，
    # 读取密集的运行会把用户决策挤出上下文。开启会带来额外的模型调用开销，
    # 因此默认关闭，按部署逐步放开。
    context_processor_preset: bool = False
    # 模型上下文窗口（输入+输出）。上下文处理器按「占用达到窗口的百分之多少」
    # 决定何时压缩，不配的话框架算不出这个比例，整条预设链等于不触发。
    # 0 表示不显式声明，交给框架按模型名推断。
    context_window_tokens: int = 0
    # 免卸载的工具返回值。判据是「丢了能不能重新拿到」：专家的回答只有一份，
    # 丢了只能再问一次；而知识包文件随时能重读，不该常驻上下文。
    context_protected_tools: tuple[str, ...] = ("ask", "request_confirmation")

    def resolved_output_budget(self, output_tokens: int | None) -> int:
        """把按角色/阶段设定的输出预算换算成实际请求的 max_tokens。

        思维链模型的推理内容与正文、工具参数共用同一个 max_tokens。阶段预算是
        给正文和工具参数留的，推理会吃掉多少事先无法预估：不留余量时模型会把
        预算全部用在推理上，正文和工具调用一个 token 都吐不出来。这种截断在
        上游表现为 reasoning_tokens 等于 output_tokens 且正文为空，需要运行侧的
        截断守卫兜底，这里先把发生概率压下去。余量只在思维链开启时追加，并且
        始终受部署级上限 llm_max_tokens 约束。
        """
        budget = min(self.llm_max_tokens, output_tokens or self.llm_max_tokens)
        if self.llm_thinking_mode != "enabled":
            return budget
        return min(self.llm_max_tokens, budget + self.reasoning_headroom_tokens)

    @property
    def runtime_db(self) -> Path:
        return self.data_dir / "runtime.sqlite"

    @property
    def checkpoint_db(self) -> Path:
        # OpenJiuwen's SQLite persistence appends .db when the configured path
        # does not already have that suffix. Keep diagnostics on the same file.
        return self.data_dir / "checkpoints.sqlite.db"

    @classmethod
    def from_env(cls) -> "Settings":
        package_root = Path(__file__).resolve().parent.parent
        return cls(
            data_dir=Path(os.getenv("MODELING_AGENT_DATA_DIR", "./data/agent")).resolve(),
            core_base_url=os.getenv("MODELING_CORE_BASE_URL", "http://127.0.0.1:5100").rstrip("/"),
            service_token=os.getenv("MODELING_SERVICE_TOKEN", ""),
            llm_base_url=os.getenv("MODELING_LLM_BASE_URL", "").rstrip("/"),
            llm_api_key=os.getenv("MODELING_LLM_API_KEY", ""),
            llm_model=os.getenv("MODELING_LLM_MODEL", ""),
            llm_timeout_seconds=_number("MODELING_LLM_TIMEOUT_SECONDS", 300.0, 10.0, 1800.0),
            llm_max_tokens=_integer("MODELING_LLM_MAX_TOKENS", 32768, 1024, 131072),
            max_iterations=_integer("MODELING_AGENT_MAX_ITERATIONS", 200, 20, 400),
            host=os.getenv("MODELING_AGENT_HOST", "0.0.0.0"),
            port=_integer("MODELING_AGENT_PORT", 5110, 1, 65535),
            skills_dir=Path(os.getenv("MODELING_AGENT_SKILLS_DIR", str(package_root / "skills"))).resolve(),
            llm_thinking_mode=os.getenv("MODELING_LLM_THINKING_MODE", "default").strip().lower() or "default",
            llm_reasoning_effort=os.getenv("MODELING_LLM_REASONING_EFFORT", "").strip().lower(),
            llm_extra_body_json=os.getenv("MODELING_LLM_EXTRA_BODY_JSON", "{}").strip() or "{}",
            llm_first_chunk_timeout_seconds=_number(
                "MODELING_LLM_FIRST_CHUNK_TIMEOUT_SECONDS", 120.0, 10.0, 600.0,
            ),
            llm_idle_timeout_seconds=_number(
                "MODELING_LLM_IDLE_TIMEOUT_SECONDS", 60.0, 10.0, 300.0,
            ),
            leader_output_tokens=_integer("MODELING_LEADER_OUTPUT_TOKENS", 8_192, 1_024, 32_768),
            analyst_output_tokens=_integer("MODELING_ANALYST_OUTPUT_TOKENS", 8_192, 2_048, 16_384),
            script_output_tokens=_integer("MODELING_SCRIPT_OUTPUT_TOKENS", 8_192, 2_048, 32_768),
            goal_output_tokens=_integer("MODELING_GOAL_OUTPUT_TOKENS", 8_192, 2_048, 16_384),
            summary_output_tokens=_integer("MODELING_SUMMARY_OUTPUT_TOKENS", 1_024, 512, 4_096),
            reasoning_headroom_tokens=_integer(
                "MODELING_REASONING_HEADROOM_TOKENS", 16_384, 0, 65_536,
            ),
            # 上限从 200/160/30 放开。故障现场专家的首次回答和第五次重复提问之间
            # 隔了约五百条消息，原来的上限即使拉满也留不住它——「放开窗口」这件事
            # 在旧上限下根本做不到。默认值不动：窗口越大单次输入越贵，该由部署按
            # 模型的上下文规模用环境变量决定，不让所有部署默认承担。
            context_max_messages=_integer("MODELING_CONTEXT_MAX_MESSAGES", 64, 16, 4000),
            context_window_messages=_integer("MODELING_CONTEXT_WINDOW_MESSAGES", 48, 8, 4000),
            context_window_rounds=_integer("MODELING_CONTEXT_WINDOW_ROUNDS", 10, 4, 400),
            context_processor_preset=(
                os.getenv("MODELING_CONTEXT_PROCESSOR_PRESET", "").strip().lower()
                in {"1", "true", "yes", "on"}
            ),
            context_window_tokens=_integer(
                "MODELING_CONTEXT_WINDOW_TOKENS", 0, 0, 2_000_000,
            ),
            context_protected_tools=tuple(
                item.strip()
                for item in os.getenv(
                    "MODELING_CONTEXT_PROTECTED_TOOLS", "ask,request_confirmation",
                ).split(",")
                if item.strip()
            ),
            required_text_guard_mode=(
                os.getenv("MODELING_REQUIRED_TEXT_GUARD_MODE", "enforce").strip().lower()
                if os.getenv("MODELING_REQUIRED_TEXT_GUARD_MODE", "enforce").strip().lower()
                in {"off", "observe", "enforce"} else "enforce"
            ),
        )

    def with_runtime_llm(self, config: dict[str, object]) -> "Settings":
        max_tokens = int(config.get("maxTokens") or self.llm_max_tokens)
        return replace(
            self,
            llm_base_url=str(config.get("baseUrl") or "").rstrip("/"),
            llm_api_key=str(config.get("apiKey") or ""),
            llm_model=str(config.get("model") or ""),
            llm_max_tokens=min(self.llm_max_tokens, max_tokens),
            llm_thinking_mode=str(config.get("thinkingMode") or "default").strip().lower() or "default",
            llm_reasoning_effort=str(config.get("reasoningEffort") or "").strip().lower(),
            llm_extra_body_json=str(config.get("extraBodyJson") or "{}").strip() or "{}",
        )

    def llm_request_extra_body(self) -> dict[str, Any]:
        try:
            extra = json.loads(self.llm_extra_body_json or "{}")
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Invalid LLM extra body JSON: {error}") from error
        if not isinstance(extra, dict):
            raise RuntimeError("Invalid LLM extra body JSON: expected object")
        mode = self.llm_thinking_mode.strip().lower()
        if mode in {"enabled", "disabled"}:
            extra = {**extra, "thinking": {"type": mode}}
        return extra

    def public_config(self) -> dict[str, object]:
        return {
            "coreBaseUrl": self.core_base_url,
            "modelConfigured": bool(self.llm_base_url and self.llm_model),
            "dynamicModelConfig": len(self.service_token) >= 32,
            "maxIterations": self.max_iterations,
            "contextPolicy": {
                "maxMessages": self.context_max_messages,
                "windowMessages": min(self.context_window_messages, self.context_max_messages),
                "windowRounds": self.context_window_rounds,
                "reloadArchivedHistory": False,
            },
            "requiredTextGuardMode": self.required_text_guard_mode,
        }
