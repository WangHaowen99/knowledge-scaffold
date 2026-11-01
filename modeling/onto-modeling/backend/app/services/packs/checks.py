"""包仓库入库前的静态检查（详设 6.9 第 1 步、8.1 第 1 步、5.9.6；契约 PL-15）。

每项给 pass / warn / fail；有 fail 不能发布。检查只读包内容、清单与仓库，不执行包里的任何代码
（脚本只做语法编译检查，不运行）。
"""

from __future__ import annotations

import fnmatch
import py_compile
import re
import subprocess
import tempfile
from pathlib import Path

from . import manifest as mf
from .manifest import Manifest, ManifestError
from .repository import PackRepository, pack_files, symlinks, verify_checksums

# 前端代码（详设 5.9.1：包不能带前端代码，页面由前端内置）
FRONTEND_SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".wasm", ".css", ".scss", ".less")
FRONTEND_NAMES = ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "vite.config.js")
FRONTEND_DIRS = ("node_modules", "frontend", "web", "static", "dist", "public")
# 凭据特征（只判有无，不在报告里回显命中的原文）
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{24,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*['\"][^'\"\s$<{]{8,}['\"]"),
    re.compile(r"(?i)\b(?:mysql|postgres(?:ql)?|mongodb|redis)://[^\s:/]+:[^\s@/]+@"),
)
_TEXT_SUFFIXES = (".md", ".py", ".sh", ".yaml", ".yml", ".json", ".txt", ".html", ".toml", ".ini", ".cfg", ".env")
# 提示词体积预算：命令、子代理、skill、规则片段合计
PROMPT_SOFT_BYTES = 1536 * 1024
PROMPT_HARD_BYTES = 4 * 1024 * 1024
# 内核占用的脚本名：包不能覆盖内核守卫（详设 4.8：包删不掉内核守卫）
_KERNEL_SCRIPT_PREFIXES = ("_guard_common",)


def item(check_id: str, label: str, result: str, detail: str = "") -> dict:
    """一项检查结果（契约 7.11 StaticCheck）。"""
    return {"id": check_id, "label": label, "result": result, "detail": detail}


_check = item


def _names(directory: Path, sub: str, suffix: str) -> set[str]:
    base = directory / sub
    if not base.is_dir():
        return set()
    return {path.stem if suffix else path.name for path in base.iterdir()
            if not path.name.startswith(".") and (path.is_dir() if not suffix else path.suffix == suffix)}


def declared_names(manifest: Manifest, directory: Path) -> dict[str, set[str]]:
    """命令、子代理、skill、脚本、模板、参考：清单声明的与目录实际的取并集（名字冲突按实际内容判）。"""
    directory = Path(directory)
    actual = {
        "commands": _names(directory, "commands", ".md"),
        "agents": _names(directory, "agents", ".md"),
        "skills": _names(directory, "skills", ""),
        "scripts": {p.name for p in (directory / "scripts").glob("*") if p.is_file() and not p.name.startswith(".")}
        if (directory / "scripts").is_dir() else set(),
    }
    return {key: actual[key] | set(manifest.items(key)) for key in actual}


def _frontend_files(directory: Path, files: list[str], tenant: bool) -> tuple[list[str], list[str]]:
    hard, soft = [], []
    for rel in files:
        parts = Path(rel).parts
        name = parts[-1]
        if name.lower().endswith(FRONTEND_SUFFIXES) or name in FRONTEND_NAMES or any(p in FRONTEND_DIRS for p in parts[:-1]):
            hard.append(rel)
        elif name.lower().endswith((".html", ".htm")):
            try:
                text = (directory / rel).read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                continue
            if "<script" in text or "javascript:" in text:
                (hard if tenant else soft).append(rel)
    return hard, soft


def _secrets(directory: Path, files: list[str]) -> list[str]:
    hits = []
    for rel in files:
        if not rel.lower().endswith(_TEXT_SUFFIXES):
            continue
        try:
            text = (directory / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
            hits.append(rel)
    return hits


def _syntax_errors(directory: Path, files: list[str]) -> list[str]:
    errors = []
    with tempfile.TemporaryDirectory(prefix="onto-pack-check-") as scratch:
        for rel in files:
            path = directory / rel
            if rel.endswith(".py"):
                try:
                    py_compile.compile(str(path), cfile=str(Path(scratch) / "x.pyc"), doraise=True)
                except py_compile.PyCompileError as exc:
                    errors.append(f"{rel}: {str(exc).splitlines()[-1][:120]}")
            elif rel.endswith(".sh"):
                try:
                    proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    continue
                if proc.returncode != 0:
                    errors.append(f"{rel}: {(proc.stderr or '').strip().splitlines()[-1:] or ['语法错误']}"[:160])
    return errors


def _touches_protected(pattern: str) -> bool:
    first = Path(str(pattern).lstrip("./")).parts[:1]
    return bool(first) and any(fnmatch.fnmatch(first[0], protected) or protected == first[0]
                               for protected in mf.PROTECTED_DIRS)


def _node_problems(directory: Path, manifest: Manifest) -> tuple[list[str], list[str]]:
    """节点声明：写入范围不碰受保护目录；入口命令确实存在（详设 6.9）。"""
    import yaml

    protected, missing_commands = [], []
    commands = {f"/{name}" for name in declared_names(manifest, directory)["commands"]}
    for rel in manifest.items("nodes"):
        try:
            node = yaml.safe_load((directory / rel).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            missing_commands.append(f"{rel} 无法解析")
            continue
        for item in node.get("produces") or []:
            if isinstance(item, dict) and _touches_protected(str(item.get("path") or "")):
                protected.append(f"{rel}: {item.get('path')}")
        entry = (node.get("entry") or {}).get("command")
        if entry and entry.split()[0] not in commands:
            missing_commands.append(f"{rel}: {entry}")
    return protected, missing_commands


def run(directory: Path, repository: PackRepository | None = None, *, tenant: bool = False,
        core_names: dict[str, set[str]] | None = None) -> tuple[Manifest | None, list[dict]]:
    """对解压后的包目录做全部静态检查，返回（清单或 None, 检查项列表）。

    core_names：当前 stable core 的命令、子代理、skill、脚本名，非 core 包不能与之重名。
    """
    directory = Path(directory)
    checks: list[dict] = []
    try:
        manifest = mf.load(directory)
        checks.append(_check("manifest", "清单格式与必填字段", "pass", f"{manifest.name} {manifest.version}"))
    except ManifestError as exc:
        checks.append(_check("manifest", "清单格式与必填字段", "fail", "；".join(exc.problems)))
        return None, checks

    problems = verify_checksums(directory)
    checks.append(_check("checksums", "校验和一致", "fail" if problems else "pass", "；".join(problems[:10])))
    links = symlinks(directory)
    checks.append(_check("symlinks", "不含符号链接", "fail" if links else "pass", "、".join(links[:10])))
    files = pack_files(directory)

    # 声明的命令、子代理、skill、脚本都要存在；有文件没声明给警告
    missing, undeclared = [], []
    for key, sub, suffix in (("commands", "commands", ".md"), ("agents", "agents", ".md"),
                             ("skills", "skills", ""), ("scripts", "scripts", None)):
        actual = (_names(directory, sub, suffix) if suffix is not None else
                  {p.name for p in (directory / sub).glob("*") if p.is_file()} if (directory / sub).is_dir() else set())
        declared = set(manifest.items(key))
        missing += [f"{key}/{name}" for name in sorted(declared - actual)]
        undeclared += [f"{key}/{name}" for name in sorted(actual - declared)]
    checks.append(_check(
        "declared", "声明的命令、子代理、脚本都存在",
        "fail" if missing else ("warn" if undeclared else "pass"),
        ("缺少：" + "、".join(missing[:10])) if missing else ("未声明：" + "、".join(undeclared[:10]) if undeclared else ""),
    ))

    # 兼容范围
    compat = []
    if not mf.satisfies(mf.KERNEL_API, manifest.kernel_api):
        compat.append(f"内核接口 {mf.KERNEL_API} 不满足 kernel_api {manifest.kernel_api}")
    if manifest.page_api and not mf.satisfies(mf.PAGE_API, manifest.page_api):
        compat.append(f"平台页面接口 {mf.PAGE_API} 不满足 page_api {manifest.page_api}")
    if repository is not None:
        for dep, spec in manifest.depends.items():
            if not any(mf.satisfies(version, spec) for version in repository.versions(dep)):
                compat.append(f"依赖 {dep} {spec} 在包仓库里没有满足的版本")
    checks.append(_check("compat", "兼容范围对得上", "fail" if compat else "pass", "；".join(compat)))

    # 名字：租户包与非 core 包的命令要带包前缀，不能和 core 重名，不能占用内核脚本名
    conflicts = []
    names = declared_names(manifest, directory)
    if manifest.name != "core":
        prefix = f"{manifest.name}-"
        if tenant or not manifest.official:
            conflicts += [f"命令 {name} 没有包前缀 {prefix}" for name in sorted(names["commands"]) if not name.startswith(prefix)]
        for key, existing in (core_names or {}).items():
            conflicts += [f"{key} 与基础建模能力重名：{name}" for name in sorted(names.get(key, set()) & existing)]
    if tenant and manifest.name == "core":
        conflicts.append("租户包不能使用 core 这个名字")
    conflicts += [f"脚本 {name} 是内核保留名" for name in sorted(names["scripts"])
                  if name.startswith(_KERNEL_SCRIPT_PREFIXES) or name.endswith("_guard.sh")]
    if repository is not None and repository.has(manifest.name, manifest.version):
        conflicts.append(f"{manifest.name} {manifest.version} 已在包仓库里")
    checks.append(_check("names", "名字不冲突", "fail" if conflicts else "pass", "；".join(conflicts[:10])))

    # 没有新增实体类型（插件产物只能带包名前缀，不能和五元组重名）
    entity = []
    for rel in manifest.items("artifacts"):
        try:
            import yaml

            data = yaml.safe_load((directory / rel).read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 - 解析失败同样算登记不合法
            entity.append(f"{rel} 无法解析")
            continue
        art_type = str(data.get("type") or "")
        if art_type in mf.CORE_ENTITY_TYPES or not art_type.startswith(f"{manifest.name}."):
            entity.append(f"{rel}: 产物类型 {art_type or '（空）'} 必须带包名前缀且不能是五元组")
        if not str(data.get("path") or "").startswith(f"ext/{manifest.name}/"):
            entity.append(f"{rel}: 产物只能放在 ext/{manifest.name}/ 下")
    checks.append(_check("entity_types", "没有新增实体类型", "fail" if entity else "pass", "；".join(entity[:10])))

    protected, missing_entry = _node_problems(directory, manifest)
    checks.append(_check("write_scope", "写入范围不碰受保护目录", "fail" if protected else "pass", "；".join(protected[:10])))
    checks.append(_check("node_entry", "节点入口命令存在", "fail" if missing_entry else "pass", "；".join(missing_entry[:10])))

    # 守卫：只能追加拒绝型（PreToolUse），不能带自己的 settings.json
    hooks = []
    if (directory / "settings.json").exists():
        hooks.append("包不能带 settings.json：权限与守卫基线由内核注入，包只能在清单 hooks.PreToolUse 里追加")
    hooks += [f"守卫命令引用的脚本不存在：{item.get('command')}" for item in manifest.hooks()
              if not any(script in str(item.get("command")) for script in names["scripts"])]
    checks.append(_check("hooks", "hooks 只追加拒绝型", "fail" if hooks else "pass", "；".join(hooks)))

    # 页面只选平台内置的，不带前端代码
    pages = [f"页面 {page.get('id')} 不是平台内置页面" for page in manifest.pages()
             if page.get("id") not in mf.BUILTIN_PAGES]
    pages += [f"子页挂在非内核页面 {sub.get('parent')}" for sub in manifest.contributes.get("subpages") or []
              if isinstance(sub, dict) and sub.get("parent") not in mf.KERNEL_PAGES]
    frontend, frontend_soft = _frontend_files(directory, files, tenant)
    if frontend:
        pages.append("包含前端代码：" + "、".join(frontend[:10]))
    checks.append(_check(
        "frontend", "页面只选用平台内置、没有前端代码",
        "fail" if pages else ("warn" if frontend_soft else "pass"),
        "；".join(pages) if pages else ("报告模板含脚本：" + "、".join(frontend_soft[:5]) if frontend_soft else ""),
    ))

    secrets = _secrets(directory, files)
    checks.append(_check("secrets", "不含凭据", "fail" if secrets else "pass",
                         ("疑似凭据：" + "、".join(secrets[:10])) if secrets else ""))

    syntax = _syntax_errors(directory, [rel for rel in files if rel.startswith("scripts/")])
    checks.append(_check("syntax", "脚本语法", "fail" if syntax else "pass", "；".join(syntax[:5])))

    prompt_bytes = sum(
        (directory / rel).stat().st_size for rel in files
        if rel.split("/")[0] in ("commands", "agents", "skills") or rel in manifest.items("rules")
    )
    budget = "fail" if prompt_bytes > PROMPT_HARD_BYTES else ("warn" if prompt_bytes > PROMPT_SOFT_BYTES else "pass")
    checks.append(_check("prompt_budget", "提示词体积预算", budget, f"{prompt_bytes // 1024} KiB"))
    return manifest, checks


def summary(checks: list[dict]) -> dict:
    return {result: sum(1 for item in checks if item["result"] == result) for result in ("pass", "warn", "fail")}


def report_markdown(title: str, checks: list[dict]) -> str:
    marks = {"pass": "通过", "warn": "警告", "fail": "失败"}
    lines = [f"# 静态检查报告：{title}", "", "| 检查项 | 结果 | 说明 |", "|---|---|---|"]
    for item in checks:
        detail = str(item.get("detail") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {item['label']} | {marks.get(item['result'], item['result'])} | {detail} |")
    return "\n".join(lines) + "\n"
