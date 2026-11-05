"""工作区布局常量与纯函数（叶子模块）：元数据文件名、受管目录、文件名与内容摘要规则。

发布、导入判别（package_archive）等模块只依赖这里，不反过来依赖工作区管理器。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

META_FILE = ".onto-web.json"  # 历史文件名保留：存量项目的识别键（ADR-0006）
PURGE_MARKER = ".onto-web.purging"  # 原地清除兜底：顶替 META_FILE，重启清扫据此认领残留
SKIP_DIRS = {".git", ".ontology-state", ".obsidian"}
RESOURCE_SUBDIRS = {"diagrams", "excel", "others"}
RESOURCE_DIRECTIVES = "resource/materials.json"
# 资料的文本版（ADR-0073）：连接器把 PDF、Word、Excel 转成的 Markdown，和原文件一一对应、随版本走；
# 不是资料分组，资料列表里不出现，只由内核写
RESOURCE_TEXT_DIR = "resource/text"


def resource_text_path(rel: str) -> str:
    """resource/others/合同.pdf → resource/text/others/合同.pdf.md"""
    return f"{RESOURCE_TEXT_DIR}/{rel.removeprefix('resource/')}.md"


def is_resource_text(rel: str) -> bool:
    return (rel or "").strip("/").startswith(f"{RESOURCE_TEXT_DIR}/")

# 已发布知识包导入时的原包留底区：随 git 版本化（跨环境迁移要带走），
# 但不是用户可编辑内容（隐藏目录，不进文件树）。
IMPORTED_PACKAGE_DIR = ".imported-package"

DEFAULT_GITIGNORE = """.onto-web.json
.ontology-state/
.onto/run/
.claude
CLAUDE.md
README.md
tmp/
"""

# 项目元数据（所有者、项目名）与工具状态（锁、回执、运行期文件）不进 git：
# 进了 git，回退版本会把所有者一起回滚，回到所有者为空的版本时项目对所有人可写
UNTRACKED_STATE_PATHS = (META_FILE, ".ontology-state", ".onto/run")
UNTRACKED_GITIGNORE_ENTRIES = (META_FILE, ".ontology-state/", ".onto/run/")
UNTRACK_COMMIT_MESSAGE = "chore: 停止跟踪项目元数据与工具状态"
_GITIGNORE_STATE_HEADER = "\n# 项目元数据与工具状态：不随版本回退\n"

# 导出知识包原始产物、判别工作区压缩包时认的顶层目录
EXPORT_ONTOLOGY_DIRS = ("ontology", "research", "reports", "knowledge")
# 导入时跳过的顶层条目：资产由本服务重新挂载；.git 不合并（v1 不保留导入方历史）；
# .onto（能力包锁文件）按本部署的 stable 重新写（迁移包导入另行处理，保留锁定版本）
IMPORT_SKIP_TOP = {".claude", "CLAUDE.md", "README.md", ".git", ".onto", META_FILE}


def is_auto_detected(directive) -> bool:
    return isinstance(directive, dict) and bool(directive.get("auto_detected"))


def _is_workspace_script(rel: Path) -> bool:
    return len(rel.parts) == 3 and rel.parts[:2] == ("ontology", "scripts") and rel.suffix == ".py"


def _is_script_descriptor(rel: Path) -> bool:
    """ontology/scripts/<脚本>.md：由 .py 派生的 consumer 调用契约（script_descriptor.py）。"""
    return (
        len(rel.parts) == 3
        and rel.parts[:2] == ("ontology", "scripts")
        and rel.suffix.lower() == ".md"
        and rel.name.lower() != "index.md"
    )


def _first_heading(path: Path) -> str:
    """index 清单的标题列：frontmatter 的 title（脚本描述符），否则首个一级标题；读不到则为空。"""
    try:
        with open(path, encoding="utf-8") as fh:
            in_frontmatter = False
            for index in range(40):
                line = fh.readline()
                if not line:
                    break
                if index == 0 and line.strip() == "---":
                    in_frontmatter = True
                    continue
                if in_frontmatter:
                    if line.strip() == "---":
                        in_frontmatter = False
                    elif line.startswith("title:"):
                        title = line[len("title:"):].strip()
                        try:
                            title = json.loads(title)
                        except ValueError:
                            pass
                        return str(title)[:80]
                    continue
                if line.startswith("# "):
                    return line[2:].strip()[:80]
    except OSError:
        pass
    return ""


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:24] or "task"


def _content_revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _default_engine() -> str:
    """新建/导入项目写入 meta 的引擎：取配置中心「模型接入」的默认引擎。

    建项目时定下来而不是读取时回落，存量项目才不会被后续改设置带着走。
    """
    from .. import model_settings

    return model_settings.default_engine()


def _strip_frontmatter(text: str) -> str:
    """去掉源文件自带的 frontmatter，避免与导出注入的 frontmatter 叠加。"""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            return text[end + 4 :].lstrip("\n")
    return text


def _is_run_state(rel: Path) -> bool:
    return rel.parts[:2] == (".onto", "run")
