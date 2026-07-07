#!/usr/bin/env python3
"""Convenience wrapper for the temporary agent simulation companion."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from agent_sim_runner import main


if __name__ == "__main__":
    raise SystemExit(main())
