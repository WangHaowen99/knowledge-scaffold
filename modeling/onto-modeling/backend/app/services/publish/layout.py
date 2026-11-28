"""发布读取的工作区布局（叶子模块）：发布源路径、包内脚本文件名规则、操作与脚本清单。

发布适配器与求解契约都按这里的规则找操作和脚本，两边口径一致。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .. import deterministic_contract, solver_contract

# 发布读取的全部来源：渲染与数据绑定实际读的对象/操作/规则/指标/脚本/求解契约目录，
# 以及资料区的用途清单和表格文件（数据绑定只读标了 schema 的表格，PDF、图片等既不读也不进包）。
# 发布按提交从这些路径取只读树；预检与发布结果据此列出不会进包的未提交改动
PACKAGE_SOURCE_DIRS = (
    "ontology/object", "ontology/operation", "ontology/logic", "ontology/rule",
    "ontology/metric", "ontology/scripts", solver_contract.SOLVER_DIR, deterministic_contract.CONTRACT_DIR,
)
PACKAGE_SOURCE_PATHS = (*PACKAGE_SOURCE_DIRS, "resource")

_PACKAGE_SCRIPT_STEM_RE = re.compile(r"[A-Za-z0-9_.-]+")


def package_script_filename(key: str) -> str:
    """知识包内的脚本文件名，满足 modeling 的 entrypoint 契约 ^[A-Za-z0-9_.-]+\\.py$。

    ASCII 脚本名原样沿用；中文等其他脚本名按 key 的哈希取稳定文件名，
    脚本 key 与标题仍保留原名。
    """
    if _PACKAGE_SCRIPT_STEM_RE.fullmatch(key) and ".." not in key:
        return f"{key}.py"
    return f"script_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:10]}.py"


def operation_stems(root: Path) -> list[str]:
    """操作清单（文件名去扩展名）；ontology/logic 是 operation 的旧目录别名（ADR-0004）。"""
    stems: list[str] = []
    for rel_dir in ("ontology/operation", "ontology/logic"):
        directory = root / rel_dir
        if directory.is_dir():
            stems.extend(
                path.stem for path in sorted(directory.glob("*.md"))
                if path.name != "index.md" and path.stem not in stems
            )
    return stems


def package_scripts(root: Path) -> dict[str, Path]:
    """包内脚本路径 → 工作区脚本：与渲染时的脚本文件名规则一致。"""
    directory = root / "ontology" / "scripts"
    if not directory.is_dir():
        return {}
    return {
        f"scripts/{package_script_filename(path.stem)}": path
        for path in sorted(directory.glob("*.py"))
        if path.is_file() and not path.is_symlink()
    }
