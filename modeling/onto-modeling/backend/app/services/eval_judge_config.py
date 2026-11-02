"""Resolve the evaluation initiator's Onto model configuration in memory only."""

from __future__ import annotations

import os

from . import model_settings, personal_model_settings


class JudgeConfigError(Exception):
    pass


def resolve(username: str, engine: str) -> dict:
    """Use the same personal override policy and engine settings as Onto chat."""
    if engine not in personal_model_settings.ENGINES:
        raise JudgeConfigError("判分模型引擎无效，请检查当前项目的会话引擎")
    owner = personal_model_settings.model_owner(username, engine)
    context = model_settings.effective_context()
    if engine == "jiuwen":
        from .. import config
        from .engines.jiuwen import model_config

        settings = model_config(owner)
        api = "openai-completions"
        output_limit = config.JIUWEN_MAX_TOKENS
    else:
        env = model_settings.session_env(owner)
        settings = {
            "base_url": env.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL", ""),
            "model": env.get("ANTHROPIC_MODEL") or os.environ.get("ANTHROPIC_MODEL", ""),
            "api_key": (
                env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
                or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY", "")
            ),
        }
        api = "anthropic-messages"
        output_limit = context["max_output_tokens"]
    if not all(str(settings.get(key) or "").strip() for key in ("base_url", "model", "api_key")):
        raise JudgeConfigError(
            "判分模型配置不完整，请在 onto-modeling「配置中心 → 我的模型配置」"
            "配置当前项目引擎的模型；未设置个人覆盖时使用 onto-modeling 管理员配置"
        )
    return {
        "api": api,
        "base_url": settings["base_url"],
        "api_key": settings["api_key"],
        "model": settings["model"],
        "max_tokens": min(output_limit, context["max_context_tokens"] - 1),
        "context_window": context["max_context_tokens"],
    }
