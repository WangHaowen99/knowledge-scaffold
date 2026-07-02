"""Entry point for running the Flask server."""

# Load .env FIRST, before any module reads os.getenv(...).
# Lookup order: platform/server/.env, then <repo>/.env.
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

from agentic import create_app

# Import server configuration
try:
    from server_config import (
        SERVER_HOST,
        SERVER_PORT,
        SERVER_DEBUG,
        SERVER_AUTO_RELOAD
    )
except ImportError:
    # Fallback to defaults if config not found
    SERVER_HOST = "0.0.0.0"
    SERVER_PORT = 5055
    SERVER_DEBUG = True
    SERVER_AUTO_RELOAD = False


if __name__ == "__main__":
    app = create_app()
    # use_reloader=False prevents semaphore leak on Ctrl+C
    # You still get debug error pages; just no auto-reload on file changes
    app.run(
        host=SERVER_HOST,
        port=SERVER_PORT,
        debug=SERVER_DEBUG,
        use_reloader=SERVER_AUTO_RELOAD,
        threaded=True,
    )
