"""WSGI entry point for production (gunicorn).

Keeps the same .env loading behavior as run.py.
"""
from pathlib import Path

try:
    from dotenv import load_dotenv

    _backend_env = Path(__file__).resolve().parent / ".env"
    _repo_env = Path(__file__).resolve().parent.parent / ".env"
    if _backend_env.exists():
        load_dotenv(_backend_env)
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
