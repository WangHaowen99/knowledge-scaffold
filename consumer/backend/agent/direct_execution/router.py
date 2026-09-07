"""OperationDirectRouter — entry point for the direct execution branch.

Called by the orchestrator when ``direct_execution.enabled`` is true. Returns a
RouteDecision indicating whether to execute the script directly, ask for missing
arguments, or fall back to the AgentLoopEngine.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from config import DirectExecutionConfig
from utils.logging import get_logger
from agent.direct_execution.arguments import (
    build_direct_cli_args,
    extract_business_placeholder_keys,
    extract_runtime_requirements,
    runtime_context_field,
)
from agent.direct_execution.matcher import find_best_script_operation
from agent.direct_execution.models import RouteDecision, RuntimeContext

logger = get_logger("direct_execution.router")


class OperationDirectRouter:
    """Determines whether the current user request can be handled by the
    direct script execution branch, bypassing the AgentLoopEngine."""

    def __init__(self, config: DirectExecutionConfig) -> None:
        self._config = config

    def route(
        self,
        *,
        user_request: str,
        context: Dict[str, Any],
        runtime_context: RuntimeContext,
        arguments: Optional[Dict[str, Any]] = None,
        ontology_manifest: Any = None,
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
        selected_ontology_version = (
            str(runtime_context.selected_ontology_version or "").strip()
            or str(context.get("selected_ontology_version") or context.get("selected_version") or "").strip()
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
            ontology_version=selected_ontology_version or None,
            confidence_threshold=self._config.confidence_threshold,
            workspace_id=runtime_context.workspace_id,
            ontology_manifest=ontology_manifest,
        )

        if script_entry is None or operation_key is None:
            logger.info(
                "路由决策: fallback — 未匹配到脚本操作 (ontology=%s)",
                selected_ontology_id,
            )
            return self._fallback("no_script_match", ontology_id=selected_ontology_id)

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
            ctx_field = runtime_context_field(key)
            if not ctx_field or runtime_context.get(ctx_field) is None:
                missing.append(key)
        return missing
