"""Studio 解析、MCP 配置校验与错误脱敏的单元测试。"""

from pathlib import Path

import pytest

from app.services import studio
from app.services.agent_sessions import _looks_like_raw_error
from app.services.mcp_settings import McpConfigError, validate


@pytest.fixture()
def ontology_root(tmp_path: Path) -> Path:
    obj_dir = tmp_path / "ontology" / "object"
    logic_dir = tmp_path / "ontology" / "logic"
    obj_dir.mkdir(parents=True)
    logic_dir.mkdir(parents=True)
    (obj_dir / "订单.md").write_text(
        "# 订单\n\n## 属性\n\n编号、金额\n\n## 与logic的关联\n\n使用logic：[[创建订单]]\n",
        encoding="utf-8",
    )
    (logic_dir / "创建订单.md").write_text(
        "# 创建订单\n\n## 逻辑描述\n\n生成[[订单]]并调用[[不存在的对象]]\n",
        encoding="utf-8",
    )
    return tmp_path


def test_studio_graph_links_and_degrees(ontology_root: Path):
    result = studio.graph(ontology_root)
    names = {n["id"]: n for n in result["nodes"]}
    assert set(names) == {"订单", "创建订单"}
    assert names["订单"]["kind"] == "object"
    # 指向不存在文档的链接不产生边
    assert {(e["source"], e["target"]) for e in result["edges"]} == {
        ("订单", "创建订单"),
        ("创建订单", "订单"),
    }
    assert names["订单"]["out"] == 1 and names["订单"]["in"] == 1


def test_studio_element_sections_and_backlinks(ontology_root: Path):
    element = studio.element(ontology_root, "ontology/object/订单.md")
    # 旧章节名「与logic的关联」读取时归一为新术语（ADR-0004）
    assert [s["title"] for s in element["sections"]] == ["属性", "与operation的关联"]
    assert element["links"] == ["创建订单"]
    assert [b["name"] for b in element["backlinks"]] == ["创建订单"]


def test_mcp_validate():
    ok = validate({"name": "srv-1", "transport": "http", "url": "https://mcp.example.com"})
    assert ok["enabled"] is True
    with pytest.raises(McpConfigError):
        validate({"name": "bad name", "transport": "http", "url": "https://x.example.com"})
    with pytest.raises(McpConfigError):
        validate({"name": "s", "transport": "http", "url": "ftp://x"})
    with pytest.raises(McpConfigError):
        validate({"name": "s", "transport": "stdio", "command": ""})


def test_raw_error_detection():
    assert _looks_like_raw_error("API Error: 400 thinking block must contain thinking")
    assert _looks_like_raw_error("Claude Code returned an error result")
    assert _looks_like_raw_error("(request id: 2026...)")
    assert not _looks_like_raw_error("订单对象提取完成，共 12 个字段。")


def test_project_visibility_rules():
    from app.services.workspace import WorkspaceManager

    projects = [
        {"id": "a", "owner": "alice"},
        {"id": "b", "owner": "bob"},
        {"id": "c", "owner": ""},  # 历史无主项目
    ]
    visible = WorkspaceManager.visible_to(projects, "alice", is_admin=False)
    assert {p["id"] for p in visible} == {"a", "c"}
    assert len(WorkspaceManager.visible_to(projects, "alice", is_admin=True)) == 3
    assert WorkspaceManager.can_access({"owner": "bob"}, "alice", False) is False
    assert WorkspaceManager.can_access({"owner": "bob"}, "alice", True) is True
    assert WorkspaceManager.can_access({"owner": ""}, "alice", False) is True


def test_coverage_metrics_parse(tmp_path: Path):
    from app.services import metrics

    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "20260918-trace.md").write_text(
        "# 追踪报告\n\n设计覆盖率: 83.3%\n推演通过率：90%\n", encoding="utf-8"
    )
    result = metrics.coverage(tmp_path)
    assert result["design_coverage"] == 83.3
    assert result["pass_rate"] == 90.0
    assert result["source"] == "reports/20260918-trace.md"

    empty = metrics.coverage(tmp_path / "nope")
    assert empty["design_coverage"] is None


def test_runtime_settings_permission_mode(tmp_path: Path, monkeypatch):
    from app import config
    from app.services import runtime_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert runtime_settings.permission_mode() == config.PERMISSION_MODE
    runtime_settings.save("default")
    assert runtime_settings.permission_mode() == "default"
    with pytest.raises(ValueError):
        runtime_settings.save("yolo")


def test_audit_log_roundtrip(tmp_path: Path, monkeypatch):
    from app import config
    from app.services import audit

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    audit.log("alice", "POST", "/api/projects")
    audit.log("bob", "DELETE", "/api/projects/x")
    entries = audit.read()
    assert entries[0]["user"] == "bob"  # 最新在前
    assert entries[1]["method"] == "POST"


def test_model_settings_roundtrip(tmp_path: Path, monkeypatch):
    from app import config
    from app.services import model_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    assert model_settings.masked()["configured"] is False

    saved = model_settings.save("https://gateway.example.com", "sk-test-1234567890abcd", "m1")
    assert saved["configured"] is True
    assert saved["api_key_masked"].startswith("sk-tes")
    assert "1234567890" not in saved["api_key_masked"]

    env = model_settings.session_env()
    assert env["ANTHROPIC_BASE_URL"] == "https://gateway.example.com"
    assert env["ANTHROPIC_API_KEY"] == "sk-test-1234567890abcd"
    assert env["ANTHROPIC_MODEL"] == "m1"

    # 留空 key 不覆盖既有值；非法地址被拒
    model_settings.save("https://gateway.example.com", "", "m2")
    assert model_settings.session_env()["ANTHROPIC_API_KEY"] == "sk-test-1234567890abcd"
    with pytest.raises(model_settings.ModelConfigError):
        model_settings.save("ftp://bad", "", "")


def test_default_engine_roundtrip_and_fallback(tmp_path: Path, monkeypatch):
    """默认引擎可设可读；未知引擎被拒，设过但部署不可用时回落内置默认。"""
    from app import config
    from app.services import model_settings
    from app.services import engines

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(engines, "available_engines", lambda: {"claude": True, "jiuwen": True})

    assert model_settings.default_engine() == "claude"
    assert model_settings.masked()["default_engine"] == "claude"

    saved = model_settings.save_default_engine("jiuwen")
    assert saved["default_engine"] == "jiuwen"
    assert model_settings.default_engine() == "jiuwen"
    # Key 等既有配置不被覆盖
    assert saved["api_key_masked"] == ""

    with pytest.raises(model_settings.ModelConfigError):
        model_settings.save_default_engine("not-an-engine")

    monkeypatch.setattr(engines, "available_engines", lambda: {"claude": True, "jiuwen": False})
    assert model_settings.default_engine() == "claude"
    with pytest.raises(model_settings.ModelConfigError):
        model_settings.save_default_engine("jiuwen")


def test_model_settings_session_env_overrides_both_auth_vars(tmp_path, monkeypatch):
    """UI 配置的 key 必须同时覆盖 API_KEY 与 AUTH_TOKEN，防旧令牌残留生效。"""
    from app import config
    from app.services import model_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    model_settings.save("https://gateway.example.com", "test-key-value", "some-model")
    env = model_settings.session_env()
    assert env["ANTHROPIC_API_KEY"] == "test-key-value"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "test-key-value"
    assert env["ANTHROPIC_BASE_URL"] == "https://gateway.example.com"
    assert env["ANTHROPIC_MODEL"] == "some-model"


def test_model_context_settings_inject_cli_env(tmp_path, monkeypatch):
    """上下文与压缩参数转成 CLI 环境变量；未设置的不注入（真实窗口除外）。"""
    from app import config
    from app.services import model_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    model_settings.save("https://gateway.example.com", "test-key-value", "some-model")

    saved = model_settings.save_context(
        {"max_context_tokens": 1000000, "auto_compact_window": 800000, "max_output_tokens": 16000}
    )
    assert saved["context"] == {
        "max_context_tokens": 1000000,
        "auto_compact_window": 800000,
        "max_output_tokens": 16000,
        "autocompact_pct": None,
    }
    env = model_settings.session_env()
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "1000000"
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "800000"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "16000"
    assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" not in env
    # 网关配置不受影响
    assert env["ANTHROPIC_API_KEY"] == "test-key-value"

    model_settings.save_context({})
    claude_env = {k: v for k, v in model_settings.session_env().items() if k.startswith("CLAUDE_")}
    assert claude_env == {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000"}


def test_model_context_window_defaults_to_1m_when_unset(tmp_path, monkeypatch):
    """只配了模型、没配上下文参数时，真实窗口按 1M 注入，配置页可读到默认值。"""
    from app import config
    from app.services import model_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    saved = model_settings.save("https://gateway.example.com", "test-key-value", "some-model")

    assert saved["context"]["max_context_tokens"] is None
    assert saved["context_defaults"] == {"max_context_tokens": 1000000}
    env = model_settings.session_env()
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "1000000"
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in env
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in env

    # 显式填写的真实窗口优先于默认值
    model_settings.save_context({"max_context_tokens": 128000})
    assert model_settings.session_env()["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "128000"


@pytest.mark.parametrize(
    "values",
    [
        {"auto_compact_window": 50000},  # 低于 CLI 接受的 100k
        {"autocompact_pct": 0},
        {"autocompact_pct": 101},
        {"max_output_tokens": True},
        {"max_context_tokens": 800000, "auto_compact_window": 1000000},
        {"max_context_tokens": 1000000, "max_output_tokens": 1000000},
        {"max_output_tokens": 1000000},  # 未填真实窗口时按默认 1M 校验
    ],
)
def test_model_context_settings_reject_invalid(tmp_path, monkeypatch, values):
    from app import config
    from app.services import model_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    with pytest.raises(model_settings.ModelConfigError):
        model_settings.save_context(values)
    assert model_settings.context_config()["autocompact_pct"] is None


def test_model_context_route_requires_admin(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app import config
    from app.routers import settings as settings_router
    from app.services import auth, model_settings
    from app.services.auth import AuthError

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(auth, "ENABLED", True)
    body = settings_router.ContextBody(autocompact_pct=80)

    modeler = SimpleNamespace(state=SimpleNamespace(user={"role": "modeler"}))
    with pytest.raises(AuthError) as exc:
        settings_router.put_model_context_settings(body, modeler)
    assert exc.value.status == 403
    assert model_settings.context_config()["autocompact_pct"] is None

    admin = SimpleNamespace(state=SimpleNamespace(user={"role": "admin"}))
    saved = settings_router.put_model_context_settings(body, admin)
    assert saved["context"]["autocompact_pct"] == 80


def test_expire_orphan_permissions_broadcasts_expired():
    from app.services.agent_sessions import Session, _expire_orphan_permissions

    session = Session(id="t1", project_id="p1")
    session.events = [
        {"type": "permission_request", "request_id": "a", "seq": 0},
        {"type": "permission_request", "request_id": "b", "seq": 1},
        {"type": "permission_resolved", "request_id": "a", "behavior": "allow", "seq": 2},
    ]
    _expire_orphan_permissions(session)
    expired = [
        e for e in session.events
        if e.get("type") == "permission_resolved" and e.get("behavior") == "expired"
    ]
    assert [e["request_id"] for e in expired] == ["b"]


def test_config_error_translation():
    from app.services.agent_sessions import (
        _looks_like_config_error,
        _looks_like_raw_error,
    )

    assert _looks_like_config_error("Not logged in · Please run /login")
    assert _looks_like_config_error("API Error: 401 Unauthorized authentication_error")
    assert not _looks_like_config_error("模型输出正常文本")
    # 未登录也必须先被识别为原始错误才会进入替换分支
    assert _looks_like_raw_error("Not logged in · Please run /login")
