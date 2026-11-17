"""自主构建策略、平台限制、模型单价（详设 8.5、4.10.1；方案 B13、B14；契约 AB-01、AB-02、SY-17、SY-18）。

- 没有预算（F6）：老请求带的 `budget` 忽略，策略和平台限制里都不再有单次上限；
- 生产库采集授权随证据采集删除：老请求带的忽略；做到哪一步不能是发布；
- 按节点排（节点模型重构 1B）：做到哪一步写旧节点编号按别名认；按段时代的 `slice_ids` 存着不用；
- 同时运行数上限是配置项（设置页保存的优先于环境变量）；
- 费用 = token × 单价；没有单价时用引擎报的费用（单价表是美元时），都没有记 0 并标估算。

测试数据全部为合成内容。
"""

from __future__ import annotations

from app.governance import config_registry
from app.services import model_pricing
from tests.test_flow_states import env  # noqa: F401 - 共用 fixture


def _url(project_id: str, tail: str = "policy") -> str:
    return f"/api/v1/projects/{project_id}/autobuild/{tail}"


def test_policy_defaults_and_save(env):
    client, _workspaces, _manager, project_id, _root = env
    first = client.get(_url(project_id)).json()
    assert set(first) == {"scope", "advanced", "platform_limits", "last_run", "revision", "not_enforced", "optional_nodes",
                          "until_order"}
    # 「包括可选步骤」多做的节点：默认启用的官方包里能自主做的可选节点（生成脚本 autobuild: false、求解适配是面板，不在里面），
    # 带挂在哪个必需节点下、排第几；没设做到哪一步时 until_order 为 null
    assert [(item["id"], item["name"], item["under"]) for item in first["optional_nodes"]] == [
        ("diagram.flowchart", "绘制流程图", "core.research"), ("core.metric", "提取规则与指标", "core.operation")]
    assert first["optional_nodes"][0]["order"] < first["optional_nodes"][1]["order"] and first["until_order"] is None
    assert first["scope"]["mode"] == "all_pending" and first["advanced"]["confidence_threshold"] == 0.8
    assert first["platform_limits"] == {} and first["last_run"] is None
    assert first["not_enforced"] == []
    body = {"base_revision": first["revision"], "budget": {"max_duration_min": 60, "max_tokens": 500_000},
            "advanced": {"below_threshold": "suspend", "must_suspend_kinds": ["新增对象"]},
            "production_collection": [{"source_id": "src-1", "allowed": True}]}
    saved = client.put(_url(project_id), json=body).json()
    assert "budget" not in saved and saved["advanced"]["below_threshold"] == "suspend"  # 老客户端带的预算忽略
    assert saved["advanced"]["confidence_threshold"] == 0.8  # 没给的项保留
    assert "production_collection" not in saved  # 老客户端带的生产库采集授权忽略
    assert saved["revision"] != first["revision"] and saved["optional_nodes"] == first["optional_nodes"]
    stale = client.put(_url(project_id), json={**body, "base_revision": first["revision"]})
    assert stale.status_code == 409 and stale.json()["code"] == "revision_conflict"


def test_policy_validation(env):
    client, _workspaces, _manager, project_id, _root = env
    # 预算不再有上限可超：再大的数也照常保存（字段本身忽略）
    assert client.put(_url(project_id), json={"budget": {"max_tokens": 20_000_000}}).status_code == 200
    for bad, field in (({"advanced": {"confidence_threshold": 0.3}}, "advanced.confidence_threshold"),
                       ({"advanced": {"max_fix_rounds": 5}}, "advanced.max_fix_rounds"),
                       ({"scope": {"until_node": "core.publish"}}, "scope.until_node"),
                       ({"scope": {"slice_ids": "s1"}}, "scope.slice_ids")):
        response = client.put(_url(project_id), json=bad)
        assert response.status_code == 400 and field in response.json()["details"]["fields"], bad
    # 不再分段：「选定的段」按「全部没做完的」存，不报错；按段时代的 slice_ids 原样存着、不再起作用
    for scope in ({"mode": "selected", "slice_ids": []}, {"mode": "selected", "slice_ids": ["s9"]}):
        saved = client.put(_url(project_id), json={"scope": scope})
        assert saved.status_code == 200 and saved.json()["scope"]["mode"] == "all_pending", scope
        assert saved.json()["scope"]["slice_ids"] == scope["slice_ids"]
    ok = client.put(_url(project_id), json={"scope": {"until_node": "core.validate"}})
    assert ok.status_code == 200 and ok.json()["scope"]["until_node"] == "core.validate"
    # 全局一致性节点删了：老策略写到它等于跑完主线，不报错
    old = client.put(_url(project_id), json={"scope": {"until_node": "core.consistency"}})
    assert old.status_code == 200 and old.json()["scope"]["until_node"] is None


def test_old_policy_with_slices_and_the_old_model_node_still_previews(env):
    """建模拆开、按节点排之前存的策略：做到「建模」（core.model）按别名认成提取操作，不回 400；选定的段、不生成场景
    存着不用，预览按节点出行，提取对象、提取操作都在范围里。"""
    from app.services.autobuild import policy, store

    client, _workspaces, _manager, project_id, _root = env
    old = policy.defaults()
    old["scope"].update(mode="selected", slice_ids=["s1", "s2"], generate_scenarios=False, until_node="core.model")
    store.write_json(policy._path(project_id), old)
    shown = client.get(_url(project_id)).json()["scope"]
    assert shown["until_node"] == "core.operation" and shown["slice_ids"] == ["s1", "s2"]
    preview = client.post(_url(project_id, "runs/preview"), json={})
    assert preview.status_code == 200, preview.text
    assert [(row["id"], row["included"]) for row in preview.json()["slices"]] == [
        ("core.object", True), ("core.operation", True)]
    saved = client.put(_url(project_id), json={"scope": {"until_node": "core.model"}}).json()["scope"]
    assert saved["until_node"] == "core.operation" and saved["mode"] == "all_pending"
    assert saved["slice_ids"] == ["s1", "s2"] and saved["generate_scenarios"] is False


def test_platform_limits_and_config_item(env):
    from app.services.autobuild import limits as autobuild_limits

    client, *_ = env
    # 证据采集删除前存的生产库采集授权：不再读，下次保存时清掉
    autobuild_limits.store.write_json(autobuild_limits._file(), {"allow_production_collection": True})
    limits = client.get("/api/v1/admin/autobuild-limits").json()
    assert limits["max_concurrent_runs"] == 2 and limits["running"] == 0
    assert not {"max_duration_min", "max_tokens", "max_cost"} & set(limits)
    assert "allow_production_collection" not in limits and limits["not_enforced"] == ["night_priority"]
    saved = client.put("/api/v1/admin/autobuild-limits", json={
        "max_concurrent_runs": 5, "max_tokens": 2_000_000, "allow_production_collection": "不是布尔值",
        "night_priority": {"enabled": True, "start": "21:00", "end": "07:00"}}).json()
    assert saved["max_concurrent_runs"] == 5 and "max_tokens" not in saved  # F6 去掉的单次上限忽略
    assert "allow_production_collection" not in saved  # 随证据采集删除，带了也忽略
    assert "allow_production_collection" not in autobuild_limits.stored()
    assert config_registry.resolve("ONTO_WEB_AUTOBUILD_MAX_RUNS") == (5, "data_dir")
    bad = client.put("/api/v1/admin/autobuild-limits", json={"max_concurrent_runs": 17, "night_priority": {}})
    assert bad.status_code == 400 and set(bad.json()["details"]["fields"]) == {"max_concurrent_runs", "night_priority"}
    # 不允许 0（主控定）：SY-17 回 422；环境变量里的 0 报配置错误、回落到下一个来源
    zero = client.put("/api/v1/admin/autobuild-limits", json={"max_concurrent_runs": 0})
    assert zero.status_code == 422 and zero.json()["code"] == "invalid_argument"
    assert zero.json()["details"] == {"fields": {"max_concurrent_runs": "min"}, "min": 1}
    assert client.get("/api/v1/admin/autobuild-limits").json()["max_concurrent_runs"] == 5


def test_zero_concurrent_runs_in_env_is_a_config_error(monkeypatch, tmp_path):
    from app import config
    from app.services.autobuild import limits

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("ONTO_WEB_AUTOBUILD_MAX_RUNS", "0")
    assert config_registry.resolve("ONTO_WEB_AUTOBUILD_MAX_RUNS") == ("2", "default")
    assert "ONTO_WEB_AUTOBUILD_MAX_RUNS" in config_registry.problems()
    assert limits.max_runs() == 2
    monkeypatch.setenv("ONTO_WEB_AUTOBUILD_MAX_RUNS", "3")
    assert config_registry.resolve("ONTO_WEB_AUTOBUILD_MAX_RUNS") == (3, "env") and limits.max_runs() == 3


def test_pricing_table_and_cost(env):
    client, *_ = env
    assert client.get("/api/v1/admin/settings/pricing").json() == {"currency": "USD", "items": []}
    bad = client.put("/api/v1/admin/settings/pricing", json={"currency": "EUR", "items": []})
    assert bad.status_code == 400
    row = {"engine": "jiuwen", "model": "model-standard", "input_per_mtok": 4, "output_per_mtok": 12}
    saved = client.put("/api/v1/admin/settings/pricing", json={"currency": "CNY", "items": [row]}).json()
    assert saved["items"][0]["cache_read_per_mtok"] is None
    usage = {"input_tokens": 1_000_000, "output_tokens": 500_000, "cache_read_input_tokens": 0}
    assert model_pricing.cost(usage, engine="jiuwen", model="model-standard") == (10.0, False)
    # 没有单价：单价表不是美元时不拿引擎报的美元费用，记 0 并标估算
    assert model_pricing.cost(usage, engine="claude", model="x", reported=0.5) == (0.0, True)
    client.put("/api/v1/admin/settings/pricing", json={"currency": "USD", "items": []})
    assert model_pricing.cost(usage, engine="claude", model="x", reported=0.5) == (0.5, False)
    # 成本口径（ADR-0088）：缓存读按 1/10 计，缓存写按输入计
    assert model_pricing.tokens_of({"input_tokens": 3, "output_tokens": 2, "cache_read_input_tokens": 20,
                                    "cache_creation_input_tokens": 4}) == 3 + 2 + 2 + 4
