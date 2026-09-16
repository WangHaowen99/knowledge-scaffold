# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Independent OR-Tools ``ortools.solver.v1`` Solver Service."""

# Pytest may prepend this directory to ``sys.path`` because its basename is
# ``ortools``.  Keep the third-party OR-Tools namespace discoverable in that
# situation so ``from ortools.sat.python import cp_model`` resolves to the
# installed engine rather than this service package.
if __name__ == "ortools":  # pragma: no cover - import-path compatibility
    import site
    import sysconfig
    from pathlib import Path

    _vendor_roots = [Path(sysconfig.get_paths()["purelib"]), Path(site.getusersitepackages())]
    for _root in _vendor_roots:
        _vendor_path = _root / "ortools"
        if _vendor_path.is_dir() and str(_vendor_path) not in __path__:
            __path__.append(str(_vendor_path))
