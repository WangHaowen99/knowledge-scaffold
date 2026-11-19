"""Prepare a stopped Core PVC and reuse the Onto project migrator in a CCE Job."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

logger = logging.getLogger(__name__)


def prepare_source(source: Path, destination: Path) -> None:
    database = source / "modeling.db"
    if not database.is_file():
        raise FileNotFoundError("Core modeling.db is missing")
    # The old writer is stopped. Copy the SQLite triplet together so a WAL
    # transaction is visible even when the CSI mount is read-only.
    for suffix in ("", "-wal", "-shm"):
        item = source / ("modeling.db" + suffix)
        if item.is_file():
            shutil.copy2(item, destination / item.name)
    staged = destination / "modeling.db"
    canonical = destination / "modeling-complete.db"
    with sqlite3.connect(str(staged)) as reader, sqlite3.connect(str(canonical)) as writer:
        reader.backup(writer)
        if writer.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Core SQLite integrity check failed")
    canonical.replace(staged)
    for suffix in ("-wal", "-shm"):
        (destination / ("modeling.db" + suffix)).unlink(missing_ok=True)
    artifacts = source / "artifacts"
    if artifacts.is_dir():
        (destination / "artifacts").symlink_to(artifacts, target_is_directory=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--summary-file", type=Path, required=True)
    args = parser.parse_args()
    args.summary_file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="core-migration-", dir=args.summary_file.parent) as temporary:
        staged = Path(temporary)
        prepare_source(args.source, staged)
        result = subprocess.run(  # NOSONAR(S4721): fixed interpreter and sibling migrator
            [sys.executable, str(Path(__file__).with_name("migrate_from_modeling.py")),
             "--source", str(staged), "--summary-file", str(args.summary_file)],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            shell=False,
        )
        if result.returncode:
            logger.error("Core project migration failed; source and target volumes remain available")
            return result.returncode
        summary = json.loads(args.summary_file.read_text(encoding="utf-8"))
        # The CCE caller consumes this JSON line as a machine-readable result.
        sys.stdout.write(json.dumps(summary, sort_keys=True) + "\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
