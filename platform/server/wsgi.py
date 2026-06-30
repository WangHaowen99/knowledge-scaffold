"""WSGI entry point for production (gunicorn).

Keeps the same .env loading behavior as run.py.
"""
from pathlib import Path


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "platform").is_dir() and (candidate / "runtime").is_dir():
            return candidate
    return start


try:
    from dotenv import load_dotenv

    _server_dir = Path(__file__).resolve().parent
    _repo_root = _find_repo_root(_server_dir)
    _server_env = _server_dir / ".env"
    _repo_env = _repo_root / ".env"
    if _server_env.exists():
        load_dotenv(_server_env)
    elif _repo_env.exists():
        load_dotenv(_repo_env)
except ImportError:
    pass

import os

from agentic import create_app

app = create_app()

# Honour SERVER_DEBUG for dev environments.
# app.run(debug=...) is not called under gunicorn, so we set it explicitly.
if os.getenv("SERVER_DEBUG", "False") == "True":
    app.debug = True
