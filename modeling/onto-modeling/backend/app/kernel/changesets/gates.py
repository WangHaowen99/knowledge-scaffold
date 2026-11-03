"""合入时的检查（详设 8.2「把变更集叠到最新主线上重新校验」；方案 C8；F3）：在合成树上跑规则检查，只看新增问题。

主线树与合成树各导出 `ontology/`、`research/` 到临时目录（资料目录挂主线工作区的那份），用本项目锁定版本的
`ontology_checker.py`（装配目录里哈希核对过的，方案 D19）各跑一遍；两边结果用 `candidate_revision._minus`
相减（同一问题挪了行号不算新问题）。主线那一份按树对象缓存。

建模过程中检查只提示、不挡合入（F3，用户 11-03 定）；发布时由发布门禁链拦。按段建模时，链接指向蓝图里已规划、
还没建的对象或操作记为「待建」（`pending_links`，带第几段），不算问题；没有蓝图或全量模式时照常算问题。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tarfile
import tempfile
import threading
from pathlib import Path

from ...services.script_env import minimal_env
from ...services.workspace.gitops import _run_git

_log = logging.getLogger(__name__)

CHECK_TIMEOUT_SECONDS = 180
EXPORT_DIRS = ("ontology", "research")
LINK_CATEGORY = "链接失效"
BLUEPRINT_REL = "research/blueprint.json"
_LINK = re.compile(r"\[\[([^\]]+)\]\]")
_cache: dict[tuple[str, str], list[dict]] = {}
_cache_lock = threading.Lock()
_CACHE_MAX = 64


class GateUnavailable(RuntimeError):
    """门禁跑不起来（检查脚本缺失、超时、输出读不出）。"""


def _export(root: Path, tree: str, dest: Path) -> None:
    present = _run_git(root, ("ls-tree", "--name-only", tree, "--", *EXPORT_DIRS)).stdout.split()
    if present:
        archive = dest / "tree.tar"
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "archive", "--format=tar", "-o", str(archive), tree, "--",
             *present], cwd=root, capture_output=True, timeout=120,
        )
        if result.returncode != 0:
            raise GateUnavailable(f"导出待检查的树失败：{result.stderr.decode(errors='replace').strip()}")
        with tarfile.open(archive) as bundle:
            kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            bundle.extractall(dest, **kwargs)
        archive.unlink()
    (dest / "ontology").mkdir(exist_ok=True)
    if (root / "resource").is_dir():
        os.symlink(root / "resource", dest / "resource")


def _checker(root: Path) -> Path:
    from ... import config
    from ...services.packs.assembler import trusted_script

    return trusted_script(root, "ontology_checker.py", Path(config.ASSETS_DIR))


def run(root: Path, tree: str) -> list[dict]:
    """在 tree（树对象）上跑规则检查，返回问题列表（ontology_checker --json 的条目）。"""
    root = Path(root)
    key = (str(root), tree)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    script = _checker(root)
    if not script.is_file():
        raise GateUnavailable("规则检查脚本不存在")
    with tempfile.TemporaryDirectory(prefix="onto-gate-") as scratch:
        workdir = Path(scratch)
        _export(root, tree, workdir)
        try:
            result = subprocess.run(
                ["python3", str(script), str(workdir / "ontology"), "--json", "--include-mock"],
                cwd=workdir, capture_output=True, text=True, timeout=CHECK_TIMEOUT_SECONDS, env=minimal_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise GateUnavailable("规则检查超时") from exc
    try:
        issues = json.loads(result.stdout or "")
    except ValueError as exc:
        raise GateUnavailable(f"规则检查的结果读不出来：{(result.stderr or '').strip()[-300:]}") from exc
    issues = [item for item in issues if isinstance(item, dict)]
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[key] = issues
    return issues


def _minus(root: Path):
    from ...services.packs import contracts

    return contracts.module_for(root, "candidate_revision")._minus


def finding(issue: dict) -> dict:
    """检查器的一条问题 → 契约 Finding。"""
    rel = str(issue.get("file") or "")
    return {
        "code": str(issue.get("category") or "check"),
        "level": "error" if issue.get("severity") == "错误" else "warning",
        "message": str(issue.get("message") or "")[:500],
        "path": f"ontology/{rel}" if rel and not rel.startswith("ontology/") else rel,
    }


def compare(root: Path, main_tree: str, candidate_tree: str) -> dict:
    """合成树相对主线新增的问题：{errors, warnings, findings: Finding[], pending, pending_links}。

    findings 只含新增的、不含待建的，错误在前；pending_links 是指向蓝图里已规划、还没建的内容的链接
    [{target, slice_id, slice_name, slice_no}]，不计进 errors、warnings。"""
    before = run(root, main_tree)
    after = run(root, candidate_tree)
    new = _minus(root)([_normalized(item) for item in after], [_normalized(item) for item in before])
    planned = _planned(root, candidate_tree)
    pending, kept = [], []
    for item in new:
        target = link_target(item)
        hit = planned.get(target) if target else None
        if hit is not None:
            if not any(entry["target"] == target for entry in pending):
                pending.append({"target": target, **hit})
            continue
        kept.append(item)
    errors = [item for item in kept if item.get("severity") == "错误"]
    warnings = [item for item in kept if item.get("severity") == "警告"]
    return {"errors": len(errors), "warnings": len(warnings),
            "findings": [finding(item) for item in [*errors, *warnings]][:200],
            "pending": len(pending), "pending_links": pending[:200]}


def link_target(issue: dict) -> str | None:
    """链接失效的问题指向的条目名（去掉路径、别名、锚点、扩展名）；不是链接失效时 None。"""
    if (issue.get("category") or issue.get("code")) != LINK_CATEGORY:
        return None
    found = _LINK.search(str(issue.get("message") or ""))
    if not found:
        return None
    name = found.group(1).split("|", 1)[0].split("#", 1)[0].strip().rsplit("/", 1)[-1]
    return name[:-3] if name.endswith(".md") else name or None


def _planned(root: Path, tree: str) -> dict[str, dict]:
    """合成树上的蓝图里按段规划的条目名 → {slice_id, slice_name, slice_no}（第几段按没排除的段的顺序）。
    没有蓝图、读不出、全量模式时为空：链接失效照常算问题。"""
    from . import worktree

    data = worktree.read_blob(root, tree, BLUEPRINT_REL)
    if not data:
        return {}
    try:
        from ...services.flow import blueprint

        module = blueprint.script(root)
        doc = module.parse(data.decode("utf-8"))
        if module.mode(doc) != "sliced":
            return {}
        out: dict[str, dict] = {}
        for number, item in enumerate(module.live_slices(doc), 1):
            for names in module.members(item).values():
                for name in names:
                    out.setdefault(str(name), {"slice_id": item["id"], "slice_name": item["name"], "slice_no": number})
        return out
    except Exception:  # noqa: BLE001 - 蓝图读不出时不区分待建
        _log.warning("项目 %s 合成树上的蓝图读不出，链接失效不区分待建", Path(root).name, exc_info=True)
        return {}


def _normalized(issue: dict) -> dict:
    return {"file": str(issue.get("file") or ""), "severity": str(issue.get("severity") or ""),
            "category": str(issue.get("category") or ""), "message": str(issue.get("message") or "")}
