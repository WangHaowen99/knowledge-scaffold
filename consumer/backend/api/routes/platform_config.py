"""onto-platform connector configuration routes."""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from integration.platform.config import (
    PlatformConfigValidationError,
    get_public_platform_config,
    save_platform_config,
    test_platform_config_connection,
)
from api.auth.auth import require_auth, require_role
from utils.logging import get_logger

logger = get_logger("routes.platform_config")

platform_config_bp = Blueprint("platform_config", __name__, url_prefix="/api")


@platform_config_bp.route("/platform-config", methods=["GET"])
@require_auth()
def get_platform_config_api():
    return jsonify(get_public_platform_config())


@platform_config_bp.route("/platform-config", methods=["PATCH"])
@require_role("admin")
def update_platform_config_api():
    payload = request.get_json(silent=True) or {}
    try:
        updated_config = save_platform_config(payload)
        refresh_platform_connector = current_app.config.get("REFRESH_PLATFORM_CONNECTOR")
        if callable(refresh_platform_connector):
            refresh_platform_connector()
    except PlatformConfigValidationError as exc:
        return jsonify({"detail": str(exc)}), 400
    except Exception:
        logger.exception("Failed to refresh onto-platform connector after config update")
        return jsonify({"detail": "配置已保存，但 Platform 接入刷新失败"}), 500
    return jsonify({"config": updated_config})


@platform_config_bp.route("/platform-config/test", methods=["POST"])
@require_role("admin")
def test_platform_config_api():
    payload = request.get_json(silent=True) or {}
    result = test_platform_config_connection(payload)
    return jsonify(
        {
            "success": bool(result.get("success")),
            "message": str(result.get("message", "")).strip(),
            "tested_at": result.get("tested_at"),
        }
    )
