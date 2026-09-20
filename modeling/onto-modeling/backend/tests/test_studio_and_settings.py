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


def test_expire_orphan_permissions_broadcasts_expired():
    from app.services.agent_sessions import Session, _expire_orphan_permissions

    session = Session(id="t1", project_id="p1", client=None)
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
