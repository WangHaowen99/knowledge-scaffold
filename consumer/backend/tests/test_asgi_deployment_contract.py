from pathlib import Path
import re

import pytest

from server_config import validate_server_settings


BACKEND_ROOT = Path(__file__).resolve().parents[1]
CONSUMER_ROOT = BACKEND_ROOT.parent


def test_server_config_rejects_multiple_workers_and_short_grace():
    with pytest.raises(RuntimeError, match="exactly one"):
        validate_server_settings(workers=2, graceful_shutdown_seconds=45)
    with pytest.raises(RuntimeError, match="at least 45"):
        validate_server_settings(workers=1, graceful_shutdown_seconds=44)
    validate_server_settings(workers=1, graceful_shutdown_seconds=45)


def test_backend_image_uses_single_worker_uvicorn_with_shutdown_grace():
    dockerfile = (BACKEND_ROOT / "Dockerfile").read_text(encoding="utf-8")
    normalized = dockerfile.lower()

    assert "uvicorn" in normalized
    assert '"--workers=1"' in dockerfile
    assert '"--timeout-graceful-shutdown=45"' in dockerfile
    assert "stopsignal sigterm" in normalized
    assert "gunicorn" not in normalized
    assert "wsgi" not in normalized
    assert "flask" not in normalized


def test_backend_dependencies_are_asgi_only():
    requirements = (BACKEND_ROOT / "requirements.txt").read_text(encoding="utf-8")
    names = {
        line.split("=", 1)[0].split("<", 1)[0].split(">", 1)[0].strip().lower()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {"fastapi", "starlette", "uvicorn", "httpx", "python-multipart"} <= names
    assert "flask" not in names
    assert "flask-cors" not in names
    assert "gunicorn" not in names


@pytest.mark.parametrize(
    "compose_path",
    (
        CONSUMER_ROOT / "docker-compose.yml",
        BACKEND_ROOT / "docker-compose.yml",
        CONSUMER_ROOT / "deploy" / "docker-compose.release.yml",
    ),
)
def test_backend_compose_grants_the_runtime_45_seconds(compose_path):
    compose = compose_path.read_text(encoding="utf-8")
    match = re.search(r"(?ms)^  backend:\n(?P<body>.*?)(?=^  \S|\Z)", compose)
    assert match is not None
    backend_block = match.group("body")

    assert "stop_grace_period: 45s" in backend_block
    assert "FLASK_APP" not in backend_block
