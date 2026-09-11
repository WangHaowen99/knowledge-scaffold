"""Migrate an ontology solver package to the operation-centric layout.

The command is dry-run by default. Pass ``--apply`` only after reviewing the
JSON plan. Applied migrations require a distinct ``--target`` and a new
``--target-version``; source revisions are never rewritten.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from ontology.solver_package_layout import (
    SolverPackageLayoutError,
    migrate_solver_package_layout,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate solver package sidecars into operations/ (dry-run by default)"
    )
    parser.add_argument(
        "source_positional",
        nargs="?",
        type=Path,
        help="Source package revision directory (legacy positional form)",
    )
    parser.add_argument(
        "--source",
        dest="source_option",
        type=Path,
        help="Source package revision directory",
    )
    parser.add_argument(
        "--target",
        type=Path,
        help="New package revision directory; required with --apply",
    )
    execution_mode = parser.add_mutually_exclusive_group()
    execution_mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply the migration; without this flag only a JSON plan is emitted",
    )
    execution_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicitly request the default dry-run behavior",
    )
    parser.add_argument(
        "--target-version",
        help="Optional version to write into the migrated manifest",
    )
    parser.add_argument(
        "--allow-missing-operation-index",
        action="store_true",
        help="Do not require operations/index.md during final validation",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = args.source_option or args.source_positional
    if source is None:
        print(
            json.dumps(
                {"dry_run": not args.apply, "ok": False, "error": "source is required"},
                ensure_ascii=False,
            )
        )
        return 2
    try:
        report = migrate_solver_package_layout(
            source,
            args.target,
            dry_run=not args.apply,
            require_operation_index=not args.allow_missing_operation_index,
            target_version=args.target_version,
        )
    except (SolverPackageLayoutError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"dry_run": not args.apply, "ok": False, "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1
    payload = {"ok": True, **report.as_dict()}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
