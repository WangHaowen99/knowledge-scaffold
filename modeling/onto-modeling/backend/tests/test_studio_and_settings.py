"""Studio 解析、MCP 配置校验与错误脱敏的单元测试。"""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from artifact_process_policy import child_environment
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


@pytest.mark.parametrize("value, expected", [
    ("", ""),
    ("https://eval.example.com:8090", "https://eval.example.com:8090/evaluation/"),
    ("https://eval.example.com/evaluation", "https://eval.example.com/evaluation/"),
    ("https://eval.example.com/evaluation/", "https://eval.example.com/evaluation/"),
])
def test_evaluation_url_is_normalized(value, expected):
    from app.services import evaluation_entry

    assert evaluation_entry.normalize(value) == expected


@pytest.mark.parametrize("value", [
    "/evaluation", "/evaluation/", "/other/", "sftp://eval.example.com/evaluation/", "https://eval.example.com/other/",
    "https://user@eval.example.com/evaluation/", "https://eval.example.com/evaluation/?token=x",
    "https://eval.example.com/evaluation/#token",
])
def test_evaluation_url_rejects_unsafe_values(value):
    from app.services import evaluation_entry

    with pytest.raises(ValueError):
        evaluation_entry.normalize(value)


def test_evaluation_entry_comes_only_from_deployment(monkeypatch):
    from app import config
    from app.routers import settings as settings_router

    monkeypatch.setattr(config, "EVAL_PUBLIC_URL", "")
    assert settings_router.get_evaluation_entry() == {"url": ""}
    monkeypatch.setattr(config, "EVAL_PUBLIC_URL", "https://eval.example.com:8090")
    assert settings_router.get_evaluation_entry() == {"url": "https://eval.example.com:8090/evaluation/"}
    monkeypatch.setattr(config, "EVAL_PUBLIC_URL", "javascript:alert(1)")
    assert settings_router.get_evaluation_entry().status_code == 500
    # 地址只由部署配置决定，没有页面修改入口
    paths = {(route.path, method) for route in settings_router.router.routes for method in route.methods}
    assert ("/api/settings/evaluation", "PUT") not in paths


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
    """配置页只存窗口与最大输出；压缩窗口按两者推算，一并转成 CLI 环境变量。"""
    from app import config
    from app.services import model_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    model_settings.save("https://gateway.example.com", "test-key-value", "some-model")

    saved = model_settings.save_context({"max_context_tokens": 200000, "max_output_tokens": 16000})
    assert saved["context"] == {"max_context_tokens": 200000, "max_output_tokens": 16000}
    env = model_settings.session_env()
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "200000"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "16000"
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "190000"
    assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" not in env
    # 网关配置不受影响
    assert env["ANTHROPIC_API_KEY"] == "test-key-value"

    # 全留空：按内置默认注入
    model_settings.save_context({})
    claude_env = {k: v for k, v in model_settings.session_env().items() if k.startswith("CLAUDE_")}
    assert claude_env == {
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "200000",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "180000",
    }


def test_model_context_defaults_when_unset(tmp_path, monkeypatch):
    """配置页与 .env 都没配时，窗口按 200K、最大输出按 32000 注入，配置页可读到默认值及来源。"""
    from app import config
    from app.services import model_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    saved = model_settings.save("https://gateway.example.com", "test-key-value", "some-model")

    assert saved["context"] == {"max_context_tokens": None, "max_output_tokens": None}
    assert saved["context_defaults"]["max_context_tokens"] == {
        "value": 200000,
        "source": "default",
        "env": "ONTO_WEB_CONTEXT_WINDOW",
    }
    assert saved["context_defaults"]["max_output_tokens"] == {
        "value": 32000,
        "source": "default",
        "env": "ONTO_WEB_CONTEXT_MAX_OUTPUT_TOKENS",
    }
    assert saved["context_defaults"]["auto_compact_window"]["value"] is None
    env = model_settings.session_env()
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "200000"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "32000"
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "180000"

    # 显式填写的窗口优先于默认值，压缩窗口随之重新推算
    model_settings.save_context({"max_context_tokens": 128000})
    env = model_settings.session_env()
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "128000"
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "110000"


def test_model_context_derives_pct_below_compact_window_minimum(tmp_path, monkeypatch):
    """所需压缩窗口低于 CLI 下限 100000 时改推触发百分比。"""
    from app import config
    from app.services import model_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    model_settings.save_context({"max_context_tokens": 128000, "max_output_tokens": 65536})
    env = model_settings.session_env()
    assert env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "45"
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in env


@pytest.mark.parametrize(
    "model, expected",
    [
        ("gateway-model-x", True),
        ("some-gateway-model", True),
        ("claude-sonnet-4-5", False),
        ("Claude-Opus-4-1[1m]", False),
        ("sonnet", False),
        ("opus[1m]", False),
        ("", False),
    ],
)
def test_cli_uses_env_window_only_for_models_cli_does_not_know(model, expected):
    from app.services import model_settings

    assert model_settings.cli_uses_env_window(model) is expected


def test_cli_context_limits_match_cli_report():
    """实测：窗口 200000、压缩窗口 180000、最大输出 32000 时 CLI 报告触发点 147000。

    CLI 报告的 rawMaxTokens 是 180000（受压缩窗口限制），用量环的分母取模型窗口 200000。
    """
    from app.services import model_settings

    env = {
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "200000",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "180000",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000",
    }
    assert model_settings.cli_context_limits(env) == (200000, 147000)
    # 没有压缩窗口：按模型窗口；最大输出未给按 CLI 默认 32000（预留封顶 20000）
    assert model_settings.cli_context_limits({"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "200000"}) == (200000, 167000)
    # 触发百分比只会让压缩提前：(128000 − 20000) × 45% = 48600
    env = {
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "128000",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "65536",
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "45",
    }
    assert model_settings.cli_context_limits(env) == (128000, 48600)
    assert model_settings.cli_context_limits(
        {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "200000", "DISABLE_AUTO_COMPACT": "1"}
    ) == (200000, None)
    assert model_settings.cli_context_limits({}) == (None, None)


def test_model_context_env_fallback_below_settings_page(tmp_path, monkeypatch):
    """优先级：配置页 > .env > 内置默认；.env 名与引擎无关，按项回落。"""
    from app import config
    from app.services import model_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("ONTO_WEB_CONTEXT_WINDOW", "128000")
    monkeypatch.setenv("ONTO_WEB_CONTEXT_MAX_OUTPUT_TOKENS", "16000")

    defaults = model_settings.masked()["context_defaults"]
    assert defaults["max_context_tokens"]["value"] == 128000
    assert defaults["max_context_tokens"]["source"] == "env"
    assert defaults["max_output_tokens"]["source"] == "env"
    env = model_settings.session_env()
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "128000"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "16000"
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "120000"

    model_settings.save_context({"max_context_tokens": 256000})
    env = model_settings.session_env()
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "256000"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "16000"

    # 留空项按 .env 值参与校验：最大输出 16000 不小于窗口 16000
    with pytest.raises(model_settings.ModelConfigError, match="最大输出留空时"):
        model_settings.save_context({"max_context_tokens": 16000})


def test_model_context_env_compaction_disables_derivation(tmp_path, monkeypatch):
    """.env 配了任一压缩参数就完全按 .env，不再推算压缩窗口。"""
    from app import config
    from app.services import model_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("ONTO_WEB_CONTEXT_COMPACT_PCT", "80")

    env = model_settings.session_env()
    assert env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "80"
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in env
    assert model_settings.context_defaults()["autocompact_pct"] == {
        "value": 80,
        "source": "env",
        "env": "ONTO_WEB_CONTEXT_COMPACT_PCT",
    }


def test_model_context_ignores_legacy_saved_compaction(tmp_path, monkeypatch):
    """旧版本在配置页存过的压缩窗口与触发百分比不再读取，压缩参数照常推算。"""
    import json

    from app import config
    from app.services import model_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "model-settings.json").write_text(
        json.dumps(
            {
                "context": {
                    "max_context_tokens": 200000,
                    "max_output_tokens": 16000,
                    "auto_compact_window": 150000,
                    "autocompact_pct": 60,
                }
            }
        ),
        encoding="utf-8",
    )

    assert model_settings.context_config() == {"max_context_tokens": 200000, "max_output_tokens": 16000}
    env = model_settings.session_env()
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "190000"
    assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" not in env


def test_model_context_env_invalid_values_ignored(tmp_path, monkeypatch):
    from app import config
    from app.services import model_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("ONTO_WEB_CONTEXT_WINDOW", "not-a-number")
    monkeypatch.setenv("ONTO_WEB_CONTEXT_COMPACT_PCT", "150")

    defaults = model_settings.context_defaults()
    assert defaults["max_context_tokens"]["value"] == 200000
    assert defaults["max_context_tokens"]["source"] == "default"
    assert defaults["autocompact_pct"]["value"] is None
    env = model_settings.session_env()
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "200000"
    assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" not in env


def test_model_context_reads_legacy_section_and_migrates_on_save(tmp_path, monkeypatch):
    import json

    from app import config
    from app.services import model_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    path = tmp_path / "model-settings.json"
    path.write_text(json.dumps({"claude_context": {"max_context_tokens": 128000}}), encoding="utf-8")

    assert model_settings.context_config()["max_context_tokens"] == 128000

    model_settings.save_context({"max_context_tokens": 64000})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "claude_context" not in data
    assert data["context"] == {"max_context_tokens": 64000}


@pytest.mark.parametrize(
    "values",
    [
        {"max_output_tokens": True},
        {"max_context_tokens": "200000"},
        {"max_context_tokens": 999},
        {"max_context_tokens": 10_000_001},
        {"max_output_tokens": 999},
        {"max_context_tokens": 200000, "max_output_tokens": 200000},
        {"max_output_tokens": 200000},  # 未填窗口时按默认 200K 校验
        {"max_context_tokens": 32000},  # 未填最大输出时按默认 32000 校验
    ],
)
def test_model_context_settings_reject_invalid(tmp_path, monkeypatch, values):
    from app import config
    from app.services import model_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    with pytest.raises(model_settings.ModelConfigError):
        model_settings.save_context(values)
    assert model_settings.context_config() == {"max_context_tokens": None, "max_output_tokens": None}


def test_model_settings_save_with_invalid_context_saves_nothing(tmp_path, monkeypatch):
    """网关配置与上下文参数一起保存：上下文不合法时整体拒绝，网关配置也不写。"""
    from types import SimpleNamespace

    from app import config
    from app.routers import settings as settings_router
    from app.services import model_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    request = SimpleNamespace(state=SimpleNamespace(user={"role": "admin"}))
    body = settings_router.ClaudeModelBody(
        base_url="https://gateway.example.com",
        model="some-model",
        context=settings_router.ContextBody(max_context_tokens=128000, max_output_tokens=128000),
    )
    response = settings_router.put_model_settings(body, request)
    assert response.status_code == 400
    assert model_settings.load() == {}

    body.context.max_output_tokens = 16000
    saved = settings_router.put_model_settings(body, request)
    assert saved["model"] == "some-model"
    assert saved["context"] == {"max_context_tokens": 128000, "max_output_tokens": 16000}


def test_model_context_route_requires_admin(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app import config
    from app.routers import settings as settings_router
    from app.services import auth, model_settings
    from app.services.auth import AuthError

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(auth, "ENABLED", True)
    body = settings_router.ContextBody(max_context_tokens=128000)

    modeler = SimpleNamespace(state=SimpleNamespace(user={"role": "modeler"}))
    with pytest.raises(AuthError) as exc:
        settings_router.put_model_context_settings(body, modeler)
    assert exc.value.status == 403
    assert model_settings.context_config()["max_context_tokens"] is None

    admin = SimpleNamespace(state=SimpleNamespace(user={"role": "admin"}))
    saved = settings_router.put_model_context_settings(body, admin)
    assert saved["context"]["max_context_tokens"] == 128000


def test_jiuwen_context_settings_saved_separately(tmp_path, monkeypatch):
    """内测引擎的窗口与最大输出单独一组；留空时最大输出按 ONTO_WEB_JIUWEN_MAX_TOKENS。"""
    from app import config
    from app.services import model_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "JIUWEN_MAX_TOKENS", 8192)

    jiuwen = model_settings.masked()["jiuwen"]
    assert jiuwen["context"] == {"max_context_tokens": None, "max_output_tokens": None}
    assert jiuwen["context_defaults"]["max_context_tokens"]["value"] == 200000
    assert jiuwen["context_defaults"]["max_output_tokens"] == {
        "value": 8192,
        "source": "default",
        "env": "ONTO_WEB_CONTEXT_MAX_OUTPUT_TOKENS",
    }
    assert model_settings.jiuwen_effective_context() == {
        "max_context_tokens": 200000,
        "max_output_tokens": 8192,
        "trigger_ratio": 0.8,
    }

    saved = model_settings.save_jiuwen(
        "https://api.example.com/v1",
        "test-key-value",
        "some-model",
        {"max_context_tokens": 128000, "max_output_tokens": 16384},
    )
    assert saved["jiuwen"]["context"] == {"max_context_tokens": 128000, "max_output_tokens": 16384}
    assert model_settings.jiuwen_effective_context()["trigger_ratio"] == 0.77
    # 稳定引擎的上下文参数不受影响
    assert saved["context"] == {"max_context_tokens": None, "max_output_tokens": None}

    # 不带 context 只改网关配置，已存的上下文参数保留
    saved = model_settings.save_jiuwen("https://api.example.com/v1", "", "other-model")
    assert saved["jiuwen"]["context"]["max_context_tokens"] == 128000

    # .env 的最大输出优先于内测引擎的内置默认
    monkeypatch.setenv("ONTO_WEB_CONTEXT_MAX_OUTPUT_TOKENS", "16000")
    assert model_settings.jiuwen_context_defaults()["max_output_tokens"]["value"] == 16000


@pytest.mark.parametrize(
    ("window", "max_output", "ratio"),
    [
        (200000, 8192, 0.8),  # 推算 0.86 晚于 SDK 默认，取 0.8
        (128000, 16384, 0.77),
        (32000, 16000, 0.4),
        (32000, 24000, 0.3),  # 留不出 10% 余量时不早于 0.3
    ],
)
def test_jiuwen_trigger_ratio_keeps_margin(window, max_output, ratio):
    from app.services import model_settings

    assert model_settings.jiuwen_trigger_ratio(window, max_output) == ratio


def test_jiuwen_route_rejects_invalid_context_and_saves_nothing(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app import config
    from app.routers import settings as settings_router
    from app.services import model_settings

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "JIUWEN_MAX_TOKENS", 8192)
    request = SimpleNamespace(state=SimpleNamespace(user={"role": "admin"}))
    body = settings_router.JiuwenModelBody(
        base_url="https://api.example.com/v1",
        model="some-model",
        context=settings_router.ContextBody(max_context_tokens=8000),
    )
    response = settings_router.put_jiuwen_model_settings(body, request)
    assert response.status_code == 400
    assert "8192" in response.body.decode("utf-8")  # 留空的最大输出按内测引擎默认校验
    assert model_settings.load() == {}

    body.context.max_output_tokens = 4000
    saved = settings_router.put_jiuwen_model_settings(body, request)
    assert saved["jiuwen"]["context"] == {"max_context_tokens": 8000, "max_output_tokens": 4000}


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



def test_studio_original_names_keep_encoded_links_and_ids(tmp_path):
    """Synthetic slash and literal-percent names must remain distinct navigation IDs."""
    import json
    from app.services.document_metadata import report_name

    entities = [
        ("operation", "synthetic_a%2Fb", "synthetic_a/b"),
        ("operation", "synthetic_a%252Fb", "synthetic_a%2Fb"),
        ("rule", "synthetic_rule%2Fdemo", "synthetic_rule/demo"),
    ]
    for kind, stem, name in entities:
        path = tmp_path / "ontology" / kind / f"{stem}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nreport_name: " + json.dumps(name) + "\n---\n"
                        + "# 业务描述\n\n[[synthetic_rule%2Fdemo]]\n"
                        + "# 逻辑描述\n\n1. synthetic step\n", encoding="utf-8")
    result = studio.graph(tmp_path)
    nodes = {node["id"]: node for node in result["nodes"]}
    for kind, stem, name in entities:
        assert nodes[stem]["label"] == name
        assert nodes[stem]["path"] == f"ontology/{kind}/{stem}.md"
    assert {(edge["source"], edge["target"]) for edge in result["edges"]} == {
        ("synthetic_a%2Fb", "synthetic_rule%2Fdemo"),
        ("synthetic_a%252Fb", "synthetic_rule%2Fdemo"),
    }
    entry = studio.element(tmp_path, "ontology/operation/synthetic_a%2Fb.md")
    assert entry["name"] == "synthetic_a%2Fb"
    assert entry["label"] == "synthetic_a/b"
    assert entry["links"] == ["synthetic_rule%2Fdemo"]
    assert "report_name" not in entry["intro"]
    edited = entry["name_frontmatter"] + studio.serialize_sections(entry["sections"])
    assert report_name(edited) == "synthetic_a/b"
    rule = studio.element(tmp_path, "ontology/rule/synthetic_rule%2Fdemo.md")
    assert {back["label"] for back in rule["backlinks"]} == {"synthetic_a/b", "synthetic_a%2Fb"}
    assert studio.operation_flow(tmp_path, "synthetic_a%2Fb")["operationId"] == "synthetic_a%2Fb"


@pytest.mark.parametrize("raw", [
    '---\nreport_name: "synthetic_a/b"\nreport_name: "synthetic_other"\n---\n',
    '---\nreport_name: invalid\n---\n',
    '---\nreport_name: "synthetic_a/b"\n',
])
def test_invalid_name_metadata_does_not_break_studio(tmp_path, raw):
    target = tmp_path / "ontology/operation/synthetic_plain.md"
    target.parent.mkdir(parents=True)
    target.write_text(raw + "# 业务描述\n\nsynthetic description\n", encoding="utf-8")
    assert studio.graph(tmp_path)["nodes"][0]["label"] == "synthetic_plain"
    assert studio.element(tmp_path, "ontology/operation/synthetic_plain.md")["label"] == "synthetic_plain"


def _studio_fixture_git(root: Path) -> tuple[str, dict[str, str]]:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("Git is required for Studio integration tests")
    executable = Path(executable).resolve(strict=True)
    if executable.is_relative_to(root.resolve()):
        raise ValueError("Fixture Git must be installed outside the temporary workspace")
    return str(executable), child_environment(root)


@pytest.fixture()
def studio_git_root(tmp_path: Path) -> Path:
    executable, env = _studio_fixture_git(tmp_path)
    commands = (
        ("init", "-q"),
        ("config", "user.name", "operator_a"),
        ("config", "user.email", "user@example.com"),
    )
    for command in commands:
        # 安全审查：固定初始化/合成身份参数、工作区外的 Git 绝对路径；仅操作
        # tmp_path，使用隔离环境，不经 shell，禁用宿主 Git hooks/配置并限制超时。
        subprocess.run(  # NOSONAR(S4721)
            [executable, *command],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
            shell=False,
            timeout=30,
        )
    return tmp_path


def _commit_at(root: Path, timestamp: int, message: str) -> None:
    if message not in {"first", "second", "rename", "rename again"}:
        raise ValueError("Commit message is outside the synthetic fixture allowlist")
    if not isinstance(timestamp, int) or timestamp <= 0:
        raise ValueError("Fixture timestamp must be a positive integer")
    executable, env = _studio_fixture_git(root)
    env.update(
        {
            "GIT_AUTHOR_DATE": f"{timestamp} +0000",
            "GIT_COMMITTER_DATE": f"{timestamp} +0000",
        }
    )
    # 安全审查：固定 add 参数只暂存临时合成仓库；隔离宿主配置与 hooks，
    # Git 使用工作区外绝对路径，argv 不经 shell，限制 30 秒超时。
    subprocess.run(  # NOSONAR(S4721)
        [executable, "add", "-A"],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        shell=False,
        timeout=30,
    )
    # 安全审查：提交消息是上述合成白名单，时间为已校验整数；其余参数固定，
    # 仅作用于临时仓库，隔离环境并关闭 shell，限制 30 秒超时。
    subprocess.run(  # NOSONAR(S4721)
        [executable, "commit", "-q", "-m", message],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        shell=False,
        timeout=30,
    )


def test_studio_updates_use_commit_time_and_worktree_mtime(studio_git_root: Path):
    tmp_path = studio_git_root
    object_dir = tmp_path / "ontology" / "object"
    rule_dir = tmp_path / "ontology" / "rule"
    object_dir.mkdir(parents=True)
    rule_dir.mkdir(parents=True)
    (object_dir / "订单.md").write_text("# 订单\n", encoding="utf-8")
    (object_dir / "synthetic_stable.md").write_text("# stable\n", encoding="utf-8")
    (rule_dir / "synthetic_rule.md").write_text("# rule v1\n", encoding="utf-8")
    _commit_at(tmp_path, 1_700_000_000, "first")
    (rule_dir / "synthetic_rule.md").write_text("# rule v2\n", encoding="utf-8")
    (object_dir / "订单.md").write_text("# 订单 v2\n", encoding="utf-8")
    _commit_at(tmp_path, 1_700_000_100, "second")

    # 已写入工作区、未提交：改动的文档和新建文档都取文件修改时间
    (rule_dir / "synthetic_rule.md").write_text("# rule v3\n", encoding="utf-8")
    os.utime(rule_dir / "synthetic_rule.md", (1_700_000_500, 1_700_000_500))
    (rule_dir / "synthetic_new.md").write_text("# new\n", encoding="utf-8")
    os.utime(rule_dir / "synthetic_new.md", (1_700_000_600, 1_700_000_600))

    assert studio.updates(tmp_path) == {
        "ontology/object/订单.md": 1_700_000_100,
        "ontology/object/synthetic_stable.md": 1_700_000_000,
        "ontology/rule/synthetic_rule.md": 1_700_000_500,
        "ontology/rule/synthetic_new.md": 1_700_000_600,
    }


def test_studio_updates_ignore_pure_renames(studio_git_root: Path):
    """纯改名（内容不变）沿用改名前的时间，改名同时改了内容才算更新。"""
    tmp_path = studio_git_root
    rule_dir = tmp_path / "ontology" / "rule"
    rule_dir.mkdir(parents=True)
    body = "# 规则描述\n合成规则，内容足够长以便识别重命名。\n"
    (rule_dir / "规则甲.md").write_text(body, encoding="utf-8")
    (rule_dir / "规则乙.md").write_text(body + "乙\n", encoding="utf-8")
    _commit_at(tmp_path, 1_700_000_000, "first")

    (rule_dir / "规则甲.md").rename(rule_dir / "规则丙.md")
    (rule_dir / "规则乙.md").rename(rule_dir / "规则丁.md")
    (rule_dir / "规则丁.md").write_text(body + "乙，改名时改了内容\n", encoding="utf-8")
    _commit_at(tmp_path, 1_700_000_100, "rename")

    # 再改名一次：连续两次纯改名也要追回最初的时间
    (rule_dir / "规则丙.md").rename(rule_dir / "规则戊.md")
    _commit_at(tmp_path, 1_700_000_200, "rename again")

    assert studio.updates(tmp_path) == {
        "ontology/rule/规则戊.md": 1_700_000_000,
        "ontology/rule/规则丁.md": 1_700_000_100,
    }


def test_studio_updates_empty_without_git(ontology_root: Path, monkeypatch):
    assert studio.updates(ontology_root) == {}

    (ontology_root / ".git").mkdir()

    def broken_git(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(studio.subprocess, "run", broken_git)
    assert studio.updates(ontology_root) == {}
