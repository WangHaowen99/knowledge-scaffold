"""Static release gate for the canonical native-async runtime."""

from __future__ import annotations

from pathlib import Path
import re
import sys


BACKEND_ROOT = Path(__file__).resolve().parents[1]
CONSUMER_ROOT = BACKEND_ROOT.parent
REPOSITORY_ROOT = CONSUMER_ROOT.parent

PRODUCTION_ROOTS = (
    BACKEND_ROOT / "agent",
    BACKEND_ROOT / "api",
    BACKEND_ROOT / "app.py",
    BACKEND_ROOT / "run.py",
    BACKEND_ROOT / "server_config.py",
    BACKEND_ROOT / "utils",
    BACKEND_ROOT / "workspace",
)

FORBIDDEN_PRODUCTION = {
    "Flask import": re.compile(r"(?:from|import)\s+flask\b", re.IGNORECASE),
    "Blueprint": re.compile(r"\bBlueprint\b"),
    "legacy Jiuwen loop": re.compile(r"\bLegacyJiuwenLoopRunner\b"),
    "async service loop": re.compile(r"\bAsyncServiceLoop\b"),
    "sync Future bridge": re.compile(r"\brun_coroutine_threadsafe\b"),
    "nested asyncio runner": re.compile(r"\basyncio\.run\s*\("),
    "sync loop runner": re.compile(r"\brun_until_complete\s*\("),
    "Starlette thread pool": re.compile(r"\brun_in_threadpool\b"),
    "AnyIO thread pool": re.compile(r"\banyio\.to_thread\b"),
    "unmanaged loop executor": re.compile(r"\brun_in_executor\s*\("),
    "atexit lifecycle": re.compile(r"\batexit\b"),
    "ACP registry": re.compile(r"\b_ACP_RUN_REGISTRY\b"),
    "private executor": re.compile(r"\bThreadPoolExecutor\b"),
    "unmanaged queue": re.compile(r"\bqueue\.Queue\b"),
    "unmanaged to_thread": re.compile(r"\basyncio\.to_thread\b"),
    "interaction resumed event": re.compile(r"interaction[.]resumed"),
}

ALLOWED_PRODUCTION_MATCHES = {
    "private executor": {BACKEND_ROOT / "agent/runtime/offload.py"},
}

REMOVED_FILES = (
    BACKEND_ROOT / "wsgi.py",
    BACKEND_ROOT / "agent/runtime/loop.py",
    BACKEND_ROOT / "agent/runtime/bridge.py",
    BACKEND_ROOT / "agent/runtime/bootstrap.py",
    BACKEND_ROOT / "persistence/run_store.py",
)

REMOVED_ROUTE_PATTERNS = (
    re.compile("/agentic/runs/" + "stream"),
    re.compile("/agentic/runs/" + "sync"),
    re.compile(r"/agentic/runs/(?:\{[^}]+\}|<[^>]+>|[^/\s]+)/" + "interact"),
    re.compile(r"/agentic/runs/(?:\{[^}]+\}|<[^>]+>|[^/\s]+)/" + "status"),
)


def _python_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.exists():
        return []
    return sorted(root.rglob("*.py"))


def _production_files() -> list[Path]:
    files: set[Path] = set()
    for root in PRODUCTION_ROOTS:
        files.update(_python_files(root))
    return sorted(files)


def _route_scan_files() -> list[Path]:
    roots = (
        BACKEND_ROOT,
        CONSUMER_ROOT / "frontend",
        CONSUMER_ROOT / "sdk",
        CONSUMER_ROOT / "docs",
        CONSUMER_ROOT / "README.md",
    )
    suffixes = {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".md",
        ".json",
        ".env",
        ".example",
    }
    files: set[Path] = set()
    for root in roots:
        if root.is_file():
            files.add(root)
            continue
        if not root.exists():
            continue
        files.update(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in suffixes
            and "node_modules" not in path.parts
            and "venv" not in path.parts
            and "__pycache__" not in path.parts
        )
    return sorted(files)


def collect_violations() -> list[str]:
    violations: list[str] = []
    for removed in REMOVED_FILES:
        if removed.exists():
            violations.append(
                f"{removed.relative_to(REPOSITORY_ROOT)}: removed file still exists"
            )

    for path in _production_files():
        text = path.read_text(encoding="utf-8")
        for label, pattern in FORBIDDEN_PRODUCTION.items():
            if path in ALLOWED_PRODUCTION_MATCHES.get(label, set()):
                continue
            if pattern.search(text):
                violations.append(
                    f"{path.relative_to(REPOSITORY_ROOT)}: forbidden {label}"
                )

    this_file = Path(__file__).resolve()
    for path in _route_scan_files():
        if path.resolve() == this_file:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in REMOVED_ROUTE_PATTERNS:
            if pattern.search(text):
                violations.append(
                    f"{path.relative_to(REPOSITORY_ROOT)}: removed Run route"
                )
                break
    return sorted(set(violations))


def main() -> int:
    violations = collect_violations()
    if violations:
        print("\n".join(violations))
        return 1
    print("native async architecture gate: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
