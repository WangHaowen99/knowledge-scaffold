"""F 第一批第 5 条：持锁的命令都声明了等回复的阶段（lock_waits），命令文档里也按约定在等回复前切 confirm。

声明了 lock_waits，平台才会在「做完了没放锁」时代为释放；声明了却不在文档里切 confirm，等回复会被当成做完。
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from app import config

ASSETS = Path(config.ASSETS_DIR)
FLOWS = [(ASSETS / "workspace-toolkit", ASSETS / "workspace-toolkit" / "flow.yaml"),
         *[(path.parent, path) for path in sorted((ASSETS / "packs").glob("*/flow.yaml"))]]


def _docs(pack: Path) -> dict[str, str]:
    return {path.stem: path.read_text(encoding="utf-8") for path in (pack / "commands").glob("*.md")}


def test_commands_holding_locks_declare_waits_and_follow_them():
    problems = []
    for pack, flow_path in FLOWS:
        commands = (yaml.safe_load(flow_path.read_text(encoding="utf-8")) or {}).get("commands") or {}
        docs = _docs(pack)
        for name, spec in commands.items():
            doc = docs.get(f"ontology-{name}") or docs.get(name) or ""
            if not re.search(rf"state_helper\.py acquire {re.escape(name)} ", doc):
                continue
            if "confirm" not in (spec.get("lock_waits") or []):
                problems.append(f"{flow_path.parent.name}/{name} 没声明 lock_waits")
            if not re.search(rf"acquire {re.escape(name)} confirm", doc):
                problems.append(f"{flow_path.parent.name}/{name} 文档里没有在等回复前切 confirm")
    assert problems == []
