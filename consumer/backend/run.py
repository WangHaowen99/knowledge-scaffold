"""Entry point for running the Flask server."""

# Load .env FIRST, before any module reads os.getenv(...).
# Lookup order: backend/.env → <consumer>/.env
from pathlib import Path

try:
    from dotenv import load_dotenv

    _backend_env = Path(__file__).resolve().parent / ".env"
    _app_env = Path(__file__).resolve().parent.parent / ".env"
    if _backend_env.exists():
        load_dotenv(_backend_env)
    elif _app_env.exists():
        load_dotenv(_app_env)
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
