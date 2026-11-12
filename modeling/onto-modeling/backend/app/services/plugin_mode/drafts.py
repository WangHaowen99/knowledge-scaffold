"""插件草稿（ADR-0087，契约 F17 PD-01～PD-05）：项目工作区 `plugins/<包名>/`。

- 一律读**主线**（主线工作区 git HEAD 的树），不读助手副本：副本里还没合入的改动不算草稿内容，装的也是主线上的；
- 平台预检：主线上的草稿导出到临时目录，版本号换成这次安装会用的号、写校验和，跑上传同一份 `checks.run(tenant=True)`，
  加 `private_name`（包名没被别人占）、`dir_name`（目录名和包名一致）、`limits`（文件数、大小在私有包上限内）。
  不建上传记录；结果按（草稿树哈希、安装会用的版本、本人、本项目启用的版本、包仓库状态）缓存在进程里；
- 安装：版本号已在包仓库里就升补丁号、作为平台提交写回草稿，然后走私有上传、静态检查、安装、启用或升级（同 PL-22、
  PL-26、PL-06、PL-07）。同一项目串行。
"""

from __future__ import annotations

import collections
import difflib
import hashlib
import io
import re
import subprocess
import tarfile
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path, PurePosixPath

import yaml

from . import DRAFTS_DIR
from .. import conversation_store
from ..packs import checks, lockfile
from ..packs import manifest as mf
from ..packs.lockfile import PackLock
from ..packs.repository import pack_files, write_checksums
from ..packs.uploads import PRIVATE_MAX_EXTRACTED_BYTES, PRIVATE_MAX_FILES, UploadError, UploadStore

TEXT_MAX = 256 * 1024
DIFF_LINES = 40
LIST_FAILED_MAX = 20
_CACHE_MAX = 64
_GIT_TIMEOUT = 30


class DraftError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


_cache: collections.OrderedDict = collections.OrderedDict()
_cache_lock = threading.Lock()
_install_locks: dict[str, threading.Lock] = {}
_install_locks_guard = threading.Lock()


# ---------------------------------------------------------------------------
# git：主线 HEAD 上的草稿
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str, binary: bool = False):
    result = subprocess.run(  # NOSONAR(S4721) 固定的 git 参数列表，不经 shell
        ["git", "-c", "core.quotepath=false", *args], cwd=root, capture_output=True, timeout=_GIT_TIMEOUT)
    if result.returncode != 0:
        return None
    return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")


def _draft_rel(name: str) -> str:
    return f"{DRAFTS_DIR}/{name}"


def _valid_name(name: str) -> bool:
    return bool(name) and "/" not in name and name not in (".", "..") and not name.startswith(".")


def names(root: Path) -> list[str]:
    """主线上 plugins/ 下的目录（读不出清单的也算）。"""
    out = _git(root, "ls-tree", "-z", "HEAD", "--", f"{DRAFTS_DIR}/")
    found = []
    for item in (out or "").split("\0"):
        meta, _, path = item.partition("\t")
        if meta.split(" ")[1:2] == ["tree"] and path.startswith(f"{DRAFTS_DIR}/"):
            name = path[len(DRAFTS_DIR) + 1:]
            if _valid_name(name):
                found.append(name)
    return sorted(found)


def tree_of(root: Path, name: str) -> str | None:
    if not _valid_name(name):
        return None
    out = _git(root, "ls-tree", "-z", "HEAD", "--", _draft_rel(name)) or ""
    meta, _, path = out.split("\0")[0].partition("\t")
    parts = meta.split()
    return parts[2] if len(parts) == 3 and parts[1] == "tree" and path == _draft_rel(name) else None


def _entries(root: Path, name: str) -> list[dict]:
    """草稿里的文件：{path（相对草稿目录）, size, mode, sha}，按路径排。"""
    out = _git(root, "ls-tree", "-r", "-l", "-z", "HEAD", "--", f"{_draft_rel(name)}/")
    entries = []
    prefix = f"{_draft_rel(name)}/"
    for item in (out or "").split("\0"):
        meta, _, path = item.partition("\t")
        parts = meta.split()
        if len(parts) < 4 or not path.startswith(prefix):
            continue
        size = int(parts[3]) if parts[3].isdigit() else 0
        entries.append({"path": path[len(prefix):], "size": size, "mode": parts[0], "sha": parts[2]})
    return sorted(entries, key=lambda entry: entry["path"])


def _updated_at(root: Path, name: str) -> str | None:
    from ..session_host.views import iso

    out = _git(root, "log", "-1", "--format=%ct", "HEAD", "--", _draft_rel(name))
    return iso(float(out.strip())) if out and out.strip().isdigit() else None


def export(root: Path, name: str, dest: Path) -> tuple[int, int]:
    """主线 HEAD 上的草稿导出到 dest（符号链接照原样建，留给静态检查报）；返回（文件数, 字节数）。"""
    data = _git(root, "archive", "--format=tar", "HEAD", "--", _draft_rel(name), binary=True)
    if data is None:
        raise DraftError("not_found", "草稿不存在", 404)
    prefix = f"{_draft_rel(name)}/"
    dest.mkdir(parents=True, exist_ok=True)
    count = size = 0
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:  # NOSONAR(S5042) 逐条校验路径，文件数和解压大小有上限
        for member in archive.getmembers():
            if not member.name.startswith(prefix):
                continue
            rel = PurePosixPath(member.name[len(prefix):])
            if not rel.parts or rel.is_absolute() or ".." in rel.parts:
                continue
            target = dest.joinpath(*rel.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.issym():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)
            elif member.isfile():
                count += 1
                size += member.size
                if count > PRIVATE_MAX_FILES or size > PRIVATE_MAX_EXTRACTED_BYTES:
                    continue  # 超出上限的不再落盘，limits 一项报失败
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
    return count, size


# ---------------------------------------------------------------------------
# 清单与内容
# ---------------------------------------------------------------------------


def with_version(text: str, version: str) -> str:
    """清单里顶层 version 换成新号，别的行（含注释）原样保留。"""
    replaced, count = re.subn(r"(?m)^version:[^\n]*$", f"version: {version}", text, count=1)
    if count:
        return replaced
    data = yaml.safe_load(text) or {}
    data["version"] = version
    return mf.dump(data)


def _bump(version: str) -> str:
    parsed = mf.Version.parse(version)
    return f"{parsed.major}.{parsed.minor}.{parsed.patch + 1}"


def next_version(repository, name: str, version: str | None) -> str | None:
    """现在点安装会用的版本号：这个号已在包仓库里就升补丁号，直到没有为止。"""
    if not version or not mf.is_version(version):
        return None
    candidate = version
    for _ in range(1000):
        if not repository.has(name, candidate):
            return candidate
        candidate = _bump(candidate)
    return None


def _count(directory: Path, rel) -> int:
    if not isinstance(rel, str):
        return 0
    try:
        data = yaml.safe_load((directory / rel).read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return 0
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        return next((len(value) for value in data.values() if isinstance(value, list)), 0)
    return 0


def _command_label(directory: Path, name: str) -> str:
    try:
        text = (directory / "commands" / f"{name}.md").read_text(encoding="utf-8")
    except OSError:
        return name
    heading = re.search(r"(?m)^#\s+(.+)$", text)
    if heading:
        return heading.group(1).strip()
    described = re.search(r"(?m)^description:\s*(.+)$", text)
    return described.group(1).strip() if described else name


def _flow_titles(root: Path) -> dict[str, str]:
    try:
        from ..flow import model

        return {str(node.get("id")): str(node.get("title") or "") for node in model.load_for(root).get("nodes") or []
                if isinstance(node, dict)}
    except Exception:  # noqa: BLE001 - 本项目流程算不出时锚点只给编号
        return {}


def contents(root: Path, directory: Path, manifest) -> dict:
    declared, _broken = checks._declared_nodes(directory, manifest)
    titles = _flow_titles(root)
    titles.update({str(node.get("id")): str(node.get("title") or "") for _, node in declared if isinstance(node, dict)})
    nodes = []
    for _, node in declared:
        if not isinstance(node, dict):
            continue
        after, before = node.get("after"), node.get("before")
        nodes.append({
            "id": node.get("id"), "title": node.get("title"), "kind": node.get("kind") or "agent",
            "placement": node.get("placement"), "section": node.get("section"),
            "after": after, "after_title": titles.get(str(after)) or None if after else None,
            "before": before, "before_title": titles.get(str(before)) or None if before else None,
            "blocks_publish": "core.publish" in (node.get("blocks") or []),
        })
    knowledge = None
    if manifest.kind == "knowledge":
        contributes = manifest.contributes
        knowledge_dir = contributes.get("knowledge_dir")
        files = 0
        if isinstance(knowledge_dir, str) and (directory / knowledge_dir).is_dir():
            files = sum(1 for path in (directory / knowledge_dir).rglob("*") if path.is_file())
        knowledge = {"concepts": _count(directory, contributes.get("concepts")),
                     "checks": _count(directory, contributes.get("checks")),
                     "standards": _count(directory, contributes.get("standards")), "files": files}
    return {
        "nodes": nodes,
        "commands": [{"name": name, "label": _command_label(directory, name)} for name in manifest.items("commands")],
        "agents": manifest.items("agents"), "skills": manifest.items("skills"), "scripts": manifest.items("scripts"),
        "knowledge": knowledge,
    }


# ---------------------------------------------------------------------------
# 和启用的版本比
# ---------------------------------------------------------------------------


def _digests(directory: Path) -> dict[str, str]:
    return {rel: hashlib.sha256((directory / rel).read_bytes()).hexdigest() for rel in pack_files(directory)}


def _text(path: Path) -> list[str] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None


def _diff(draft: Path, installed: Path, change: dict) -> dict | None:
    rel = change["path"]
    new = _text(draft / rel) if change["status"] != "deleted" else []
    old = _text(installed / rel) if change["status"] != "added" else []
    if new is None or old is None:
        return None
    lines = []
    for line in difflib.unified_diff(old, new, lineterm="", n=2):
        if line.startswith(("---", "+++", "@@")):
            continue
        lines.append({"op": line[:1] if line[:1] in "+-" else " ", "text": line[1:]})
        if len(lines) >= DIFF_LINES:
            break
    return {"path": rel, "lines": lines}


def installed_twin(repository, name: str, owner: str, manifest, draft: Path) -> str | None:
    """草稿和本人包仓库里同版本号的那份一模一样（停用后再启用、撤回到装过的版本）：直接用那个版本，不再上传、不占新版本号。"""
    if manifest is None or not owner or repository.owner(name) != owner:
        return None
    if not repository.has(name, manifest.version):
        return None
    same = _digests(draft) == _digests(repository.version_dir(name, manifest.version))
    return manifest.version if same else None


def _install_state(repository, name: str, owner: str, enabled: str | None, draft: Path, next_: str | None) -> dict:
    view = {"state": "not_installed", "enabled_version": enabled,
            "installed_versions": repository.versions(name) if owner and repository.owner(name) == owner else [],
            "next_version": next_, "changes": [], "diff": None}
    if not enabled or not repository.has(name, enabled):
        return view
    installed = repository.version_dir(name, enabled)
    mine, theirs = _digests(draft), _digests(installed)
    changes = [{"path": rel, "status": "added" if rel not in theirs else "deleted" if rel not in mine else "modified"}
               for rel in sorted(set(mine) | set(theirs)) if mine.get(rel) != theirs.get(rel)]
    if not changes:
        return {**view, "state": "installed"}
    diff = next((found for found in (_diff(draft, installed, change) for change in changes) if found), None)
    return {**view, "state": "outdated", "changes": changes, "diff": diff}


# ---------------------------------------------------------------------------
# 预检与草稿视图
# ---------------------------------------------------------------------------


def _core_names(repository) -> dict | None:
    from ..packs import builtin

    stable = repository.latest(builtin.CORE, "stable")
    if not stable:
        return None
    return checks.declared_names(repository.manifest(builtin.CORE, stable), repository.version_dir(builtin.CORE, stable))


def _repository_mark(repository, name: str) -> tuple:
    try:
        channels = (Path(repository.root) / "channels.json").stat().st_mtime_ns
    except OSError:
        channels = 0
    return channels, tuple(repository.versions(name))


def _precheck(repository, directory: Path, manifest, owner: str, dir_name: str, files: tuple[int, int],
              version: str | None) -> list[dict]:
    if manifest is not None and version and version != manifest.version:
        path = directory / mf.MANIFEST_FILE
        path.write_text(with_version(path.read_text(encoding="utf-8"), version), encoding="utf-8")
    if (directory / mf.MANIFEST_FILE).is_file():
        write_checksums(directory)
    try:
        _manifest, items = checks.run(directory, repository, tenant=True, core_names=_core_names(repository))
    except Exception as exc:  # noqa: BLE001 - 检查本身出错也要落成失败，同上传
        items = [checks.item("internal", "静态检查", "fail", f"检查过程出错：{type(exc).__name__}")]
    count, size = files
    if count > PRIVATE_MAX_FILES or size > PRIVATE_MAX_EXTRACTED_BYTES:
        items.append(checks.item("limits", "文件数和大小在上限内", "fail",
                                 f"{count} 个文件、{size} 字节，私有包最多 {PRIVATE_MAX_FILES} 个文件、"
                                 f"{PRIVATE_MAX_EXTRACTED_BYTES // (1024 * 1024)} MiB"))
    if manifest is not None:
        free = repository.name_free(manifest.name, owner)
        items.append(checks.item("private_name", "包名没被占用", "pass" if free else "fail",
                                 "" if free else f"{manifest.name} 已被占用，请换一个名字"))
        same = manifest.name == dir_name
        items.append(checks.item("dir_name", "目录名和包名一致", "pass" if same else "fail",
                                 "" if same else f"目录是 plugins/{dir_name}/，清单里的包名是 {manifest.name}"))
    return items


def _analyze(manager, project_id: str, name: str, owner: str) -> dict:
    root = manager.project_root(project_id)
    tree = tree_of(root, name)
    if tree is None:
        raise DraftError("not_found", "草稿不存在", 404)
    repository = manager.packs.repository
    entry = (lockfile.read(root) or PackLock()).get(name)
    enabled = entry.version if entry is not None and entry.enabled else None
    key = (str(root), name, tree, owner, enabled, _repository_mark(repository, name))
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    with tempfile.TemporaryDirectory(prefix="onto-draft-") as tmp:
        directory = Path(tmp) / "pack"
        files = export(root, name, directory)
        try:
            manifest = mf.load(directory)
        except mf.ManifestError:
            manifest = None
        version = next_version(repository, name, manifest.version if manifest else None)
        twin = installed_twin(repository, name, owner, manifest, directory)
        result = {
            "tree": tree, "manifest": manifest,
            "contents": contents(root, directory, manifest) if manifest else
            {"nodes": [], "commands": [], "agents": [], "skills": [], "scripts": [], "knowledge": None},
            "install": {**_install_state(repository, name, owner, enabled, directory, twin or version),
                        "reuse": bool(twin)},
        }
        items = _precheck(repository, directory, manifest, owner, name, files, version)
    summary = checks.summary(items)
    result["precheck"] = {"state": "failed" if summary["fail"] else "passed", "summary": summary, "items": items}
    with _cache_lock:
        _cache[key] = result
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return result


def view(manager, project_id: str, name: str, owner: str, *, full: bool = True) -> dict:
    """DraftView（契约 F17 第 3 条）。full=False 是列表项：不带 files，预检只带失败项。"""
    root = manager.project_root(project_id)
    analysis = _analyze(manager, project_id, name, owner)
    manifest = analysis["manifest"]
    entries = _entries(root, name)
    precheck = analysis["precheck"]
    items = precheck["items"] if full else [item for item in precheck["items"] if item["result"] == "fail"][
        :LIST_FAILED_MAX]
    out = {
        "name": name,
        "label": manifest.label if manifest else None, "description": manifest.description if manifest else None,
        "kind": manifest.kind if manifest else None, "category": manifest.category if manifest else None,
        "version": manifest.version if manifest else None,
        "manifest_ok": manifest is not None,
        "file_count": len(entries),
        "updated_at": _updated_at(root, name),
        "conversation_id": conversation_store.plugin_conversation(project_id, name),
        "contents": analysis["contents"],
        "precheck": {**precheck, "items": items},
        "install": analysis["install"],
    }
    if full:
        out["files"] = [{"path": entry["path"], "size": entry["size"]} for entry in entries]
    return out


def list_views(manager, project_id: str, owner: str) -> list[dict]:
    root = manager.project_root(project_id)
    items = []
    for name in names(root):
        try:
            items.append(view(manager, project_id, name, owner, full=False))
        except DraftError:
            continue
    return sorted(items, key=lambda item: item["updated_at"] or "", reverse=True)


def failed_items(manager, project_id: str, name: str, owner: str, limit: int = 5) -> list[dict]:
    """上一次平台预检没过的项（宿主注入用，缓存里有就不重算）。"""
    analysis = _analyze(manager, project_id, name, owner)
    return [item for item in analysis["precheck"]["items"] if item["result"] == "fail"][:limit]


# ---------------------------------------------------------------------------
# 文件（PD-03、PD-04）
# ---------------------------------------------------------------------------


def _file_rel(name: str, path: str) -> str:
    """草稿里的相对路径 → 工作区相对路径；越界、改校验和回 400。"""
    raw = str(path or "").replace("\\", "/").strip()
    rel = PurePosixPath(raw)
    if not raw or rel.is_absolute() or any(part in ("..", "", ".") for part in raw.split("/")):
        raise DraftError("invalid_argument", "路径不合法", 400, {"fields": {"path": "pattern"}})
    if rel.as_posix() == mf.CHECKSUMS_FILE:
        raise DraftError("invalid_argument", "校验和由检查工具写，不能手改", 400, {"fields": {"path": "checksums"}})
    if ".git" in rel.parts:
        raise DraftError("invalid_argument", "路径不合法", 400, {"fields": {"path": "pattern"}})
    return f"{_draft_rel(name)}/{rel.as_posix()}"


def read_file(manager, project_id: str, name: str, path: str) -> dict:
    root = manager.project_root(project_id)
    if tree_of(root, name) is None:
        raise DraftError("not_found", "草稿不存在", 404)
    raw = str(path or "").replace("\\", "/").strip()
    entry = next((item for item in _entries(root, name) if item["path"] == raw), None)
    if entry is None:
        raise DraftError("not_found", "文件不存在", 404)
    if entry["mode"] == "120000":
        return {"path": raw, "content": None, "size": entry["size"], "binary": True}
    if entry["size"] > TEXT_MAX:
        return {"path": raw, "content": None, "size": entry["size"], "binary": False}
    data = _git(root, "cat-file", "blob", entry["sha"], binary=True) or b""
    try:
        text = data.decode("utf-8") if b"\0" not in data else None
    except UnicodeDecodeError:
        text = None
    return {"path": raw, "content": text, "size": entry["size"], "binary": text is None}


def write_file(manager, project_id: str, name: str, path: str, content: str, actor: str) -> dict:
    """网页改草稿里的一个文本文件：一次用户提交合入主线（同 Studio 保存，可以撤回）。"""
    from ...kernel.writepath import ChangeTx, Origin

    rel = _file_rel(name, path)
    if not isinstance(content, str):
        raise DraftError("invalid_argument", "content 要是文本", 400, {"fields": {"content": "type"}})
    if len(content.encode("utf-8")) > TEXT_MAX:
        raise DraftError("invalid_argument", "文件不超过 256 KiB", 400, {"fields": {"content": "length"}})
    root = manager.project_root(project_id)
    if tree_of(root, name) is None:
        raise DraftError("not_found", "草稿不存在", 404)
    with ChangeTx(manager, project_id, Origin("web", actor), summary=f"改插件草稿 {rel}") as tx:
        tx.write(rel, content)
        tx.commit(f"web: 改插件草稿 {rel} by {actor or '本机用户'}")
    return {"path": str(path), "size": len(content.encode("utf-8")), "draft": view(manager, project_id, name, actor)}


# ---------------------------------------------------------------------------
# 一轮结束（宿主调）：绑定、草稿卡
# ---------------------------------------------------------------------------


def _round_drafts(cwd: Path, start: str, end: str) -> dict[str, list[str]]:
    from ...kernel.changesets import worktree

    touched: dict[str, list[str]] = {}
    if not start or not end or start == end:
        return touched
    for _status, rel in worktree.diff(cwd, start, end):
        parts = rel.split("/")
        if len(parts) >= 3 and parts[0] == DRAFTS_DIR and _valid_name(parts[1]):
            touched.setdefault(parts[1], []).append("/".join(parts[2:]))
    return touched


def after_round(project_id: str, conversation_id: str, cwd: Path, start: str, end: str,
                changeset_id: str | None) -> dict | None:
    """插件对话一轮合入之后：看这一轮改到了哪份草稿，没绑定的绑上，返回草稿卡（契约 F17 第 8 条）。"""
    from ...api import deps

    touched = _round_drafts(cwd, start, end)
    if not touched:
        return None
    entry = conversation_store.entry(project_id, conversation_id) or {}
    name = entry.get("plugin")
    if not name:
        name = sorted(touched, key=lambda key: (-len(touched[key]), key))[0]
        label = ""
        try:
            data = yaml.safe_load((Path(cwd) / DRAFTS_DIR / name / mf.MANIFEST_FILE).read_text(encoding="utf-8"))
            label = str((data or {}).get("label") or "") if isinstance(data, dict) else ""
        except (OSError, ValueError, yaml.YAMLError):
            pass
        if conversation_store.bind_plugin(project_id, conversation_id, name, label) is not None:
            from .. import project_events

            project_events.conversation_changed(project_id, conversation_id)
    if name not in touched:
        return None
    version = None
    root = deps.workspaces.project_root(project_id)
    try:
        version = mf.load(root / DRAFTS_DIR / name).version
    except mf.ManifestError:
        pass
    # 卡片约定同 EC-40：card_id 每轮唯一、card_type；kind 留着兼容
    card_id = f"pd-{changeset_id}-{name}" if changeset_id else f"pd-{uuid.uuid4().hex[:10]}-{name}"
    return {"card_id": card_id, "card_type": "plugin_draft", "kind": "plugin_draft", "name": name, "version": version,
            "changed_files": sorted(touched[name]), "changeset_id": changeset_id}


# ---------------------------------------------------------------------------
# 安装并启用（PD-05）
# ---------------------------------------------------------------------------


def _failed_details(items: list[dict]) -> dict:
    """422 static_check_failed 的 details：失败项全量（F17），和 api-v1 错误表里的 failed（label 列表）。"""
    return {"items": items, "failed": [item["label"] for item in items]}


def _project_install_lock(project_id: str) -> threading.Lock:
    with _install_locks_guard:
        return _install_locks.setdefault(project_id, threading.Lock())


def _write_version(manager, project_id: str, name: str, version: str, actor: str) -> None:
    from ...kernel.writepath import ChangeTx, Origin

    rel = f"{_draft_rel(name)}/{mf.MANIFEST_FILE}"
    root = manager.project_root(project_id)
    text = (root / rel).read_text(encoding="utf-8")
    with ChangeTx(manager, project_id, Origin("plugin", actor), summary=f"插件草稿 {name} 升版本") as tx:
        tx.write(rel, with_version(text, version))
        tx.commit(f"chore: 插件草稿 {name} 版本号升到 {version}")


def _zip(directory: Path, target: Path) -> None:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel in [*pack_files(directory), mf.CHECKSUMS_FILE]:
            archive.write(directory / rel, rel)


def install(manager, project_id: str, name: str, actor: str) -> dict:
    """主线上的草稿装到 actor 名下并启用到本项目。返回 {version, upload_id, enabled: {version, effective}, action}。"""
    from ..packs import service

    with _project_install_lock(project_id):
        analysis = _analyze(manager, project_id, name, actor)
        manifest = analysis["manifest"]
        failed = [item for item in analysis["precheck"]["items"] if item["result"] == "fail"]
        repository = manager.packs.repository
        if manifest is not None and not repository.name_free(manifest.name, actor):
            raise DraftError("pack_name_taken", f"包名 {manifest.name} 已被占用，请换一个名字", 409)
        if manifest is None or failed:
            raise DraftError("static_check_failed", "平台预检没过", 422, _failed_details(failed))
        version = analysis["install"]["next_version"]
        root = manager.project_root(project_id)
        record = None if analysis["install"].get("reuse") else _upload_and_install(manager, project_id, name, version,
                                                                                  manifest, actor)
        entry = (lockfile.read(root) or PackLock()).get(name)
        if entry is not None and entry.enabled:
            # 草稿退回到装过的旧版本（撤回了这几轮）时是回滚
            action = "rollback" if mf.version_key(version) < mf.version_key(entry.version) else "upgrade"
            result = service.change_version(manager, project_id, name, version, kind=action, actor=actor)
        else:
            action = "enable"
            result = service.enable(manager, project_id, name, version, actor)
    return {"version": version, "upload_id": record["id"] if record else None, "label": manifest.label or name,
            "action": action, "enabled": {"version": (result.get("pack") or {}).get("locked_version") or version,
                                          "effective": result.get("effective")}}


def _upload_and_install(manager, project_id: str, name: str, version: str, manifest, actor: str) -> dict:
    """版本号写回草稿（要升号时），打包，走普通用户上传同一条路：静态检查、装到本人名下。"""
    repository = manager.packs.repository
    if version != manifest.version:
        _write_version(manager, project_id, name, version, actor)
    root = manager.project_root(project_id)
    store = UploadStore(repository, manager.assets_dir)
    with tempfile.TemporaryDirectory(prefix="onto-draft-install-") as tmp:
        directory = Path(tmp) / name
        export(root, name, directory)
        write_checksums(directory)
        archive = Path(tmp) / f"{name}-{version}.zip"
        _zip(directory, archive)
        try:
            record = store.create(archive, archive.name, by=actor, private=True)
        except UploadError as exc:
            raise DraftError(exc.code, str(exc), exc.status, exc.details) from exc
    record = store.run_checks(record["id"])
    if record["state"] != "passed":
        items = [item for item in record.get("checks") or [] if item["result"] == "fail"]
        raise DraftError("static_check_failed", "静态检查没过", 422,
                         {**_failed_details(items), "upload_id": record["id"]})
    try:
        return store.install_private(record["id"], by=actor)
    except UploadError as exc:
        raise DraftError(exc.code, str(exc), exc.status, exc.details) from exc


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
