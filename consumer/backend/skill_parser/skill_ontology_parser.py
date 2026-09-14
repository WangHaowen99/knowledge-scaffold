# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Entrypoint for converting modeling-template ontology sources."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import TextIO

from .mapping import build_file_mapping
from .pipeline import run

logger = logging.getLogger(__name__)


def _configure_cli_logging(stream: TextIO) -> None:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert modeling-template ontology source folders to runtime ontology layout",
    )
    parser.add_argument(
        "--source",
        required=True,
        help=(
            "Path to extracted source root (contains object/logic/SKILL.md, scripts optional; action is legacy"
            " fallback)"
        ),
    )
    parser.add_argument(
        "--target",
        required=False,
        help="Path to ontology target root (default: source/../..)",
    )
    parser.add_argument(
        "--write-mapping-json",
        action="store_true",
        help="Write mapping json file to target root",
    )
    return parser


def main() -> None:
    _configure_cli_logging(sys.stdout)
    parser = _build_cli_parser()
    args = parser.parse_args()

    source_root = Path(args.source).resolve()
    if args.target:
        target_root = Path(args.target).resolve()
    else:
        target_root = source_root.parent.parent

    result = run(
        source_root=source_root,
        target_root=target_root,
        write_mapping_json=bool(args.write_mapping_json),
    )
    logger.info(json.dumps(result, ensure_ascii=False, indent=2))


__all__ = [
    "build_file_mapping",
    "run",
    "main",
]


if __name__ == "__main__":
    main()
