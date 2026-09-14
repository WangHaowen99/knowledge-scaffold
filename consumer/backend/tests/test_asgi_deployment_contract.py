# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement
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


def test_backend_pins_plain_openjiuwen_0_1_17_without_feature_extras():
    requirements = (BACKEND_ROOT / "requirements.txt").read_text(encoding="utf-8")
    active_requirement_lines = [
        line.strip() for line in requirements.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    normalized_lines = {line.lower() for line in active_requirement_lines}
    parsed_requirements = [Requirement(line) for line in active_requirement_lines]
    openjiuwen_requirements = [
        requirement for requirement in parsed_requirements if requirement.name.lower() == "openjiuwen"
    ]

    assert "openjiuwen==0.1.17" in normalized_lines
    assert len(openjiuwen_requirements) == 1
    assert not openjiuwen_requirements[0].extras
    assert "fastmcp>=2.14.2,<3.0" in normalized_lines


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
