"""Execute qualified package code with runtime-enforced capabilities."""

from __future__ import annotations

import builtins
import importlib.machinery
import io
import json
import os
import runpy
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


def _load_runtime_contract() -> Mapping[str, Any]:
    module_path = Path(__file__).resolve()
    candidates = (
        module_path.parents[2] / "runtime-contract.json",
        module_path.parents[1] / "integration" / "sandbox" / "runtime-contract.json",
    )
    for path in candidates:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("version") == 1:
                return payload
    raise RuntimeError("deterministic runtime contract is unavailable")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def _local_module_exists(module_name: str, search_roots: Sequence[Path]) -> bool:
    parts = tuple(part for part in module_name.split(".") if part)
    if not parts:
        return False
    for root in search_roots:
        candidate = root.joinpath(*parts)
        if candidate.with_suffix(".py").is_file() or (candidate / "__init__.py").is_file():
            return True
    return False


def _local_module_names(script_dir: Path, entrypoint: Path) -> frozenset[str]:
    names: set[str] = set()
    for candidate in script_dir.iterdir():
        if candidate == entrypoint:
            continue
        if candidate.is_file() and candidate.suffix == ".py":
            if candidate.stem != "__init__" and candidate.stem.isidentifier():
                names.add(candidate.stem)
            continue
        if not candidate.is_dir() or not candidate.name.isidentifier():
            continue
        if (candidate / "__init__.py").is_file():
            names.add(candidate.name)
    return frozenset(names)


def _reject_local_module_collisions(
    import_roots: Sequence[Path],
    entrypoint: Path,
) -> None:
    """Reject local imports that would shadow an installed runtime module."""
    root_values = frozenset(os.path.realpath(root) for root in import_roots)
    runtime_paths: list[str] = []
    for raw_path in sys.path:
        resolved = os.path.realpath(raw_path or os.getcwd())
        if resolved not in root_values:
            runtime_paths.append(raw_path)
    local_modules: set[str] = set()
    for import_root in import_roots:
        for module_name in _local_module_names(import_root, entrypoint):
            if module_name in local_modules:
                raise ImportError("local deterministic module name is ambiguous")
            local_modules.add(module_name)
            runtime_spec = importlib.machinery.PathFinder.find_spec(
                module_name,
                runtime_paths,
            )
            if runtime_spec is not None:
                raise ImportError("local deterministic module collides with a runtime module")


def _path_value_is_within(raw_path: object, root: str) -> bool:
    value = str(raw_path or "").strip()
    if not value:
        return False
    try:
        return os.path.commonpath((os.path.realpath(value), root)) == root
    except (OSError, ValueError):
        return False


def _package_caller(ontology_root: str) -> bool:
    try:
        frame = sys._getframe(2)
    except ValueError:
        return False
    while frame is not None:
        raw_path = str(frame.f_globals.get("__file__") or "").strip()
        if raw_path:
            if _path_value_is_within(raw_path, ontology_root):
                return True
            # The first real caller outside this guard decides the capability
            # boundary. Imports and SDK internals execute from trusted runtime
            # paths and may use their own private implementation capabilities.
            if os.path.realpath(raw_path) != os.path.realpath(__file__):
                return False
        frame = frame.f_back
    return False


def _install_runtime_guards(
    *,
    ontology_root: Path,
    script_dir: Path,
    contract: Mapping[str, Any],
) -> None:
    allowed_standard = frozenset(str(item).strip() for item in contract.get("allowed_standard_library_imports") or [])
    allowed_external_roots = frozenset(
        str(item).strip().split(".", 1)[0] for item in contract.get("allowed_external_import_roots") or []
    )
    runtime_imports = contract.get("allowed_runtime_imports") or {}
    allowed_runtime_exports = {
        str(module).strip(): frozenset(str(item).strip() for item in exports)
        for module, exports in runtime_imports.items()
        if isinstance(exports, list)
    }
    if not allowed_standard or not allowed_runtime_exports:
        raise RuntimeError("deterministic runtime capability contract is invalid")

    original_import = builtins.__import__
    ontology_root_value = os.path.realpath(ontology_root)
    forbidden_import_exports = frozenset(
        {
            "asyncio",
            "concurrent",
            "ctypes",
            "datetime",
            "glob",
            "importlib",
            "inspect",
            "marshal",
            "multiprocessing",
            "os",
            "pathlib",
            "pickle",
            "platform",
            "random",
            "resource",
            "secrets",
            "signal",
            "socket",
            "subprocess",
            "sys",
            "tempfile",
            "threading",
            "time",
            "uuid",
        }
    )

    def guarded_import(
        name: str,
        globals: Mapping[str, Any] | None = None,  # pylint: disable=redefined-builtin
        locals: Mapping[str, Any] | None = None,  # pylint: disable=redefined-builtin
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> Any:
        caller_path = str((globals or {}).get("__file__") or "").strip()
        package_call = (
            _path_value_is_within(caller_path, ontology_root_value)
            if caller_path
            else _package_caller(ontology_root_value)
        )
        if not package_call:
            return original_import(name, globals, locals, fromlist, level)
        module_name = str(name or "").strip()
        import_root = module_name.split(".", 1)[0]
        requested_exports = frozenset(str(item).strip() for item in (fromlist or ()))
        if package_call and requested_exports.intersection(forbidden_import_exports):
            raise ImportError("module export is outside the deterministic runtime capability contract")
        runtime_import_allowed = (
            module_name in allowed_runtime_exports
            and bool(requested_exports)
            and requested_exports.issubset(allowed_runtime_exports[module_name])
        )
        allowed = (
            level > 0
            or module_name in allowed_standard
            or import_root in allowed_external_roots
            or runtime_import_allowed
            or _local_module_exists(module_name, (script_dir, ontology_root))
        )
        if not allowed:
            raise ImportError("module is outside the deterministic runtime capability contract")
        return original_import(name, globals, locals, fromlist, level)

    builtins.__import__ = guarded_import

    def guard_callable(original: Callable[..., Any], capability: str) -> Callable[..., Any]:
        def guarded(*args: Any, **kwargs: Any) -> Any:
            if _package_caller(ontology_root_value):
                raise PermissionError("{} is unavailable to deterministic package code".format(capability))
            return original(*args, **kwargs)

        return guarded

    builtins.open = guard_callable(builtins.open, "filesystem open")
    io.open = guard_callable(io.open, "filesystem open")
    for name in (
        "getrandom",
        "urandom",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "fork",
        "forkpty",
        "popen",
        "posix_spawn",
        "posix_spawnp",
        "setpgid",
        "setsid",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "system",
    ):
        original = getattr(os, name, None)
        if callable(original):
            setattr(os, name, guard_callable(original, "os.{}".format(name)))
    for name in (
        "breakpoint",
        "compile",
        "delattr",
        "eval",
        "exec",
        "getattr",
        "id",
        "input",
        "setattr",
        "vars",
    ):
        original = getattr(builtins, name, None)
        if callable(original):
            setattr(builtins, name, guard_callable(original, name))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    if not arguments:
        raise ValueError("deterministic script path is required")

    ontology_root = Path.cwd().resolve()
    script_path = Path(arguments[0]).resolve()
    if not script_path.is_file() or not _is_within(script_path, ontology_root):
        raise ValueError("deterministic script must be inside the ontology directory")
    contract = _load_runtime_contract()
    import_roots = [script_path.parent]
    if script_path.parent != ontology_root:
        import_roots.append(ontology_root)
    _reject_local_module_collisions(import_roots, script_path)
    _install_runtime_guards(
        ontology_root=ontology_root,
        script_dir=script_path.parent,
        contract=contract,
    )
    original_argv = sys.argv
    original_path = sys.path
    sys.argv = [str(script_path), *arguments[1:]]
    sys.path = [*(str(root) for root in import_roots), *original_path]
    try:
        runpy.run_path(str(script_path), run_name="__main__")
    finally:
        sys.argv = original_argv
        sys.path = original_path
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
