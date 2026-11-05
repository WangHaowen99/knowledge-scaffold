"""越过的上游节点算可补做（ADR-0084）：导入知识包后建模已完成，资料分析不再挡下一步。"""

from app.routers import projects as projects_router
from app.services import publish as publish_service
from app.services.flow import model as flow_model
from app.services.flow import view
from tests.test_changesets import main_write, obj
from tests.test_node_pack_lifecycle import env  # noqa: F401 - 共用 fixture


def _node(built: dict, node_id: str) -> dict:
    return next(node for node in built["nodes"] if node["id"] == node_id)


def _model_done(root) -> None:
    main_write(root, "ontology/object/订单.md", obj("订单", "合成"))
    main_write(root, "ontology/operation/下单.md", "# 下单\n\n合成操作 [[订单]]\n")


def _primary_command(built: dict) -> str:
    return (built["suggestions"]["primary"] or {}).get("action", {}).get("command", "")


def test_research_is_backfill_once_model_is_done(env):
    _client, manager, project_id, _tmp = env
    root = manager.project_root(project_id)
    total = flow_model.progress(root)["total"]
    _model_done(root)
    built = view.build(root)
    research = _node(built, "core.research")
    assert research["flags"]["backfill"] is True and research["display_state"] == "backfill"
    assert research["counts_in_progress"] is False
    assert flow_model.progress(root)["total"] == total - 1
    assert _primary_command(built) == "/ontology-validate"


def test_research_stays_next_when_nothing_uses_it_yet(env):
    _client, manager, project_id, _tmp = env
    built = view.build(manager.project_root(project_id))
    assert _node(built, "core.research")["flags"]["backfill"] is False
    assert built["suggestions"]["primary"]["id"].startswith("core.research")


def test_validate_is_not_bypassed_by_a_done_scenario_node(env):
    _client, manager, project_id, _tmp = env
    root = manager.project_root(project_id)
    _model_done(root)
    main_write(root, "ontology/testcase/下单.md", "# 测试场景：下单\n\n## 场景 1：合成\n")
    built = view.build(root)
    assert _node(built, "trace.trace")["state"] in ("confirmed", "stale")
    validate = _node(built, "core.validate")
    assert validate["flags"]["backfill"] is False and validate["counts_in_progress"] is True
    assert _primary_command(built) == "/ontology-validate"


def test_import_start_follows_the_delivered_entries():
    assert projects_router._start_node({})["id"] == "core.research"
    assert projects_router._start_node({"objects": 2})["id"] == "core.research"
    start = projects_router._start_node({"objects": 2, "operations": 1})
    assert start["id"] == "core.validate" and start["title"] == "校验"


def test_imported_package_starts_from_validate(env):
    client, manager, project_id, tmp_path = env
    _model_done(manager.project_root(project_id))
    archive = tmp_path / "package.zip"
    publish_service.export_package_zip(manager, project_id, archive, allow_unbound=True)
    files = {"file": ("package.zip", archive.read_bytes(), "application/zip")}
    upload_id = client.post("/api/v1/uploads", data={"purpose": "project_import"}, files=files).json()["upload_id"]
    inspected = client.post("/api/v1/projects/import/inspect", json={"upload_id": upload_id, "name": "导入"}).json()
    assert inspected["kind"] == "published_package"
    assert inspected["after_import"]["start_node"] == "core.validate"
    assert inspected["after_import"]["start_title"] == "校验"
    created = client.post("/api/v1/projects/import", json={"upload_id": upload_id, "name": "导入"})
    assert created.status_code == 201, created.text
    built = view.build(manager.project_root(created.json()["id"]))
    assert _node(built, "core.research")["flags"]["backfill"] is True
    model = _node(built, "core.model")
    assert model["state"] == "confirmed" and model["flags"]["needs_review"] is True
    assert _primary_command(built) == "/ontology-validate"
