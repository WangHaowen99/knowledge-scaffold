# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Shared prompt construction helpers for loop backends."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.prompts.universal import build_lang_instruction, build_universal_system_prompt
from utils.lang import detect_language
from ontology.ontologies import build_ontologies_section
from utils.skills import build_skills_section
from utils.logging import get_logger
from workspace.workspaces import get_current_workspace_runtime_config, get_workspace_runtime_config
from .interaction_policy import user_interaction_supported_from_state
from .ontology_context import active_ontology_id_from_state

logger = get_logger("agent.prompting")


def build_main_system_prompt(state: Mapping[str, Any], *, include_orchestration: bool = True) -> str:
    workspace_id = str(state.get("workspace_id") or "").strip() or None
    lang_instruction = None
    try:
        runtime_config = (
            get_workspace_runtime_config(workspace_id)
            if workspace_id
            else get_current_workspace_runtime_config()
        )
        lang_config = runtime_config.get("lang", {})
        if lang_config.get("enabled"):
            if isinstance(state, dict) and "_lang_detected" not in state:
                user_query = state.get("user_request", "")
                explicit_lang = state.get("user_lang")
                state["_lang_detected"] = detect_language(
                    user_query,
                    lang=explicit_lang,
                    default_lang=lang_config.get("default"),
                )
            detected_lang = state.get("_lang_detected") if isinstance(state, Mapping) else None
            lang_instruction = build_lang_instruction(detected_lang)
    except Exception as exc:
        logger.warning("Unable to resolve runtime language configuration: %s", exc, exc_info=True)

    return build_universal_system_prompt(
        ontologies_section=build_ontologies_section(
            selected_ontology_id=active_ontology_id_from_state(state),
            workspace_id=workspace_id,
        ),
        skills_section=(
            build_skills_section(workspace_id=workspace_id)
            if workspace_id
            else build_skills_section()
        ),
        memory_context=state.get("memory_context") if isinstance(state, Mapping) else None,
        include_orchestration=include_orchestration,
        lang_instruction=lang_instruction,
        allow_user_interaction=user_interaction_supported_from_state(state),
    )
