"""证据源的接口占位（ADR-0056，F13 起大部分已实现，ADR-0082；凭据 EV-06、EV-07 在 F16 开放，ADR-0086）：PL-04、PL-19 仍回 501；
契约 3.19 的每个接口都登记了路由；助手拿不到 evidence.* MCP 工具；内核 SPI 只有协议。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.evidence import gateway_client, registry, spi
from app.main import app
from app.routers import evidence
from app.services import auth
from app.services.packs import builtin
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import make_assets

CONTRACT = Path(__file__).resolve().parents[1].parent / "docs" / "api-v1.md"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    manager = WorkspaceManager(tmp_path / "data", make_assets(tmp_path))
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    client = TestClient(app)
    project_id = client.post("/api/v1/projects", json={"name": "证据", "goal": "合成场景：图书借阅建模"}).json()["id"]
    yield client, project_id
    builtin.clear_cache()


def _concrete(path: str, project_id: str) -> str:
    return re.sub(r"\{[a-z_]+\}", lambda m: project_id if m.group(0) == "{project_id}" else "x-1", path)


def test_every_reserved_route_returns_501_with_contract_envelope(env):
    client, project_id = env
    seen = set()
    for api, method, path in evidence.ROUTES:
        response = client.request(method, _concrete(path, project_id), json={})
        body = response.json()
        assert response.status_code == 501, (api, method, path, response.text)
        assert body["code"] == "evidence_not_available" and body["details"] == {"api": api}
        assert set(body) == {"code", "message", "details", "trace_id"}
        assert body["trace_id"] == response.headers["x-trace-id"]
        seen.add(api)
    # F16 起证据源接口全部开放（见 test_f13_*、test_f16_kernel_*），只剩提供方安装
    assert seen == {"PL-04", "PL-19"}
    assert {item[0] for item in evidence.IMPLEMENTED} == {f"EV-{n:02d}" for n in range(1, 35)}


def test_fixed_segment_wins_over_path_parameter(env, monkeypatch):
    client, project_id = env
    monkeypatch.setattr(evidence, "_pack_enabled", lambda root: True)
    diff = client.get(f"/api/v1/projects/{project_id}/evidence/snapshots/diff").json()
    one = client.get(f"/api/v1/projects/{project_id}/evidence/snapshots/s-1").json()
    assert diff["code"] == "invalid_argument" and set(diff["details"]["fields"]) == {"from", "to"}
    assert one["code"] == "snapshot_not_found"


@pytest.mark.skipif(not CONTRACT.is_file(), reason="契约副本不在仓库里")
def test_routes_cover_contract_section_3_19():
    """契约 3.19 每个接口块的方法与路径都登记了（路径参数名不比）。"""
    text = CONTRACT.read_text(encoding="utf-8")
    section = text.split("### 3.19 证据源", 1)[1].split("### 3.20", 1)[0]
    declared = set()
    for line in section.splitlines():
        for method, path in re.findall(r"`(GET|POST|PUT|PATCH|DELETE) (/api/v1/[^`]+)`", line):
            declared.add((method, re.sub(r"\{[^}]+\}", "{}", path)))
        for suffix in re.findall(r"`\.\.\./(\w+)`", line):  # EV-17 的 `.../resume`、`.../retry`
            declared.add(("POST", f"/api/v1/projects/{{}}/evidence/jobs/{{}}/{suffix}"))
    registered = {(method, re.sub(r"\{[^}]+\}", "{}", path))
                  for _api, method, path, *_handler in (*evidence.ROUTES, *evidence.IMPLEMENTED)}
    assert declared and declared <= registered, declared - registered


def test_assistant_tools_do_not_include_evidence():
    """MCP 工具目录与命令行都不注册 evidence.*：一律失败的工具只会白耗助手的轮次。"""
    from app.services.onto_mcp import definitions

    names = set(definitions.TOOLS)
    assert names and not any(name.startswith("evidence") for name in names)
    toolkit = Path(__file__).resolve().parents[2] / "assets" / "workspace-toolkit" / "scripts"
    assert not list(toolkit.glob("evidence*"))


def test_spi_is_protocol_only():
    assert getattr(spi.EvidenceProvider, "_is_protocol", False)
    assert getattr(gateway_client.ConnectorGatewayClient, "_is_protocol", False)
    assert getattr(registry.SourceRegistry, "_is_protocol", False)
    # 一次性票据的字段（详设 5.8.2）
    fields = set(gateway_client.Ticket.__dataclass_fields__)
    assert {"jti", "job_id", "source_id", "operation", "scope_hash", "budget", "expires_at", "single_use"} <= fields
    assert gateway_client.TICKET_TTL_S == 600
    # 提供方清单要声明的内容（详设 5.8.3）
    info = set(spi.ProviderInfo.__dataclass_fields__)
    assert {"evidence_types", "capabilities", "cred_fields", "readonly_method", "anchor_rule", "runtime", "license_note"} <= info
    # 登记内容（详设 5.8.7）：只有凭据引用，没有放明文的字段
    reg = set(registry.SourceRegistration.__dataclass_fields__)
    assert {"cred_ref", "scope_allowlist", "egress", "access_window", "budget", "masking_policy",
            "authorization_note", "owner", "environment"} <= reg
    assert not {"password", "secret", "token"} & reg


def test_quota_marks_collection_items_not_enforced(env):
    client, _ = env
    body = client.get("/api/v1/admin/quotas").json()
    assert body["not_enforced"] == ["collections_per_source_per_hour", "snapshot_git_mb"]
    assert "collections_per_source_per_hour" in body["per_user"] and "snapshot_git_mb" in body["per_project"]
