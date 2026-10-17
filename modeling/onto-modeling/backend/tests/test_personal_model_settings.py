"""个人模型配置：用户自己的 API Key 覆盖管理员配置。

重点守住两条：
- 没有任何个人配置（或未启用鉴权 / 管理员关闭）时，行为与改造前完全一致；
- 个人 Key 只给本人的会话轮次用，绝不回落或泄漏管理员 Key。

所有 Key、地址均为合成值。
"""

import asyncio
import json
from pathlib import Path

import pytest

from app import config
from app.services import agent_sessions, auth, model_settings, personal_model_settings
from app.services.agent_sessions import SessionError, SessionManager
from app.services.engines import jiuwen
from app.services.engines.base import CONFIG_ERROR, PERSONAL_CONFIG_ERROR, config_error_for
from app.services.engines.claude import ClaudeEngine, _session_env

ADMIN_URL = "https://admin-gateway.example.com"
ADMIN_KEY = "admin-key-synthetic-000000"
USER_URL = "https://user-gateway.example.net"
USER_KEY = "user-key-synthetic-111111"


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.delenv("ONTO_WEB_MODEL_SECRET_KEY", raising=False)
    return tmp_path


@pytest.fixture()
def auth_on(monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", True)


# ---- 管理员配置不受影响 ----


def test_without_personal_config_admin_env_is_unchanged(data_dir, auth_on):
    model_settings.save(ADMIN_URL, ADMIN_KEY, "admin-model")
    before = model_settings.session_env()

    assert personal_model_settings.model_owner("employee_0001", "claude") == ""
    assert model_settings.session_env("") == before
    assert before["ANTHROPIC_AUTH_TOKEN"] == ADMIN_KEY
    assert not (data_dir / "user-model-settings.json").exists()


def test_other_users_personal_key_does_not_change_admin_env(data_dir, auth_on):
    model_settings.save(ADMIN_URL, ADMIN_KEY, "admin-model")
    before = model_settings.session_env()
    personal_model_settings.save("employee_0001", "claude", "", USER_KEY, "")

    assert model_settings.session_env() == before
    assert personal_model_settings.model_owner("employee_0002", "claude") == ""
    # 管理员继续保存配置，不会碰到个人配置文件或开关
    model_settings.save(ADMIN_URL, "", "admin-model-2")
    assert model_settings.session_env()["ANTHROPIC_AUTH_TOKEN"] == ADMIN_KEY
    assert model_settings.masked()["allow_personal"] is True
    assert personal_model_settings.model_owner("employee_0001", "claude") == "employee_0001"


def test_personal_ignored_when_auth_disabled(data_dir, monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", True)
    personal_model_settings.save("employee_0001", "claude", "", USER_KEY, "")
    monkeypatch.setattr(auth, "ENABLED", False)

    assert personal_model_settings.model_owner("employee_0001", "claude") == ""
    with pytest.raises(personal_model_settings.PersonalConfigError):
        personal_model_settings.save("employee_0001", "claude", "", USER_KEY, "")


def test_admin_can_switch_personal_override_off(data_dir, auth_on):
    personal_model_settings.save("employee_0001", "claude", "", USER_KEY, "")
    model_settings.save_personal_policy(False)

    assert personal_model_settings.model_owner("employee_0001", "claude") == ""
    with pytest.raises(personal_model_settings.PersonalConfigError) as exc:
        personal_model_settings.save("employee_0001", "claude", "", USER_KEY, "")
    assert exc.value.status == 403

    model_settings.save_personal_policy(True)
    assert personal_model_settings.model_owner("employee_0001", "claude") == "employee_0001"


# ---- 个人配置的解析与安全约束 ----


def test_personal_key_inherits_admin_gateway_but_never_admin_key(data_dir, auth_on):
    model_settings.save(ADMIN_URL, ADMIN_KEY, "admin-model")
    personal_model_settings.save("employee_0001", "claude", "", USER_KEY, "")

    env = model_settings.session_env("employee_0001")
    assert env["ANTHROPIC_BASE_URL"] == ADMIN_URL
    assert env["ANTHROPIC_MODEL"] == "admin-model"
    assert env["ANTHROPIC_API_KEY"] == USER_KEY
    assert env["ANTHROPIC_AUTH_TOKEN"] == USER_KEY

    personal_model_settings.save("employee_0001", "claude", USER_URL, "", "user-model")
    env = model_settings.session_env("employee_0001")
    assert env["ANTHROPIC_BASE_URL"] == USER_URL
    assert env["ANTHROPIC_MODEL"] == "user-model"
    assert env["ANTHROPIC_AUTH_TOKEN"] == USER_KEY  # 留空 Key = 保持原个人 Key
    assert ADMIN_KEY not in json.dumps(env)


def test_personal_config_requires_own_key(data_dir, auth_on):
    model_settings.save(ADMIN_URL, ADMIN_KEY, "")
    # 只填地址不填 Key：会把管理员 Key 发往用户自填地址，必须拒绝
    with pytest.raises(personal_model_settings.PersonalConfigError):
        personal_model_settings.save("employee_0001", "claude", USER_URL, "", "")
    with pytest.raises(personal_model_settings.PersonalConfigError):
        personal_model_settings.save("employee_0001", "claude", "ftp://bad", USER_KEY, "")
    with pytest.raises(personal_model_settings.PersonalConfigError):
        personal_model_settings.save("employee_0001", "unknown", "", USER_KEY, "")


def test_personal_key_encrypted_at_rest_and_bound_to_user(data_dir, auth_on):
    personal_model_settings.save("employee_0001", "claude", "", USER_KEY, "")
    path = data_dir / "user-model-settings.json"
    raw = path.read_text(encoding="utf-8")
    assert USER_KEY not in raw
    assert path.stat().st_mode & 0o777 == 0o600
    assert (data_dir / ".model-secret-key").stat().st_mode & 0o777 == 0o600

    masked = personal_model_settings.masked("employee_0001")
    assert USER_KEY not in json.dumps(masked)
    assert masked["claude"]["active"] is True
    assert masked["jiuwen"]["active"] is False

    # 把密文挪给另一个用户：附加数据不匹配，解不开
    data = json.loads(raw)
    data["users"]["employee_0002"] = data["users"]["employee_0001"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(personal_model_settings.PersonalConfigError):
        personal_model_settings.resolve("employee_0002", "claude")


def test_explicit_master_key_is_used(data_dir, auth_on, monkeypatch):
    monkeypatch.setenv("ONTO_WEB_MODEL_SECRET_KEY", "x" * 40)
    personal_model_settings.save("employee_0001", "claude", "", USER_KEY, "")
    assert not (data_dir / ".model-secret-key").exists()
    assert personal_model_settings.resolve("employee_0001", "claude")["api_key"] == USER_KEY

    monkeypatch.setenv("ONTO_WEB_MODEL_SECRET_KEY", "y" * 40)
    with pytest.raises(personal_model_settings.PersonalConfigError):
        personal_model_settings.resolve("employee_0001", "claude")


def test_clear_and_delete_user_fall_back_to_admin(data_dir, auth_on):
    personal_model_settings.save("employee_0001", "claude", "", USER_KEY, "")
    personal_model_settings.save("employee_0001", "jiuwen", "", USER_KEY, "")

    personal_model_settings.clear("employee_0001", "claude")
    assert personal_model_settings.model_owner("employee_0001", "claude") == ""
    assert personal_model_settings.model_owner("employee_0001", "jiuwen") == "employee_0001"

    personal_model_settings.delete_user("employee_0001")
    assert personal_model_settings.model_owner("employee_0001", "jiuwen") == ""


def test_claude_session_env_owner_errors_are_not_swallowed(data_dir, auth_on):
    model_settings.save(ADMIN_URL, ADMIN_KEY, "")
    assert _session_env("")["ANTHROPIC_AUTH_TOKEN"] == ADMIN_KEY
    # 个人配置读取失败时必须报错，不能悄悄退回管理员或部署机的 Key
    with pytest.raises(personal_model_settings.PersonalConfigError):
        _session_env("employee_0001")


def test_jiuwen_model_config_personal_overlay(data_dir, auth_on, monkeypatch):
    monkeypatch.setattr(config, "JIUWEN_BASE_URL", "https://env.example.org/v1")
    monkeypatch.setattr(config, "JIUWEN_API_KEY", "env-key-synthetic")
    monkeypatch.setattr(config, "JIUWEN_MODEL", "env-model")
    admin_cfg = jiuwen.model_config()
    personal_model_settings.save("employee_0001", "jiuwen", "", USER_KEY, "user-model")

    assert jiuwen.model_config() == admin_cfg
    cfg = jiuwen.model_config("employee_0001")
    assert cfg == {
        "base_url": "https://env.example.org/v1",
        "api_key": USER_KEY,
        "model": "user-model",
    }


def test_config_error_message_by_owner():
    assert config_error_for("") == CONFIG_ERROR
    assert config_error_for("employee_0001") == PERSONAL_CONFIG_ERROR


# ---- 会话层：谁发消息用谁的配置 ----


class FakeClient:
    def __init__(self, options):
        self.options = options
        self.queries = []

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def query(self, text):
        self.queries.append(text)

    async def interrupt(self):
        pass

    async def receive_messages(self):
        if False:  # pragma: no cover
            yield
        await asyncio.Event().wait()


@pytest.fixture()
def pool(data_dir, auth_on, monkeypatch):
    monkeypatch.setattr(config, "MAX_ACTIVE_SESSIONS", 4)
    monkeypatch.setattr(agent_sessions, "SDK_AVAILABLE", True)
    created = []

    def create_client(self, options):
        client = FakeClient(options)
        created.append(client)
        return client

    monkeypatch.setattr(ClaudeEngine, "_create_client", create_client)
    model_settings.save(ADMIN_URL, ADMIN_KEY, "")
    return SessionManager(), created


async def _settle(manager, project_id):
    session = manager.get(project_id)
    for _ in range(20):
        if session.connect_task:
            await session.connect_task
        await asyncio.sleep(0)
        if session.client is not None:
            break
    return session


def test_members_without_personal_key_share_one_engine(pool, tmp_path):
    manager, created = pool

    async def scenario():
        await manager.send("p1", tmp_path, "一", username="employee_0001")
        session = await _settle(manager, "p1")
        session.state = "idle"
        await manager.send("p1", tmp_path, "二", username="employee_0002")
        assert len(created) == 1  # 都用管理员配置：不重连
        assert created[0].options.env["ANTHROPIC_AUTH_TOKEN"] == ADMIN_KEY
        assert created[0].options.settings is None
        assert created[0].queries == ["一", "二"]
        await manager.shutdown()

    asyncio.run(scenario())


def test_sender_personal_key_used_and_switch_back(pool, tmp_path):
    manager, created = pool
    personal_model_settings.save("employee_0001", "claude", "", USER_KEY, "")

    async def scenario():
        await manager.send("p1", tmp_path, "一", username="employee_0001")
        session = await _settle(manager, "p1")
        assert session.model_owner == "employee_0001"
        assert created[0].options.env["ANTHROPIC_AUTH_TOKEN"] == USER_KEY
        # 个人 Key 同时经 --settings 注入，盖过部署机 ~/.claude/settings.json 的 env
        flag_env = json.loads(created[0].options.settings)["env"]
        assert flag_env["ANTHROPIC_AUTH_TOKEN"] == USER_KEY
        assert all(name.startswith("ANTHROPIC_") for name in flag_env)

        # 本轮没结束，另一成员发消息：拒绝，不能让他的消息花 employee_0001 的 Key
        with pytest.raises(SessionError) as exc:
            await manager.send("p1", tmp_path, "插话", username="employee_0002")
        assert exc.value.status == 409
        assert created[0].queries == ["一"]

        session.state = "idle"
        await manager.send("p1", tmp_path, "二", username="employee_0002")
        session = await _settle(manager, "p1")
        assert session.model_owner == ""
        assert len(created) == 2
        assert created[1].options.env["ANTHROPIC_AUTH_TOKEN"] == ADMIN_KEY
        assert created[1].options.settings is None  # 管理员配置：原行为，不加 --settings
        assert created[1].queries == ["二"]
        await manager.shutdown()

    asyncio.run(scenario())


def test_send_with_undecryptable_personal_key_fails_fast(pool, tmp_path, monkeypatch):
    manager, created = pool
    monkeypatch.setenv("ONTO_WEB_MODEL_SECRET_KEY", "x" * 40)
    personal_model_settings.save("employee_0001", "claude", "", USER_KEY, "")
    monkeypatch.setenv("ONTO_WEB_MODEL_SECRET_KEY", "y" * 40)

    async def scenario():
        with pytest.raises(SessionError) as exc:
            await manager.send("p1", tmp_path, "一", username="employee_0001")
        assert exc.value.status == 409
        assert created == []
        await manager.shutdown()

    asyncio.run(scenario())


# ---- 接口 ----


@pytest.fixture()
def client(data_dir, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(auth, "ENABLED", True)
    users = {
        "admin-token": {"sub": "operator_admin", "role": "admin"},
        "user-token": {"sub": "employee_0001", "role": "user"},
    }
    monkeypatch.setattr(auth, "verify_access_token", lambda token: users.get(token))
    return TestClient(app)


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def test_personal_routes(client):
    # 普通用户仍然不能读写管理员配置
    assert client.get("/api/settings/model", headers=_h("user-token")).status_code == 403
    assert (
        client.put(
            "/api/settings/model/personal-policy", json={"allow": False}, headers=_h("user-token")
        ).status_code
        == 403
    )

    res = client.put(
        "/api/settings/model/me/claude",
        json={"base_url": "", "api_key": USER_KEY, "model": ""},
        headers=_h("user-token"),
    )
    assert res.status_code == 200
    assert res.json()["claude"]["active"] is True
    assert USER_KEY not in res.text

    mine = client.get("/api/settings/model/me", headers=_h("user-token")).json()
    assert mine["claude"]["api_key_masked"] == "已设置"
    # 管理员看自己的个人配置：互不可见
    theirs = client.get("/api/settings/model/me", headers=_h("admin-token")).json()
    assert theirs["claude"]["active"] is False

    res = client.put(
        "/api/settings/model/me/claude",
        json={"base_url": USER_URL, "api_key": "", "model": ""},
        headers=_h("admin-token"),
    )
    assert res.status_code == 400  # 没有自己的 Key 不能只改地址

    res = client.delete("/api/settings/model/me/claude", headers=_h("user-token"))
    assert res.json()["claude"]["active"] is False
