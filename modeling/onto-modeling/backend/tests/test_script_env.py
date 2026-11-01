"""后端跑工作区 / 工具箱脚本时不继承后端环境（D19）。

用合成的「密钥」环境变量，断言反查脚本、示例注入脚本与 Jiuwen 守卫钩子都拿不到。
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.routers import projects
from app.services import auth
from app.services.engines.jiuwen_assets import run_hooks_sync
from app.services.script_env import minimal_env
from app.services.workspace import WorkspaceManager

SECRETS = {
    "ANTHROPIC_AUTH_TOKEN": "synthetic-anthropic-token",
    "ANTHROPIC_API_KEY": "synthetic-anthropic-key",
    "OPENAI_API_KEY": "synthetic-openai-key",
    "ONTO_WEB_JIUWEN_API_KEY": "synthetic-jiuwen-key",
    "ONTO_WEB_MODEL_SECRET_KEY": "synthetic-master-key-0000000000000000",
    "ONTO_WEB_AUTH_ADMIN_TOKEN": "synthetic-admin-token",
    "ONTO_WEB_MODELING_TOKEN": "synthetic-modeling-token",
    "ONTO_WEB_MODELING_CLEANUP_TOKEN": "synthetic-cleanup-token",
    "JWT_SECRET": "synthetic-jwt-secret",
}

# 把子进程能看到的环境变量名写到工作区，供断言
DUMP_ENV = (
    "import json, os, pathlib\n"
    "out = pathlib.Path('research') / 'env-{name}.json'\n"
    "out.parent.mkdir(exist_ok=True)\n"
    "out.write_text(json.dumps(sorted(os.environ)))\n"
    "print('# synthetic')\n"
)


@pytest.fixture()
def secret_env(monkeypatch):
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)


def assert_no_secrets(names: list[str]) -> None:
    leaked = [name for name in names if name in SECRETS or name.startswith("ONTO_WEB_")]
    assert leaked == []
    assert "PATH" in names
    assert "PYTHONIOENCODING" in names and "PYTHONDONTWRITEBYTECODE" in names


@pytest.fixture()
def manager(tmp_path: Path, monkeypatch) -> WorkspaceManager:
    assets = tmp_path / "assets"
    scripts = assets / "workspace-toolkit" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "query.py").write_text(DUMP_ENV.format(name="query"), encoding="utf-8")
    (scripts / "seed_example.py").write_text(DUMP_ENV.format(name="seed"), encoding="utf-8")
    (assets / "resource-template").mkdir()
    workspaces = WorkspaceManager(tmp_path / "data", assets)
    monkeypatch.setattr(projects, "workspaces", workspaces)
    monkeypatch.setattr(config, "MODELING_URL", "")
    return workspaces


def test_minimal_env_drops_backend_credentials(secret_env, monkeypatch):
    monkeypatch.setenv("ONTO_WEB_DATA_DIR", "/tmp/synthetic-data")
    env = minimal_env()
    assert_no_secrets(sorted(env))
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_seed_example_script_does_not_inherit_backend_env(manager, secret_env):
    meta = manager.create_project("project_alpha")
    manager.seed_example(meta["id"])
    root = manager.project_root(meta["id"])
    assert_no_secrets(json.loads((root / "research" / "env-seed.json").read_text()))


def test_reverse_query_script_does_not_inherit_backend_env(manager, secret_env, monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", False)
    meta = manager.create_project("project_alpha")
    response = TestClient(app).get(f"/api/projects/{meta['id']}/query", params={"kind": "orphans"})
    assert response.status_code == 200
    root = manager.project_root(meta["id"])
    assert_no_secrets(json.loads((root / "research" / "env-query.json").read_text()))


def test_jiuwen_hooks_do_not_inherit_backend_env(tmp_path, secret_env):
    hook = (
        "python3 -c 'import json, os; "
        "print(json.dumps({\"decision\": \"block\", \"reason\": \",\".join(sorted(os.environ))}))'"
    )
    entries = [(re.compile("^(?:Bash)$"), [hook])]
    reason = run_hooks_sync(entries, "bash", {"command": "ls"}, tmp_path)
    assert reason
    assert_no_secrets(reason.split(","))
