#!/usr/bin/env python3
"""Convenience wrapper for the temporary agent simulation companion."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "platform" / "server"
SKILLS = ROOT / "modeling" / "skills"
SCRIPTS = ROOT / "examples" / "scripts"
for path in (SERVER, SKILLS, SCRIPTS):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from agent_sim_runner import main


if __name__ == "__main__":
    raise SystemExit(main())
