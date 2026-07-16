"""Security-oriented logging helpers for sandbox integration."""

from __future__ import annotations

from typing import Any

from utils.logging import get_logger

logger = get_logger("sandbox.alerts")

_SANDBOX_FALLBACK_ALERT = (
    "SANDBOX_SECURITY_FALLBACK: script execution fell back to unsandboxed local "
    "subprocess; host environment (including secrets) is exposed to the script. "
    "reason=%s run_id=%s script_path=%s call_context=%s error=%s "
    "remediation=fix jiuwenbox availability or set SANDBOX_FALLBACK_LOCAL=false "
    "in production."
)


def log_sandbox_security_fallback(
    *,
    reason: str,
    call_context: str,
    script_path: str | None = None,
    run_id: str | None = None,
    error: Any = None,
) -> None:
    """Emit a high-visibility alert when sandbox isolation is bypassed."""
    logger.error(
        _SANDBOX_FALLBACK_ALERT,
        reason,
        run_id or "",
        script_path or "",
        call_context,
        error,
        extra={
            "sandbox_fallback": True,
            "security_event": "sandbox_bypass",
            "fallback_reason": reason,
            "run_id": run_id or "",
            "script_path": script_path or "",
        },
    )

