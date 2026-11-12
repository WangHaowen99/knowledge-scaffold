"""发布前必须完成的建模阶段（与 develop 的 PUBLISH_REQUIRED_STAGES 同一规则，ADR-0098）。

四个阶段：资料分析、对象提取、操作提取（新目录或旧的 logic 目录）、质量检查。阶段目录里有一个可见文件就算完成：
index.md、隐藏文件和符号链接都不算；资料分析的验收没通过、对象也还没提取时，资料分析不算完成。
网页发布、API 与 MCP 都不能绕过；前端按同一规则即时提示（publishModel 的 stageNote）。
"""

from __future__ import annotations

import os
from pathlib import Path

from ..workspace.core import _research_review_for
from ..workspace.errors import WorkspaceError

PUBLISH_REQUIRED_STAGES = (
    ("research", "资料分析", ("research",)),
    ("object", "对象提取", ("ontology/object",)),
    ("operation", "操作提取", ("ontology/operation", "ontology/logic")),
    ("validate", "质量检查", ("reports",)),
)
CODE = "modeling_stages_incomplete"


def _dir_has_entries(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    return any(entry for entry in directory.iterdir() if not entry.name.startswith(".") and entry.name.lower() != "index.md")


def _stage_has_visible_file(root: Path, rel_paths: tuple[str, ...]) -> bool:
    """与前端文件树的阶段探针同口径：除目录索引外的可见文件，不跟符号链接。"""
    for rel_path in rel_paths:
        directory = root / rel_path
        if not directory.is_dir() or directory.is_symlink():
            continue
        for current, dirs, files in os.walk(directory, followlinks=False):
            current_path = Path(current)
            dirs[:] = [name for name in dirs if not name.startswith(".") and not (current_path / name).is_symlink()]
            for name in files:
                path = current_path / name
                if not name.startswith(".") and name.lower() != "index.md" and not path.is_symlink():
                    return True
    return False


def _research_blocking(root: Path) -> bool:
    review_module = _research_review_for(root)
    review = review_module.review_status(root)
    return (
        review["status"] in (review_module.PENDING, review_module.FAILED, review_module.UNVERIFIABLE)
        and not _dir_has_entries(root / "ontology" / "object")
    )


def readiness(manager, project_id: str) -> dict:
    """四个必经阶段各自是否完成，与缺的阶段。"""
    root = manager.project_root(project_id)
    research_blocking = _research_blocking(root)
    stages = []
    for key, title, rel_paths in PUBLISH_REQUIRED_STAGES:
        finished = _stage_has_visible_file(root, rel_paths)
        if key == "research" and research_blocking:
            finished = False
        stages.append({"key": key, "title": title, "finished": finished})
    missing = [{"key": item["key"], "title": item["title"]} for item in stages if not item["finished"]]
    return {"ready": not missing, "stages": stages, "missing_stages": missing}


def message(missing: list[dict]) -> str:
    return "建模未完成，请先完成：" + "、".join(item["title"] for item in missing)


def ensure(manager, project_id: str) -> None:
    state = readiness(manager, project_id)
    if not state["ready"]:
        raise WorkspaceError(message(state["missing_stages"]), 409, CODE, {"missing_stages": state["missing_stages"]})
