"""发布前结构自检：用 consumer 的导入规则检查建模导出的知识包 ZIP。

同目录的规则副本由 scripts/sync_consumer_package_rules.py 从 consumer 上游生成（来源见
upstream.json 与 PROVENANCE.md），本文件只按上游导入接口的顺序把它们串起来。consumer 导入或
激活时会拒绝的包、求解布局会被判为不可用的包，都在这里拦下；运行时才能发现的问题（求解服务、
数据范围等）交给发布后的评测。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from .archive import (
    ONTOLOGY_MAX_ZIP_BYTES,
    OntologyImportError,
    _find_converted_ontology_root,
    _is_valid_ontology_id,
    _safe_extract_zip,
)
from .ontologies import is_valid_ontology_version
from .package_validation import (
    reject_package_links,
    validate_imported_field_aliases,
    validate_imported_object_aliases,
    validate_ontology_revision,
    validate_ontology_tree_limits,
)
from .solver_capability_catalog import _is_enabled, declares_solver_layout
from .solver_layout import SolverPackageLayoutError, SolverPackageLayoutSecurityError, inspect_solver_package_layout

MESSAGE_LIMIT = 300


def _finding(code: str, message: object) -> dict:
    return {"code": code, "message": str(message).strip()[:MESSAGE_LIMIT]}


def check_package_zip(path: Path) -> list[dict]:
    """consumer 导入这个 ZIP 时会拒绝的原因；空列表表示结构可导入。"""
    data = Path(path).read_bytes()
    if not data:
        return [_finding("package_archive_invalid", "Uploaded file is empty")]
    if len(data) > ONTOLOGY_MAX_ZIP_BYTES:
        return [_finding("package_archive_invalid", "Zip file too large")]
    with tempfile.TemporaryDirectory(prefix="onto-package-rules-") as directory:
        extract_root = Path(directory) / "unzipped"
        extract_root.mkdir()
        error = _safe_extract_zip(data, extract_root)
        if error:
            return [_finding("package_archive_invalid", error)]
        try:
            found = _find_converted_ontology_root(extract_root)
        except OntologyImportError as exc:
            return [_finding("package_manifest_invalid", exc)]
        if found is None:
            return [_finding("package_manifest_invalid", "ZIP 中未找到可导入的 ontology-package v0.1/v0.2 修订包")]
        if not _is_valid_ontology_id(found.ontology_id):
            return [_finding("package_manifest_invalid", "ontology id不合法，仅支持字母、数字、点、下划线和短横线，且需字母或数字开头")]
        if found.version and not is_valid_ontology_version(found.version):
            return [_finding("package_manifest_invalid", "version不合法，仅支持[A-Za-z0-9][A-Za-z0-9._-]*")]
        return check_package_tree(found.path)


def check_package_tree(root: Path) -> list[dict]:
    """已解压修订包的核心校验、数量上限、别名与映射、求解布局。"""
    findings: list[dict] = []
    manifest = None
    # 上游 normalize_imported_package：不接受安装目录形态，再做链接与核心校验
    if (root / "versions").exists() or (root / "versions").is_symlink():
        findings.append(_finding("package_revision_invalid", "Import a single ontology revision, not an installation directory"))
    try:
        reject_package_links(root)
        manifest = validate_ontology_revision(root)
    except ValueError as exc:
        findings.append(_finding("package_revision_invalid", exc))
    # 上游 validate_import_rules：先查数量上限，超限时不再查别名
    tree_error = validate_ontology_tree_limits(root)
    if tree_error:
        findings.append(_finding("package_limit_exceeded", tree_error))
    else:
        findings += [_finding("object_aliases_invalid", e) for e in validate_imported_object_aliases(root).errors]
        findings += [_finding("field_aliases_invalid", e) for e in validate_imported_field_aliases(root).errors]
    if manifest is not None:
        findings += _solver_layout_findings(root, manifest)
    return findings


def _solver_layout_findings(root: Path, manifest: dict) -> list[dict]:
    """上游求解准入里的布局部分：安全问题拒绝导入，其余问题让求解操作不可用，发布前都要拦下。"""
    capabilities = manifest.get("capabilities")
    if str(manifest.get("schema_version") or "").strip() != "0.2" or not _is_enabled(manifest.get("enabled", True)):
        return []
    if isinstance(capabilities, dict) and not _is_enabled(capabilities.get("enabled", True)):
        return []
    if not declares_solver_layout(manifest):
        return []
    try:
        inspection = inspect_solver_package_layout(root, manifest)
    except SolverPackageLayoutSecurityError as exc:
        return [_finding("solver_package_unsafe", exc)]
    except SolverPackageLayoutError as exc:
        return [_finding("solver_layout_invalid", exc)]
    issues = list(inspection.package_issues)
    issues += [f"{key}: {message}" for key, message in sorted(inspection.invalid.items())]
    return [_finding("solver_layout_invalid", issue) for issue in issues]
