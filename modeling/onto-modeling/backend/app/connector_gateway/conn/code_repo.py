"""代码仓库提供方 `code-repo`（ADR-0084；证据采集接口 §8、§10）。

`test`：`git ls-remote` 验证能访问、令牌能读。`snapshot`：加固浅取一个提交 → 文件清单 → 按语言分析 → 发记录。
`ctx.result(artifact=…)` 交出 `{repo_url（去掉用户信息）, ref, commit}`，网关写进快照清单的 `artifact`。
只读源码，不执行、不装依赖；识别不了的写法和解析失败的文件汇总成局限。
"""

from __future__ import annotations

import importlib.metadata
import re
import time
from dataclasses import dataclass, field

from . import catalog, code_git
from .code_model import Sink
from .context import BudgetExceeded, ConnError, Context

INFO = {key: value for key, value in catalog.PROVIDERS["code-repo"].items() if key not in catalog.INTERNAL}

LANGUAGES = {".java": "java", ".py": "python", ".ts": "typescript"}
# 仓库里有、但这一版不分析的源码，按扩展名汇总进局限
OTHER_SOURCES = {".kt": "Kotlin", ".scala": "Scala", ".groovy": "Groovy", ".go": "Go", ".cs": "C#", ".php": "PHP",
                 ".rb": "Ruby", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".tsx": "TSX",
                 ".rs": "Rust", ".prisma": "Prisma schema", ".sql": "SQL 脚本"}
VENDORED = ("node_modules/", "vendor/", "third_party/", "dist/", "build/", "target/", "out/", ".venv/", "venv/",
            "site-packages/", "__pycache__/", ".git/", "generated/", ".next/", "coverage/")
TEST_PARTS = ("src/test/", "test/", "tests/", "__tests__/", "spec/")
_TEST_FILE = re.compile(r"^test_.*\.py$|_test\.py$|\.(spec|test)\.ts$|(Test|Tests|IT)\.java$")
INVENTORY_MAX = 5000
SKIPPED_MAX = 2000
READ_BATCH_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_BYTES = 256 * 1024 * 1024


def _language(path: str) -> str | None:
    if path.endswith(".d.ts"):
        return None
    for suffix, language in LANGUAGES.items():
        if path.endswith(suffix):
            return language
    return None


def _excluded(path: str) -> str | None:
    lowered = f"/{path.lower()}"
    if any(f"/{part}" in lowered for part in VENDORED):
        return "vendored"
    name = path.rsplit("/", 1)[-1]
    if any(f"/{part}" in lowered for part in TEST_PARTS) or _TEST_FILE.search(name):
        return "test"
    return None


def _analyzer(language: str):
    # 按需导入：javalang 只在分析 Java 时才要
    if language == "java":
        from . import code_java
        return code_java.analyze
    if language == "python":
        from . import code_python
        return code_python.analyze
    from . import code_ts
    return code_ts.analyze


@dataclass
class Plan:
    to_read: list[tuple[code_git.Entry, str]] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    others: dict[str, int] = field(default_factory=dict)
    budget_bytes: int = 0

    def count(self, reason: str) -> int:
        return sum(1 for item in self.skipped if item["reason"] == reason)


def plan(ctx: Context, entries: list[code_git.Entry], max_bytes: int) -> Plan:
    """分出要读的源码和跳过的文件；源码按顺序累计，超过 `max_bytes` 的记 `budget`。"""
    result = Plan()
    total = 0
    for entry in entries:
        language = _language(entry.path)
        reason = None
        if entry.is_submodule:
            reason = "submodule"
        elif entry.is_symlink:
            reason = "symlink"
        elif not ctx.in_scope(entry.path):
            reason = "scope"
        elif language is None:
            name = entry.path.rsplit("/", 1)[-1]
            suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
            if suffix in OTHER_SOURCES and not _excluded(entry.path):
                label = OTHER_SOURCES[suffix]
                result.others[label] = result.others.get(label, 0) + 1
        else:
            reason = _excluded(entry.path)
            if reason is None and entry.size > code_git.FILE_MAX_BYTES:
                reason = "too_large"
            elif reason is None and total + entry.size > max_bytes:
                reason = "budget"
                result.budget_bytes += entry.size
        if reason:
            result.skipped.append({"object": entry.path, "reason": reason})
            continue
        result.items.append({"object": entry.path, "kind": language or "file", "estimate_bytes": entry.size})
        if language:
            result.to_read.append((entry, language))
            total += entry.size
    return result


def _batches(files: list[tuple[code_git.Entry, str]]):
    batch: list[tuple[code_git.Entry, str]] = []
    size = 0
    for item in files:
        if batch and size + item[0].size > READ_BATCH_BYTES:
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += item[0].size
    if batch:
        yield batch


def _decode(data: bytes) -> str | None:
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", "replace").lstrip("﻿")


def snapshot(ctx: Context, *, allow_http: bool = False) -> None:
    """`allow_http` 只给测试放开本机 http 仓库，网关的运行器不传。"""
    max_bytes = int(ctx.budget.get("max_bytes") or DEFAULT_MAX_BYTES)
    ctx.progress("clone", 0, 1, "repo")
    target = code_git.parse_url(ctx.connection.get("repo_url"), allow_http=allow_http)
    ref = code_git.check_ref(ctx.connection.get("ref"))
    repo = code_git.clone(ctx.connection, ctx.secret.get("token", ""), ctx.egress, remaining_s=ctx.remaining_s,
                          max_bytes=max_bytes, allow_http=allow_http)
    artifact = {"repo_url": target.url, "ref": ref, "commit": repo.commit}
    try:
        ctx.progress("clone", 1, 1, "repo")
        files = plan(ctx, repo.entries, max_bytes)
        ctx.inventory(files.items[:INVENTORY_MAX], files.skipped[:SKIPPED_MAX])
        if len(files.items) > INVENTORY_MAX or len(files.skipped) > SKIPPED_MAX:
            ctx.limitation(f"文件太多，清单只列了前 {INVENTORY_MAX} 个文件和前 {SKIPPED_MAX} 个跳过的文件")
        complete, done = _analyze(ctx, repo, files.to_read)
    finally:
        repo.close()
    if files.count("too_large"):
        ctx.limitation(f"{files.count('too_large')} 个源码文件超过 1 MiB，没有读")
    if files.budget_bytes:
        ctx.limitation(f"源码总量超过预算 {max_bytes} 字节，{files.count('budget')} 个文件没有分析")
    if files.count("submodule"):
        ctx.limitation(f"{files.count('submodule')} 个子模块没有拉取")
    for language, count in sorted(files.others.items()):
        ctx.limitation(f"{language} 文件 {count} 个，这一版不分析")
    total = sum(entry.size for entry, _ in files.to_read) + files.budget_bytes
    coverage = 100.0 if not total else min(100.0, done * 100.0 / total)
    ctx.result(complete=complete and not files.budget_bytes, coverage_pct=coverage, artifact=artifact,
               tool=_tool(files, repo))


def _tool(files: Plan, repo: code_git.Clone) -> dict:
    """分析了 Java 就报 javalang 的版本（记录内容随它变），否则报取代码用的 git。"""
    if any(language == "java" for _, language in files.to_read):
        try:
            return {"name": "javalang", "version": importlib.metadata.version("javalang")}
        except importlib.metadata.PackageNotFoundError:
            pass
    return {"name": "git", "version": repo.git_version}


def _analyze(ctx: Context, repo: code_git.Clone, to_read: list[tuple[code_git.Entry, str]]) -> tuple[bool, int]:
    """逐批读 blob、逐个文件分析；预算或时限到了就停，回 (完整没有, 分析过的字节数)。"""
    sink = Sink(ctx.source_id, ctx.record)
    failures: dict[str, list[str]] = {}
    done_files = 0
    done_bytes = 0
    complete = True
    names = {"java": "Java", "python": "Python", "typescript": "TypeScript"}
    try:
        for batch in _batches(to_read):
            ctx.check_deadline()
            contents = repo.read([entry for entry, _ in batch], timeout=ctx.remaining_s())
            for entry, language in batch:
                ctx.check_deadline()
                text = _decode(contents.get(entry.path, b""))
                if text is None:
                    failures.setdefault(names[language], []).append(entry.path)
                    continue
                try:
                    _analyzer(language)(sink, entry.path, text)
                except (ConnError, MemoryError):
                    raise
                except Exception as exc:  # noqa: BLE001 - 目标代码不可信，解析器抛什么都只算这个文件失败
                    failures.setdefault(names[language], []).append(f"{entry.path}（{type(exc).__name__}）")
                done_files += 1
                done_bytes += entry.size
            ctx.progress("analyze", done_files, len(to_read), "file")
        sink.finish()
    except BudgetExceeded as exc:
        complete = False
        ctx.limitation(f"{exc.message}，分析在第 {done_files} 个文件处停下")
    except ConnError as exc:
        if exc.code != "timeout":
            raise
        complete = False
        ctx.limitation(f"超过任务时限，分析在第 {done_files} 个文件处停下")
    for language, files in failures.items():
        shown = "、".join(files[:5]) + (f" 等 {len(files)} 个" if len(files) > 5 else "")
        ctx.limitation(f"{language} 解析失败：{shown}")
    for text in sink.limitations():
        ctx.limitation(text)
    return complete, done_bytes


def test(ctx: Context, *, allow_http: bool = False) -> None:
    started = time.monotonic()
    try:
        refs = code_git.ls_remote(ctx.connection, ctx.secret.get("token", ""), ctx.egress,
                                  timeout=min(60.0, ctx.remaining_s()), allow_http=allow_http)
    except ConnError as exc:
        ctx.result(complete=False, checks=[{"name": "connectivity", "ok": False, "detail": exc.message,
                                            "latency_ms": int((time.monotonic() - started) * 1000)}])
        return
    latency = int((time.monotonic() - started) * 1000)
    ref = code_git.check_ref(ctx.connection.get("ref"))
    readonly = "令牌是否只读验证不了；采集只浅取、不推送"
    names = {name for _, name in refs}
    if ref == "HEAD" or code_git.is_commit(ref):
        ok, detail = True, f"能访问。{readonly}"
    elif {ref, f"refs/heads/{ref}", f"refs/tags/{ref}"} & names:
        ok, detail = True, f"能访问，找到 {ref}。{readonly}"
    else:
        ok, detail = False, f"能访问，但找不到 {ref}"
    ctx.result(complete=ok, checks=[{"name": "connectivity", "ok": ok, "detail": detail, "latency_ms": latency}])
