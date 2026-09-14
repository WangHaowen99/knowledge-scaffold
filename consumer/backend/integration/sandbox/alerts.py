# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Security-oriented logging helpers for sandbox integration."""

from __future__ import annotations

from typing import Any

from utils.logging import get_logger

logger = get_logger("sandbox.alerts")

_SANDBOX_FALLBACK_ALERT = (
    "SANDBOX_SECURITY_FALLBACK: script execution fell back to unsandboxed local "
    "subprocess; host environment (including secrets) is exposed to the script. "
    "mode=%s reason=%s provider=%s workspace_id=%s ontology_id=%s run_id=%s "
    "script_path=%s call_context=%s error=%s"
)


def log_sandbox_security_fallback(
    *,
    reason: str,
    call_context: str,
    script_path: str | None = None,
    run_id: str | None = None,
    error: Any = None,
    mode: str = "prefer",
    provider: str = "",
    workspace_id: str | None = None,
    ontology_id: str | None = None,
) -> None:
    """Emit a high-visibility alert when sandbox isolation is bypassed."""
    logger.error(
        _SANDBOX_FALLBACK_ALERT,
        mode,
        reason,
        provider,
        workspace_id or "",
        ontology_id or "",
        run_id or "",
        script_path or "",
        call_context,
        error,
        extra={
            "sandbox_fallback": True,
            "security_event": "sandbox_bypass",
            "execution_mode": mode,
            "execution_decision": "local_fallback",
            "fallback_reason": reason,
            "provider": provider,
            "workspace_id": workspace_id or "",
            "ontology_id": ontology_id or "",
            "run_id": run_id or "",
            "script_path": script_path or "",
            "call_context": call_context,
            "error_type": str(error or ""),
        },
    )
