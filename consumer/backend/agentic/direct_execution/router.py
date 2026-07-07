"""OperationDirectRouter — entry point for the direct execution branch.

Called by the orchestrator when ``direct_execution.enabled`` is true. Returns a
RouteDecision indicating whether to execute the script directly, ask for missing
arguments, or fall back to the Jiuwen ReAct loop.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Set

from ..config import DirectExecutionConfig
from ..utils.ontology_catalog import get_ontology_manifest
from ..utils.logging import get_logger
from .arguments import (
    build_direct_cli_args,
    extract_business_placeholder_keys,
    extract_runtime_requirements,
)
from .matcher import find_best_script_operation
from .models import RouteDecision, RuntimeContext

logger = get_logger("direct_execution.router")


class OperationDirectRouter:
    """Determines whether the current user request can be handled by the
    direct script execution branch, bypassing the Jiuwen ReAct loop."""

    def __init__(self, config: DirectExecutionConfig) -> None:
        self._config = config

    def route(
        self,
        *,
        user_request: str,
        context: Dict[str, Any],
        runtime_context: RuntimeContext,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> RouteDecision:
        """Evaluate the user request and return a routing decision.

        ``arguments`` — optional pre-extracted business arguments. When provided,
        skips regex-based extraction from ``user_request`` text. Keys should
        match script placeholder names (e.g. ``"洞察目标"``).
        """
        # ----------------------------------------------------------------
        # Precondition: ontology selection
        # ----------------------------------------------------------------
        logger.info(
            "路由决策: 开始评估 (run_id=%s, ontology=%s, enabled=%s, threshold=%.2f)",
            runtime_context.run_id,
            runtime_context.selected_ontology_id or "未指定",
            self._config.enabled,
            self._config.confidence_threshold,
        )
        selected_ontology_id = (
            str(runtime_context.selected_ontology_id or "").strip()
            or str(context.get("selected_ontology_id") or "").strip()
        )

        if self._config.selected_ontology_only and not selected_ontology_id:
            logger.info(
                "路由决策: fallback — 没有 selected_ontology_id (run_id=%s)",
                runtime_context.run_id,
            )
            return self._fallback("no_selected_ontology")

        if not selected_ontology_id:
            logger.info(
                "路由决策: fallback — 没有选中 ontology (run_id=%s)",
                runtime_context.run_id,
            )
            return self._fallback("no_ontology")

        # ----------------------------------------------------------------
        # Step 1: Find best script operation match
        # ----------------------------------------------------------------
        script_entry, operation_key, confidence = find_best_script_operation(
            user_request=user_request,
            ontology_id=selected_ontology_id,
            confidence_threshold=self._config.confidence_threshold,
        )

        if script_entry is None or operation_key is None:
            logger.info(
                "路由决策: fallback — 未匹配到脚本操作 (ontology=%s)",
                selected_ontology_id,
            )
            return self._fallback("no_script_match", ontology_id=selected_ontology_id)

        if _operation_requires_user_interaction(selected_ontology_id, operation_key):
            logger.info(
                "路由决策: fallback — operation 含用户交互点 (ontology=%s, operation=%s)",
                selected_ontology_id,
                operation_key,
            )
            return self._fallback(
                "operation_requires_user_interaction",
                ontology_id=selected_ontology_id,
                operation_key=operation_key,
            )

        # ----------------------------------------------------------------
        # Step 2: Extract runtime requirements
        # ----------------------------------------------------------------
        script_args = list(script_entry.get("arguments") or [])
        script_parameters = list(script_entry.get("parameters") or [])
        runtime_requirements = extract_runtime_requirements(
            script_args=script_args,
            script_parameters=script_parameters,
        )

        # ----------------------------------------------------------------
        # Step 3: Check runtime context completeness
        # ----------------------------------------------------------------
        missing_runtime = self._check_runtime_requirements(runtime_requirements, runtime_context)
        if missing_runtime:
            # Runtime context incomplete → system error, fallback
            logger.warning(
                "Direct execution fallback: runtime requirements not met (ontology=%s, operation=%s, missing=%s)",
                selected_ontology_id,
                operation_key,
                missing_runtime,
            )
            return self._fallback(
                "runtime_context_incomplete",
                ontology_id=selected_ontology_id,
                operation_key=operation_key,
            )

        # ----------------------------------------------------------------
        # Step 4: Check business parameter completeness
        # ----------------------------------------------------------------
        business_arguments = dict(arguments) if arguments else {}
        # Filter out keys not declared in script_catalog (silently ignore unknown args)
        declared_keys = extract_business_placeholder_keys(
            script_args=script_args,
            script_parameters=script_parameters,
        )
        unknown = [k for k in business_arguments if k not in declared_keys]
        if unknown:
            logger.info("忽略未声明的参数: %s (operation=%s)", unknown, operation_key)
            for k in unknown:
                business_arguments.pop(k)
        business_arguments.update(
            _extract_business_arguments_from_request(
                user_request=user_request,
                script_parameters=script_parameters,
                declared_keys=declared_keys,
                existing_arguments=business_arguments,
            )
        )
        logger.info(
            "参数输入: arguments=%s (operation=%s)",
            business_arguments,
            operation_key,
        )

        cli_args, missing_business, system_error = build_direct_cli_args(
            script_args=script_args,
            script_parameters=script_parameters,
            business_arguments=business_arguments,
            runtime_context=runtime_context,
        )

        if system_error:
            logger.warning(
                "Direct execution fallback: %s (ontology=%s, operation=%s)",
                system_error,
                selected_ontology_id,
                operation_key,
            )
            return self._fallback(
                "system_error",
                ontology_id=selected_ontology_id,
                operation_key=operation_key,
            )

        # ----------------------------------------------------------------
        # Step 5: Route decision
        # ----------------------------------------------------------------
        if missing_business:
            logger.info(
                "Direct execution missing_args: operation=%s missing=%s",
                operation_key,
                missing_business,
            )
            return RouteDecision(
                route="missing_args",
                reason="missing_business_arguments",
                confidence=confidence,
                ontology_id=selected_ontology_id,
                operation_key=operation_key,
                business_arguments=business_arguments,
                missing_business_arguments=missing_business,
                runtime_requirements=runtime_requirements,
                script_entry=script_entry,
            )

        logger.info(
            "Direct execution direct_script: operation=%s confidence=%.3f",
            operation_key,
            confidence,
        )
        return RouteDecision(
            route="direct_script",
            reason="clear_script_operation",
            confidence=confidence,
            ontology_id=selected_ontology_id,
            operation_key=operation_key,
            business_arguments=business_arguments,
            missing_business_arguments=[],
            runtime_requirements=runtime_requirements,
            script_entry=script_entry,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fallback(
        self,
        reason: str,
        *,
        ontology_id: Optional[str] = None,
        operation_key: Optional[str] = None,
    ) -> RouteDecision:
        return RouteDecision(
            route="fallback_agent_loop",
            reason=reason,
            confidence=0.0,
            ontology_id=ontology_id,
            operation_key=operation_key,
        )

    def _check_runtime_requirements(
        self,
        requirements: list[str],
        runtime_context: RuntimeContext,
    ) -> list[str]:
        missing: list[str] = []
        for key in requirements:
            ctx_field = _RUNTIME_CTX_FIELDS.get(key)
            if ctx_field and runtime_context.get(ctx_field) is None:
                missing.append(key)
        return missing

# Field mapping for runtime context validation
_RUNTIME_CTX_FIELDS = {
    "session-id": "session_id",
    "run_id": "run_id",
    "turn_id": "turn_id",
    "user_id": "user_id",
    "workspace_id": "workspace_id",
    "selected_ontology_id": "selected_ontology_id",
}


_INTERACTION_MARKERS = (
    "request_user_interaction",
    "需要用户补充",
    "用户补充",
    "用户自定义输入",
    "让用户从选项中选择",
    "从选项中选择",
    "阶段性步骤",
    "阶段确认",
    "用户确认",
    "中间决策",
    "是否继续",
    "决定是否继续",
    "确认后继续",
)


def _operation_requires_user_interaction(ontology_id: str, operation_key: str) -> bool:
    ontology, error = get_ontology_manifest(ontology_id)
    if error or ontology is None:
        return False

    operation_path = Path(ontology.ontology_dir).resolve() / "operations" / "{}.md".format(operation_key)
    if not operation_path.exists():
        return False
    try:
        content = operation_path.read_text(encoding="utf-8")
    except OSError:
        logger.warning(
            "Failed to inspect operation for interaction markers: ontology=%s operation=%s",
            ontology_id,
            operation_key,
            exc_info=True,
        )
        return False

    return any(marker in content for marker in _INTERACTION_MARKERS)


_VALUE_PATTERN = r"([A-Za-z][A-Za-z0-9_.-]{1,})"


def _extract_business_arguments_from_request(
    *,
    user_request: str,
    script_parameters: Sequence[Mapping[str, Any]],
    declared_keys: Set[str],
    existing_arguments: Mapping[str, Any],
) -> Dict[str, Any]:
    if not user_request or not script_parameters:
        return {}

    extracted: Dict[str, Any] = {}
    for parameter in script_parameters:
        name = str(parameter.get("name") or "").strip()
        if not name or name not in declared_keys:
            continue
        if name in existing_arguments and existing_arguments.get(name) is not None:
            continue
        for label in _parameter_labels(parameter):
            value = _extract_value_after_label(user_request, label)
            if value:
                extracted[name] = value
                break
    return extracted


def _parameter_labels(parameter: Mapping[str, Any]) -> list[str]:
    labels: list[str] = []
    name = str(parameter.get("name") or "").strip()
    flag = str(parameter.get("flag") or "").strip().lstrip("-")
    description = str(parameter.get("description") or "").strip()

    for value in (name, flag, description):
        if value:
            labels.append(value)
            labels.append(value.replace("_", ""))
            labels.append(value.replace("_", "-"))
            labels.append(value.replace("-", ""))

    aliases = parameter.get("aliases")
    if isinstance(aliases, list):
        labels.extend(str(alias).strip() for alias in aliases if str(alias or "").strip())

    stem = name.lower().removesuffix("_id")
    if stem and stem != name.lower():
        labels.extend(
            [
                "{}编号".format(stem),
                "{}编码".format(stem),
                "{}ID".format(stem),
                "{}id".format(stem),
            ]
        )

    seen: set[str] = set()
    unique: list[str] = []
    for label in labels:
        clean = str(label or "").strip()
        if not clean:
            continue
        normalized = clean.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(clean)
    return unique


def _extract_value_after_label(text: str, label: str) -> Optional[str]:
    label_pattern = r"\s*".join(re.escape(ch) for ch in label if not ch.isspace())
    if not label_pattern:
        return None
    patterns = [
        r"{}\s*(?:是|为|=|:|：)\s*{}".format(label_pattern, _VALUE_PATTERN),
        r"{}\s+{}".format(label_pattern, _VALUE_PATTERN),
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip(" ,，。；;")
    return None
