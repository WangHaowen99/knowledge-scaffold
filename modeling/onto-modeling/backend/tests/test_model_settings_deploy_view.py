"""SY-01：界面没配、只靠部署环境变量接模型时，模型接入页要能看出来（V 阶段：内测引擎只配了环境变量，页面显示「未配置」）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import config
from app.routers import settings


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "JIUWEN_BASE_URL", "")
    monkeypatch.setattr(config, "JIUWEN_API_KEY", "")
    monkeypatch.setattr(config, "JIUWEN_MODEL", "")
    return tmp_path


def test_jiuwen_from_env_is_reported_as_deploy_configured(data_dir, monkeypatch):
    monkeypatch.setattr(config, "JIUWEN_BASE_URL", "https://api.example.invalid/v1")
    monkeypatch.setattr(config, "JIUWEN_API_KEY", "sk-synthetic-0123456789abcdef")
    monkeypatch.setattr(config, "JIUWEN_MODEL", "model-synthetic")
    view = settings.model_settings_view()["engines"]
    assert view["jiuwen"]["configured"] is False
    assert view["jiuwen"]["deploy"] == {"configured": True, "model": "model-synthetic"}
    # 只给是否配置与模型名，网关地址和 Key 不出接口
    assert "example.invalid" not in str(view) and "0123456789" not in str(view)
    assert view["claude"]["deploy"] == {"configured": False, "model": ""}


def test_claude_from_env_is_reported_as_deploy_configured(data_dir, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-synthetic-0123456789abcdef")
    monkeypatch.setenv("ANTHROPIC_MODEL", "model-synthetic")
    view = settings.model_settings_view()["engines"]["claude"]
    assert view["configured"] is False
    assert view["deploy"] == {"configured": True, "model": "model-synthetic"}
