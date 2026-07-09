"""Authentication and user management endpoints (extracted from app.py, behavior unchanged)."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..services.auth import (
    PasswordPolicyError,
    get_auth_manager,
    get_current_user,
    require_auth,
    require_role,
)

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    auth_manager = get_auth_manager()
    if not auth_manager:
        return jsonify({"error": "Auth not configured"}), 500
    user = auth_manager.authenticate(username, password)
    if not user:
        return jsonify({"error": "Invalid credentials"}), 401
    token = auth_manager.issue_token(user)
    return jsonify({"token": token, "user": user.to_public_dict()})


@auth_bp.route("/me", methods=["GET"])
@require_auth()
def me():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"user": user.to_public_dict()})


@auth_bp.route("/users", methods=["GET"])
@require_role("admin")
def list_users():
    auth_manager = get_auth_manager()
    if not auth_manager:
        return jsonify({"error": "Auth not configured"}), 500
    return jsonify({"users": auth_manager.list_users()})


@auth_bp.route("/users", methods=["POST"])
@require_role("admin")
def create_user():
    auth_manager = get_auth_manager()
    if not auth_manager:
        return jsonify({"error": "Auth not configured"}), 500
    data = request.get_json() or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    try:
        user = auth_manager.create_user(username, password)
        return jsonify({"user": user.to_public_dict()}), 201
    except PasswordPolicyError as exc:
        return jsonify({
            "error": str(exc),
            "code": "PASSWORD_POLICY_FAILED",
            "violations": exc.violations,
        }), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@auth_bp.route("/users/<username>", methods=["PUT"])
@require_role("admin")
def update_user(username):
    auth_manager = get_auth_manager()
    if not auth_manager:
        return jsonify({"error": "Auth not configured"}), 500
    data = request.get_json() or {}
    password = data.get("password")
    if password is not None:
        password = str(password)
    current = get_current_user()
    if password is not None and current and current.username == username:
        return jsonify({"error": "不能通过用户管理重置当前登录用户密码，请使用修改密码入口"}), 400
    try:
        user = auth_manager.update_user(username, password=password)
        return jsonify({"user": user.to_public_dict()})
    except PasswordPolicyError as exc:
        return jsonify({
            "error": str(exc),
            "code": "PASSWORD_POLICY_FAILED",
            "violations": exc.violations,
        }), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@auth_bp.route("/users/<username>", methods=["DELETE"])
@require_role("admin")
def delete_user(username):
    auth_manager = get_auth_manager()
    if not auth_manager:
        return jsonify({"error": "Auth not configured"}), 500
    current = get_current_user()
    if current and current.username == username:
        return jsonify({"error": "不能删除自己"}), 400
    if username == "admin":
        return jsonify({"error": "不能删除 admin 管理员"}), 400
    try:
        auth_manager.delete_user(username)
        return jsonify({"success": True})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@auth_bp.route("/change-password", methods=["PUT"])
@require_auth()
def change_password():
    auth_manager = get_auth_manager()
    if not auth_manager:
        return jsonify({"error": "Auth not configured"}), 500
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    old_password = str(data.get("old_password", ""))
    new_password = str(data.get("new_password", ""))
    if not old_password or not new_password:
        return jsonify({"error": "原密码和新密码不能为空"}), 400
    try:
        auth_manager.change_password(user.username, old_password, new_password)
        return jsonify({
            "success": True,
            "requires_relogin": True,
        })
    except PasswordPolicyError as exc:
        return jsonify({
            "error": str(exc),
            "code": "PASSWORD_POLICY_FAILED",
            "violations": exc.violations,
        }), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
