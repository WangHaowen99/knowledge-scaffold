"""Shared HTTP error responses for agentic routes."""

from __future__ import annotations

from flask import jsonify

from workspace.workspaces import WorkspaceNotFoundError


def not_found_response(exc: KeyError):
    if isinstance(exc, WorkspaceNotFoundError):
        return jsonify(exc.to_dict()), 404
    return jsonify({"detail": str(exc)}), 404


def workspace_not_found_response(exc: WorkspaceNotFoundError):
    return jsonify(exc.to_dict()), 404
