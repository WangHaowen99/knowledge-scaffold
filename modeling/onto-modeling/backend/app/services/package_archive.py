"""导入压缩包的格式判别，以及已发布知识包到工作区布局的转换。

「导入已有知识包」此前只认工作区原样导出的 zip：摘要按 ontology/object 与
ontology/operation 两个**单数**目录数数，正式导入（import_zip）把成员原样铺到项目根。
而 export_package_zip 发布的 ontology-package v0.1 是 `<包根>/objects`、
`<包根>/operations` 这套**复数**布局，两者之间没有任何转换，于是预览数出 0 对象
0 操作、文件整体嵌在包根目录里、项目概览与阶段探针同样数不到，最后留下一个
"导入成功但是空"的任务（Issue #359）。

判别收敛在这一处：预览（dry_run）与正式导入拿同一份结果，两边口径不会再漂移。
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterator, Mapping
from pathlib import Path

from . import solver_contract
from .workspace import (
    IMPORTED_PACKAGE_DIR,
    META_FILE,
    WorkspaceError,
    WorkspaceManager,
    _strip_frontmatter,
)

KIND_KNOWLEDGE_PACKAGE = "knowledge_package"
KIND_WORKSPACE_ONTOLOGY = "workspace_ontology"
KIND_WORKSPACE_TRANSFER = "workspace_transfer"
KIND_UNKNOWN = "unknown"

KIND_LABELS = {
    KIND_KNOWLEDGE_PACKAGE: "已发布知识包",
    KIND_WORKSPACE_ONTOLOGY: "建模任务工作区内容",
    KIND_WORKSPACE_TRANSFER: "项目迁移包",
    KIND_UNKNOWN: "无法识别",
}

MANIFEST_NAME = "manifest.yaml"
# 各级 index.md 是 _refresh_indexes 的派生物：不从包里搬，导入后按工作区规则重建
DERIVED_ENTRY_NAME = "index.md"

# 发布包目录（复数）→ 工作区目录（单数），与 export_package_zip 的写入路径一一对应
PACKAGE_ENTITY_DIRS = (
    ("objects", "objects", "ontology/object"),
    ("operations", "operations", "ontology/operation"),
    ("rules", "rules", "ontology/rule"),
    ("metrics", "metrics", "ontology/metric"),
)
PACKAGE_SCRIPTS_DIR = "scripts"
WORKSPACE_SCRIPTS_DIR = "ontology/scripts"

# 工作区布局下的实体目录；ontology/logic 是 operation 的旧目录别名（ADR-0004）
WORKSPACE_ENTITY_DIRS = (
    ("objects", ("ontology/object",)),
    ("operations", ("ontology/operation", "ontology/logic")),
    ("rules", ("ontology/rule",)),
    ("metrics", ("ontology/metric",)),
)

ENTITY_KEYS = ("objects", "operations", "rules", "metrics", "scripts")


def _is_importable(info: zipfile.ZipInfo) -> bool:
    """可落盘的包成员：目录项、空名、路径穿越、绝对路径、包内 git 目录与符号链接一律排除。"""
    path = info.filename
    if not path or info.is_dir():
        return False
    parts = path.split("/")
    if path.startswith("/") or ".." in parts or ".git" in parts:
        return False
    return (info.external_attr >> 28) != 0xA  # 高位 0xA 表示符号链接


def _direct_child(path: str, directory: str, suffix: str) -> str | None:
    """`directory` 下的直接子文件名；不是则返回 None。派生索引不算条目。"""
    prefix = f"{directory}/"
    if not path.startswith(prefix):
        return None
    name = path[len(prefix) :]
    if "/" in name or not name.endswith(suffix):
        return None
    if name.lower() == DERIVED_ENTRY_NAME:
        return None
    return name


def _count_entries(names: set[str], dirs: tuple[str, ...], suffix: str = ".md") -> int:
    total = 0
    for name in names:
        if any(_direct_child(name, directory, suffix) for directory in dirs):
            total += 1
    return total


def _package_root(names: set[str]) -> str | None:
    """最浅 manifest.yaml 所在目录（空串或以斜杠结尾）。

    发布包可能带一层 `<包id>/` 外层目录，也可能没有，两种都要能吃。取最浅那一份，
    避免被包内嵌套目录里的同名清单带偏。
    """
    roots = [
        name[: -len(MANIFEST_NAME)]
        for name in names
        if name == MANIFEST_NAME or name.endswith(f"/{MANIFEST_NAME}")
    ]
    if not roots:
        return None
    return min(roots, key=lambda root: (root.count("/"), len(root)))


def detect_kind(names: set[str]) -> str:
    """按互不重叠的必需文件判别压缩包格式。

    顺序不能换：迁移包里同样有 ontology/ 目录，不先认它的工作区元数据，就会被当成
    工作区内容原样铺开、丢掉版本历史。
    """
    if META_FILE in names:
        return KIND_WORKSPACE_TRANSFER
    root = _package_root(names)
    if root is not None:
        entity_prefixes = tuple(
            f"{root}{package_dir}/"
            for _key, package_dir, _workspace_dir in PACKAGE_ENTITY_DIRS
        )
        if any(name.startswith(entity_prefixes) for name in names):
            return KIND_KNOWLEDGE_PACKAGE
    if any(
        name.split("/")[0] in WorkspaceManager.EXPORT_ONTOLOGY_DIRS for name in names
    ):
        return KIND_WORKSPACE_ONTOLOGY
    return KIND_UNKNOWN


def _front_field(raw: bytes, field: str) -> str:
    """读 frontmatter / manifest 里的单行标量，不为两个字段引入 YAML 依赖。"""
    lines = raw.decode("utf-8", "replace").splitlines()
    if lines and lines[0].strip() == "---":
        end = next(
            (i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"),
            len(lines),
        )
        lines = lines[1:end]
    prefix = f"{field}:"
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix) :].strip().strip("'\"")
    return ""


class _PackageFiles(Mapping[str, bytes]):
    """包根相对路径 → 成员字节的惰性视图：求解契约只读它引用到的文件，不整包读入内存。"""

    def __init__(self, zf: zipfile.ZipFile, root: str):
        self._zf = zf
        self._members = {
            info.filename[len(root) :]: info.filename
            for info in zf.infolist()
            if _is_importable(info) and info.filename.startswith(root)
        }

    def __getitem__(self, rel: str) -> bytes:
        return self._zf.read(self._members[rel])

    def __iter__(self) -> Iterator[str]:
        return iter(self._members)

    def __len__(self) -> int:
        return len(self._members)


def _package_solver(zf: zipfile.ZipFile, root: str) -> solver_contract.PackageSolver:
    return solver_contract.extract_package_solver(_PackageFiles(zf, root))


def _blockers(kind: str, root: str, counts: dict[str, int], tops: list[str]) -> list[str]:
    """阻断项：有阻断项就不建项目。

    认不出格式却照样建任务，正是"导入成功但是空"的来源；这里把判别结果直接变成
    导入前的拒绝理由，而不是留给用户在空项目里自己发现。
    """
    if kind == KIND_WORKSPACE_TRANSFER:
        return [
            "这是项目迁移包（含版本历史），请用首页「导入项目」入口导入；"
            "从这里导入会丢掉版本历史。"
        ]
    if kind == KIND_UNKNOWN:
        listed = "、".join(tops) or "（空）"
        return [
            f"无法识别的压缩包：既没有知识包清单（{MANIFEST_NAME}），也没有 "
            f"{'、'.join(WorkspaceManager.EXPORT_ONTOLOGY_DIRS)} 等工作区目录。"
            f"实际顶层内容：{listed}。"
        ]
    if kind == KIND_KNOWLEDGE_PACKAGE and counts["objects"] == 0:
        where = root.rstrip("/") or "压缩包根目录"
        return [
            f"识别为已发布知识包（包根 {where}），但 objects 目录下没有任何对象定义，"
            "导入后只会得到一个空任务。请确认导出的知识包是否完整。"
        ]
    return []


def inspect_archive(archive_path: Path) -> dict:
    """导入向导第二步的内容摘要，同时是正式导入的判别与门禁依据。"""
    skip_top = WorkspaceManager.IMPORT_SKIP_TOP
    top_dirs: dict[str, int] = {}
    skipped: list[str] = []
    names: set[str] = set()
    manifest = b""
    solver = solver_contract.PackageSolver()
    files = 0
    total_size = 0
    try:
        with zipfile.ZipFile(archive_path) as zf:
            for info in zf.infolist():
                path = info.filename
                if not path or info.is_dir():
                    continue
                files += 1
                total_size += info.file_size
                names.add(path)
                parts = path.split("/")
                top = parts[0]
                # 顶层受管条目与穿越路径不计入目录明细：它们不会落盘
                if top in skip_top or path.startswith("/") or ".." in parts:
                    if top not in skipped:
                        skipped.append(top)
                    continue
                top_dirs[top] = top_dirs.get(top, 0) + 1
            kind = detect_kind(names)
            root = _package_root(names) if kind == KIND_KNOWLEDGE_PACKAGE else None
            if root is not None:
                manifest = zf.read(f"{root}{MANIFEST_NAME}")
                solver = _package_solver(zf, root)
    except zipfile.BadZipFile as exc:
        raise WorkspaceError(f"压缩包无法解析：{exc}") from exc

    root = root or ""
    if kind == KIND_KNOWLEDGE_PACKAGE:
        counts = {
            key: _count_entries(names, (f"{root}{package_dir}",))
            for key, package_dir, _workspace_dir in PACKAGE_ENTITY_DIRS
        }
        counts["scripts"] = _count_entries(
            names, (f"{root}{PACKAGE_SCRIPTS_DIR}",), suffix=".py"
        )
    elif kind == KIND_WORKSPACE_ONTOLOGY:
        counts = {key: _count_entries(names, dirs) for key, dirs in WORKSPACE_ENTITY_DIRS}
        counts["scripts"] = _count_entries(
            names, (WORKSPACE_SCRIPTS_DIR,), suffix=".py"
        )
    else:
        counts = {key: 0 for key in ENTITY_KEYS}

    tops = sorted(top_dirs)
    blockers = _blockers(kind, root, counts, tops)
    # 求解契约导入后要能原样再发布；带病导入只会把问题推迟到发布时、且丢了定位
    blockers += [f"求解契约：{problem}（{problem.path}）" for problem in solver.problems]
    return {
        "kind": kind,
        "kind_label": KIND_LABELS[kind],
        "files": files,
        "total_size": total_size,
        "top_dirs": [{"name": name, "files": top_dirs[name]} for name in tops],
        "skipped": sorted(skipped),
        "package_root": root,
        "package_name": _front_field(manifest, "name"),
        "package_version": _front_field(manifest, "version"),
        **counts,
        "solver_operations": len(solver.operation_keys),
        "solver_problems": [problem.as_dict() for problem in solver.problems],
        "blockers": blockers,
    }


def _script_keys(zf: zipfile.ZipFile, paths: list[str], root: str) -> dict[str, str]:
    """脚本描述符 `scripts/<key>.md` 的 entrypoint → 原始脚本 key。

    打包时非 ASCII 脚本名会被哈希成 `script_<hash>.py`（见 _package_script_filename），
    按包内文件名直接落地会把脚本 key 永久变成那串哈希。描述符里记着原名，按它还原。
    """
    keys: dict[str, str] = {}
    directory = f"{root}{PACKAGE_SCRIPTS_DIR}"
    for path in paths:
        name = _direct_child(path, directory, ".md")
        if name is None:
            continue
        entrypoint = _front_field(zf.read(path), "entrypoint")
        if entrypoint and "/" not in entrypoint:
            keys[entrypoint] = name[: -len(".md")]
    return keys


def _workspace_target(rel: str, script_keys: dict[str, str]) -> tuple[str, str] | None:
    """包内相对路径 → (统计口径, 工作区路径)；没有工作区落点时返回 None。"""
    for key, package_dir, workspace_dir in PACKAGE_ENTITY_DIRS:
        name = _direct_child(rel, package_dir, ".md")
        if name is not None:
            return key, f"{workspace_dir}/{name}"
    name = _direct_child(rel, PACKAGE_SCRIPTS_DIR, ".py")
    if name is not None:
        stem = script_keys.get(name)
        filename = f"{stem}.py" if stem else name
        return "scripts", f"{WORKSPACE_SCRIPTS_DIR}/{filename}"
    return None


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def materialize(workspace_root: Path, archive_path: Path, package_root: str) -> dict[str, int]:
    """把已发布知识包落到工作区布局，并把原包整份留底。

    留底区存的是收到的原始包。对象↔数据表的绑定记在对象文档的 mapping/sources
    frontmatter 与 mappings/*.json 里，而新工作区没有数据源文件、推导不出这份绑定
    （package_mapping.resolve_object_tables 依赖已上传的表格），工作区文档剥掉
    frontmatter 后只有留底区还留着它。只搬目录就会在这里丢掉引用。

    求解 Operation 的 frontmatter 与 sidecar 由 solver_contract 展开到受管的
    ontology/solver/<key>/，发布时据此原样还原。
    """
    stats = {key: 0 for key in ENTITY_KEYS}
    stats["preserved"] = 0
    with zipfile.ZipFile(archive_path) as zf:
        solver = _package_solver(zf, package_root)
        if solver.problems:
            raise WorkspaceError("；".join(f"求解契约：{problem}" for problem in solver.problems))
        for workspace_rel, data in solver.workspace_files.items():
            _write(workspace_root / workspace_rel, data)
        stats["solver_operations"] = len(solver.operation_keys)
        paths = [info.filename for info in zf.infolist() if _is_importable(info)]
        script_keys = _script_keys(zf, paths, package_root)
        for path in paths:
            data = zf.read(path)
            inside = path.startswith(package_root)
            rel = path[len(package_root) :] if inside else path
            _write(workspace_root / IMPORTED_PACKAGE_DIR / rel, data)
            stats["preserved"] += 1
            target = _workspace_target(rel, script_keys) if inside else None
            if target is None:
                continue
            key, workspace_rel = target
            if workspace_rel.endswith(".md"):
                # 工作区文档不带包契约 frontmatter；发布时由 export_package_zip 重新注入
                try:
                    data = _strip_frontmatter(data.decode("utf-8")).encode("utf-8")
                except UnicodeDecodeError:
                    pass
            _write(workspace_root / workspace_rel, data)
            stats[key] += 1
    return stats
