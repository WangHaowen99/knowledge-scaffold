#!/usr/bin/env python3
"""Read-only draft preflight for the modeling Agent; never execute project code."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> int:
    backend = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend))
    bundled_assets = Path("/opt/onto-modeling/assets")
    if bundled_assets.is_dir():
        os.environ.setdefault("ONTO_WEB_ASSETS_DIR", str(bundled_assets))

    from app.services import deterministic_contract
    from app.services.workspace.errors import WorkspaceError

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", required=True)
    args = parser.parse_args()
    try:
        result = deterministic_contract.candidate_draft(Path.cwd(), args.operation)
        if result.get("draft_origin") != "agent":
            output = {"ready": False, "findings": [{"message": "请先生成助手候选草稿"}]}
        else:
            output = {
                "ready": True, "findings": [], "sandbox_verified": False,
                "message": "平台预检查通过；仍需用户确认保存并运行真实沙箱验证",
            }
    except WorkspaceError as exc:
        output = {
            "ready": False, "code": exc.code,
            "findings": (exc.details or {}).get("findings") or [{"message": str(exc)}],
        }
    print(json.dumps(output, ensure_ascii=False))
    return 0 if output["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
