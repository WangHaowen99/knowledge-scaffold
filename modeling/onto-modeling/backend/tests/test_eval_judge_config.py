"""Evaluation judge settings use synthetic users, endpoints and credentials only."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config
from app.services import auth, eval_judge_config, model_settings, modeling_eval, personal_model_settings


@pytest.fixture
def judge_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(model_settings, "load", lambda: {
        "base_url": "https://gateway.example.com",
        "api_key": "synthetic-admin-key",
        "model": "synthetic-admin-model",
        "jiuwen": {
            "base_url": "https://service.example.net/v1",
            "api_key": "synthetic-openai-admin-key",
            "model": "synthetic-openai-admin-model",
        },
    })
    sections = {
        "operator_a": {engine: {
            "base_url": "https://user.example.org/v1" if engine == "jiuwen" else "https://user.example.org",
            "model": f"synthetic-personal-{engine}",
            "api_key": "synthetic-personal-key",
        } for engine in personal_model_settings.ENGINES},
        "operator_b": {engine: {
            "base_url": "", "model": "", "api_key": "synthetic-second-key",
        } for engine in personal_model_settings.ENGINES},
    }
    monkeypatch.setattr(personal_model_settings, "_section", lambda user, engine: sections.get(user, {}).get(engine, {}))
    monkeypatch.setattr(personal_model_settings, "_decrypt", lambda value, user, engine: value)
    monkeypatch.setattr(model_settings, "effective_context", lambda: {
        "max_context_tokens": 32_000, "max_output_tokens": 4096,
    })
    monkeypatch.setattr(config, "JIUWEN_MAX_TOKENS", 8192)


@pytest.mark.parametrize("engine,api", [("claude", "anthropic-messages"), ("jiuwen", "openai-completions")])
def test_resolves_initiator_and_engine(judge_settings, engine, api):
    first = eval_judge_config.resolve("operator_a", engine)
    second = eval_judge_config.resolve("operator_b", engine)
    assert first["api"] == second["api"] == api
    assert first["model"] == f"synthetic-personal-{engine}"
    assert first["api_key"] == "synthetic-personal-key"
    assert second["api_key"] == "synthetic-second-key"
    assert second["model"] == ("synthetic-admin-model" if engine == "claude" else "synthetic-openai-admin-model")


@pytest.mark.parametrize("engine", ["claude", "jiuwen"])
def test_uses_onto_admin_when_personal_override_disabled(judge_settings, monkeypatch, engine):
    monkeypatch.setattr(personal_model_settings, "allowed", lambda: False)
    resolved = eval_judge_config.resolve("operator_a", engine)
    assert resolved["api_key"] == ("synthetic-admin-key" if engine == "claude" else "synthetic-openai-admin-key")


def test_missing_onto_configuration_is_an_explicit_error(judge_settings, monkeypatch):
    monkeypatch.setattr(model_settings, "load", lambda: {})
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(eval_judge_config.JudgeConfigError, match="我的模型配置"):
        eval_judge_config.resolve("operator_c", "claude")


def test_personal_decryption_failure_does_not_use_admin(judge_settings, monkeypatch):
    def broken(*args):
        raise personal_model_settings.PersonalConfigError("个人 API Key 无法解密", 409)
    monkeypatch.setattr(personal_model_settings, "_decrypt", broken)
    with pytest.raises(personal_model_settings.PersonalConfigError):
        eval_judge_config.resolve("operator_a", "claude")


def test_route_resolves_current_user_and_only_forwards_server_settings(tmp_path, monkeypatch):
    from app.routers import projects

    root = tmp_path / "workspace_demo"
    root.mkdir()
    monkeypatch.setattr(projects, "ensure_access", lambda *args, **kwargs: root)
    monkeypatch.setattr(projects.workspaces, "get_project", lambda pid: {"engine": "jiuwen"})
    monkeypatch.setattr(config, "MODELING_TOKEN", "synthetic-service-token")
    monkeypatch.setattr(modeling_eval, "evaluation_object_snapshot", lambda root: {})
    monkeypatch.setattr(modeling_eval, "enabled", lambda: True)
    monkeypatch.setattr(modeling_eval, "reachable", lambda: True)
    settings = {"model": "synthetic-personal-model", "api_key": "synthetic-personal-key"}
    selected = []
    def resolve(user, engine):
        selected.append((user, engine))
        return settings
    monkeypatch.setattr(eval_judge_config, "resolve", resolve)
    captured = []
    async def run(*args, **kwargs):
        captured.append(kwargs)
    monkeypatch.setattr(modeling_eval, "run", run)
    request = SimpleNamespace(state=SimpleNamespace(user={"sub": "operator_a"}), headers={})
    body = projects.EvalRunBody(
        cases=[{"question": "Q1", "groundTruth": "G1"}], allow_unbound=True,
        judge_model={"model": "untrusted-browser-model"},
    )
    async def call():
        result = await projects.eval_run("project_alpha", body, request)
        await asyncio.sleep(0)
        return result
    result = asyncio.run(call())
    assert result["started"] is True
    assert selected == [("operator_a", "jiuwen")]
    assert captured[0]["judge_model"] is settings
    assert "synthetic-personal-key" not in json.dumps(result)
    assert list(root.iterdir()) == []


def test_route_rejects_missing_judge_before_starting_background_task(tmp_path, monkeypatch):
    from app.routers import projects

    monkeypatch.setattr(projects, "ensure_access", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(projects.workspaces, "get_project", lambda pid: {"engine": "claude"})
    monkeypatch.setattr(config, "MODELING_TOKEN", "synthetic-service-token")
    monkeypatch.setattr(modeling_eval, "evaluation_object_snapshot", lambda root: {})
    monkeypatch.setattr(modeling_eval, "enabled", lambda: True)
    monkeypatch.setattr(modeling_eval, "reachable", lambda: True)
    def missing(*args):
        raise eval_judge_config.JudgeConfigError("请检查我的模型配置")
    monkeypatch.setattr(eval_judge_config, "resolve", missing)
    monkeypatch.setattr(modeling_eval, "run", lambda *args, **kwargs: pytest.fail("must not start"))
    body = projects.EvalRunBody(cases=[{"question": "Q1", "groundTruth": "G1"}], allow_unbound=True)
    request = SimpleNamespace(state=SimpleNamespace(user={"sub": "operator_a"}), headers={})
    result = asyncio.run(projects.eval_run("project_alpha", body, request))
    assert result.status_code == 400
    assert "我的模型配置" in json.loads(result.body)["error"]


def test_route_does_not_require_judge_for_cases_without_reference(tmp_path, monkeypatch):
    from app.routers import projects

    monkeypatch.setattr(projects, "ensure_access", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(config, "MODELING_TOKEN", "synthetic-service-token")
    monkeypatch.setattr(modeling_eval, "evaluation_object_snapshot", lambda root: {})
    monkeypatch.setattr(modeling_eval, "enabled", lambda: True)
    monkeypatch.setattr(modeling_eval, "reachable", lambda: True)
    def unexpected(*args):
        raise AssertionError("no judge needed")
    monkeypatch.setattr(eval_judge_config, "resolve", unexpected)
    async def run(*args, **kwargs):
        assert "judge_model" not in kwargs
    monkeypatch.setattr(modeling_eval, "run", run)
    request = SimpleNamespace(state=SimpleNamespace(user=None), headers={})
    body = projects.EvalRunBody(cases=[{"question": "Q1"}], allow_unbound=True)
    async def call():
        result = await projects.eval_run("project_alpha", body, request)
        await asyncio.sleep(0)
        return result
    assert asyncio.run(call())["started"] is True


def test_judge_configuration_only_appears_in_evaluate_request(tmp_path, monkeypatch):
    import tempfile
    from app.services.workspace import WorkspaceManager

    root = tmp_path / "workspace_demo"
    root.mkdir()
    package_dir = tmp_path / "package_demo"
    package_dir.mkdir()
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **kwargs: str(package_dir))
    settings = {"api_key": "synthetic-private-judge-key", "model": "synthetic-judge-model"}
    monkeypatch.setattr(modeling_eval, "evaluation_object_snapshot", lambda root: {})
    monkeypatch.setattr(modeling_eval.package_mapping, "resolve_object_tables", lambda *args, **kwargs: {"issues": []})
    monkeypatch.setattr(WorkspaceManager, "export_package_zip", lambda self, pid, target, **kwargs: target.write_bytes(b"synthetic package"))
    monkeypatch.setattr(modeling_eval, "_poll_round", lambda *args: None)
    # Automatic reuse would keep the previous user's judge credentials.
    monkeypatch.setattr(modeling_eval, "_try_resume", lambda *args, **kwargs: pytest.fail("must use new user configuration"))
    calls = []
    def http(method, path, token, body=None, **kwargs):
        calls.append((path, body))
        if path == "/api/package-workspaces/import":
            return {"project": {"id": "project_alpha"}}
        if path.endswith("/evaluate"):
            return {"roundId": "round_demo"}
        return {}
    monkeypatch.setattr(modeling_eval, "_http", http)
    modeling_eval._run_sync("project_alpha", root, "synthetic-access", [{"question": "Q1", "groundTruth": "G1"}], judge_model=settings)
    assert [body["judge_model"] for path, body in calls if path.endswith("/evaluate")] == [settings]
    assert all("judge_model" not in (body or {}) for path, body in calls if not path.endswith("/evaluate"))
    state = modeling_eval.read_state(root)
    assert state["judge_source"] == "onto-modeling"
    assert state.get("status") != "failed", state
    assert "synthetic-private-judge-key" not in json.dumps(state)
