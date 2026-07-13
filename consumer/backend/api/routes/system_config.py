"""System configuration routes."""

from __future__ import annotations

import json

from flask import Blueprint, Response, current_app, jsonify, request

from config.system_config import (
    SystemConfigValidationError,
    get_public_system_config,
    get_system_config_readiness,
    get_system_config_template,
    import_system_config,
    test_system_config_connection,
    update_system_config_section,
)
from api.auth.auth import require_auth, require_role
from utils.logging import get_logger

logger = get_logger("routes.system_config")

system_config_bp = Blueprint("system_config", __name__, url_prefix="/api")


@system_config_bp.route("/system-config", methods=["GET"])
@require_auth()
def get_system_config_api():
    return jsonify(get_public_system_config())


@system_config_bp.route("/system-config/readiness", methods=["GET"])
@require_auth()
def get_system_config_readiness_api():
    return jsonify(get_system_config_readiness())


@system_config_bp.route("/system-config/template", methods=["GET"])
@require_role("admin")
def get_system_config_template_api():
    content = get_system_config_template()
    return Response(
        json.dumps(content, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="system_config.template.json"',
        },
    )


@system_config_bp.route("/system-config", methods=["PATCH"])
@require_role("admin")
def update_system_config_api():
    payload = request.get_json(silent=True) or {}
    try:
        section = payload.get("section")
        data = payload.get("data", {})
        updated_section = update_system_config_section(section, data)
        refresh_runtime = current_app.config.get("REFRESH_RUNTIME_SERVICES")
        if callable(refresh_runtime):
            refresh_runtime(current_app)
    except SystemConfigValidationError as exc:
        return jsonify({"detail": str(exc)}), 400
    except Exception:
        logger.exception("Failed to refresh runtime services after config update")
        return jsonify({"detail": "配置已保存，但运行时刷新失败"}), 500

    return jsonify({"section": section, "config": updated_section})


@system_config_bp.route("/system-config/import", methods=["POST"])
@require_role("admin")
def import_system_config_api():
    payload = request.get_json(silent=True) or {}
    try:
        imported_sections, config = import_system_config(payload.get("config", {}))
        refresh_runtime = current_app.config.get("REFRESH_RUNTIME_SERVICES")
        if callable(refresh_runtime):
            refresh_runtime(current_app)
    except SystemConfigValidationError as exc:
        return jsonify({"detail": str(exc)}), 400
    except Exception:
        logger.exception("Failed to refresh runtime services after config import")
        return jsonify({"detail": "配置已导入，但运行时刷新失败"}), 500

    return jsonify(
        {
            "success": True,
            "message": "配置导入成功",
            "imported_sections": imported_sections,
            "config": config,
        }
    )


@system_config_bp.route("/system-config/test", methods=["POST"])
@require_role("admin")
def test_system_config_api():
    payload = request.get_json(silent=True) or {}
    try:
        result = test_system_config_connection(payload.get("section"), payload.get("data", {}))
    except SystemConfigValidationError as exc:
        return jsonify({"detail": str(exc)}), 400
    return jsonify(
        {
            "section": payload.get("section"),
            "success": bool(result.get("success")),
            "message": str(result.get("message", "")).strip(),
            "tested_at": result.get("tested_at"),
        }
    )
