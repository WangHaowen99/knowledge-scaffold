"""发布 / 评测前的数据绑定门禁（ADR-0007）：问题分级、修复入口、降级发布与审计。"""

import asyncio
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config
from app.services import modeling_eval, package_mapping
from app.services.workspace import WorkspaceError, WorkspaceManager

openpyxl = pytest.importorskip("openpyxl")

FIELDS_HEADER = (
    "# 对象属性清单\n"
    "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |\n"
    "| --- | --- | --- | --- | --- | --- | --- |\n"
)


def object_doc(source: str | None, columns: list[str]) -> str:
    rows = "".join(f"| 字段{i} | {name} | 合成字段 | 否 | 字符串 | 无 | 无 |\n" for i, name in enumerate(columns))
    head = "# 对象描述\n合成对象。\n\n# 英文名称\nOrder\n\n"
    return head + (f"# 数据来源\n{source}\n\n" if source is not None else "") + FIELDS_HEADER + rows


def write_schema(root: Path, sheets: dict[str, list[str]], purpose: str = "schema") -> None:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, columns in sheets.items():
        workbook.create_sheet(name).append(columns)
    (root / "resource" / "excel").mkdir(parents=True, exist_ok=True)
    workbook.save(root / "resource" / "excel" / "结构.xlsx")
    (root / "resource" / "materials.json").write_text(
        json.dumps({"resource/excel/结构.xlsx": {"purpose": purpose}}, ensure_ascii=False), encoding="utf-8"
    )


def write_object(root: Path, key: str, source: str | None, columns: list[str]) -> None:
    (root / "ontology" / "object").mkdir(parents=True, exist_ok=True)
    (root / "ontology" / "object" / f"{key}.md").write_text(object_doc(source, columns), encoding="utf-8")


# ---- 问题分级与候选表 ----


def test_declaration_without_any_table_is_unverifiable_not_blocking(tmp_path):
    write_object(tmp_path, "订单", "T_ORDER", ["ORDER_ID"])
    result = package_mapping.resolve_object_tables(tmp_path)
    assert not result["problems"]
    [issue] = result["issues"]
    assert issue["kind"] == "declared_unverifiable" and issue["blocking"] is False
    assert issue["declared"] == "T_ORDER"
    assert issue["message"] in result["warnings"]


def test_missing_declaration_blocks_with_ranked_candidates(tmp_path):
    write_schema(tmp_path, {
        "T_ORDER": ["ORDER_ID", "AMOUNT"],
        "T_ORDER_HIST": ["ORDER_ID"],
        "CUSTOMER_PROFILE": ["CUSTOMER_ID"],
    })
    write_object(tmp_path, "订单", "T_ORDR", ["ORDER_ID", "AMOUNT"])

    strict = package_mapping.resolve_object_tables(tmp_path)
    [issue] = strict["issues"]
    assert issue["kind"] == "declared_missing" and issue["blocking"] is True
    assert issue["message"] in strict["problems"]
    assert [c["table"] for c in issue["candidates"]] == ["T_ORDER", "T_ORDER_HIST"]
    assert issue["candidates"][0]["matched"] == 2

    lenient = package_mapping.resolve_object_tables(tmp_path, strict_declared=False)
    assert not lenient["problems"]
    assert lenient["issues"][0]["blocking"] is False


def test_declared_table_without_matching_column_suggests_other_tables(tmp_path):
    write_schema(tmp_path, {"T_STOCK": ["STOCK_ID"], "T_ORDER": ["ORDER_ID", "AMOUNT"]})
    write_object(tmp_path, "订单", "T_STOCK", ["ORDER_ID", "AMOUNT"])
    [issue] = package_mapping.resolve_object_tables(tmp_path)["issues"]
    assert issue["kind"] == "no_columns" and issue["table"] == "T_STOCK"
    assert [c["table"] for c in issue["candidates"]] == ["T_ORDER"]


# ---- 修复：改写「# 数据来源」声明 ----


def test_set_declared_source_replaces_bare_name_and_keeps_notes():
    text = object_doc("T_OLD\n\n> 按日汇总。", ["ORDER_ID"])
    out = package_mapping.set_declared_source(text, "T_ORDER")
    assert "# 数据来源\nT_ORDER\n\n> 按日汇总。\n\n# 对象属性清单" in out
    assert "T_OLD" not in out


def test_set_declared_source_keeps_sentence_as_note_and_drops_placeholder():
    sentence = object_doc("- 取自 `T_OLD` 的明细（按月）。", ["ORDER_ID"])
    out = package_mapping.set_declared_source(sentence, "")
    assert "# 数据来源\n无\n> 取自 `T_OLD` 的明细（按月）。\n" in out

    placeholder = object_doc("{待填写：真实数据表名}", ["ORDER_ID"])
    out = package_mapping.set_declared_source(placeholder, "T_ORDER")
    assert "# 数据来源\nT_ORDER\n\n# 对象属性清单" in out and "待填写" not in out


def test_set_declared_source_inserts_missing_section_after_english_name():
    out = package_mapping.set_declared_source(object_doc(None, ["ORDER_ID"]), "T_ORDER")
    assert "# 英文名称\nOrder\n\n# 数据来源\nT_ORDER\n\n# 对象属性清单" in out
    bare = package_mapping.set_declared_source(FIELDS_HEADER, "T_ORDER")
    assert bare.startswith("# 数据来源\nT_ORDER\n\n# 对象属性清单")


# ---- 工作区：体检、修复入口、降级发布与审计 ----


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    return WorkspaceManager(tmp_path / "data", assets)


def gated_project(manager: WorkspaceManager) -> tuple[str, Path]:
    """T_ORDER 可绑；「客户」声明了不存在的表；另有一份未标 schema 的表格资料。"""
    pid = manager.create_project("发布门禁")["id"]
    root = manager.project_root(pid)
    write_schema(root, {"T_ORDER": ["ORDER_ID", "AMOUNT"], "T_CUSTOMER": ["CUSTOMER_ID", "NAME"]})
    (root / "resource" / "others").mkdir(parents=True, exist_ok=True)
    (root / "resource" / "others" / "附表.csv").write_text("A,B\n", encoding="utf-8")
    write_object(root, "订单", "T_ORDER", ["ORDER_ID", "AMOUNT"])
    write_object(root, "客户", "T_CLIENT", ["CUSTOMER_ID", "NAME"])
    return pid, root


def test_binding_check_returns_fix_handles(manager):
    pid, root = gated_project(manager)
    check = manager.package_binding_check(pid)
    assert check["blocking"] == 1 and check["objects"] == 2 and check["bound_objects"] == 1
    [issue] = check["issues"]
    assert issue["object"] == "客户" and issue["path"] == "ontology/object/客户.md"
    assert issue["revision"] and issue["candidates"][0]["table"] == "T_CUSTOMER"
    assert check["tables"] == ["T_CUSTOMER", "T_ORDER"] and check["table_count"] == 2
    assert check["table_resources"] == ["resource/others/附表.csv"]
    assert check["truncated"] is False and check["last_publish"] is None


def test_strict_publish_blocks_and_degraded_publish_is_audited(manager, tmp_path):
    pid, _ = gated_project(manager)
    with pytest.raises(WorkspaceError) as blocked:
        manager.publish_package(pid, tmp_path / "strict.zip", author="alice")
    assert blocked.value.code == "binding_problems" and "T_CLIENT" in str(blocked.value)
    assert manager.get_project(pid).get("last_package_publish") is None

    dest = manager.publish_package(pid, tmp_path / "degraded.zip", allow_unbound=True, author="alice")
    with zipfile.ZipFile(dest) as zf:
        mapping_name = next(n for n in zf.namelist() if n.endswith("object-data-mappings.json"))
        mappings = json.loads(zf.read(mapping_name))
    assert "订单" in mappings["objects"] and "客户" not in mappings["objects"]
    audit = manager.get_project(pid)["last_package_publish"]
    assert audit["by"] == "alice" and audit["degraded"] is True
    assert audit["unbound_count"] == 1 and audit["unbound_objects"] == ["客户"]
    assert manager.package_binding_check(pid)["last_publish"]["degraded"] is True


def test_degraded_publish_still_enforces_script_contract(manager, tmp_path):
    """逃生通道只放过数据绑定，脚本契约等其他发布检查照常阻断。"""
    pid, root = gated_project(manager)
    (root / "ontology" / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "ontology" / "scripts" / "calc.py").write_text(
        '"""\n关联operation：无\n输入参数：\n    order_id    string    订单号，必填\n"""\n'
        "import argparse\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--other-id', required=True)\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceError, match="契约无效"):
        manager.publish_package(pid, tmp_path / "pkg.zip", allow_unbound=True)
    assert manager.get_project(pid).get("last_package_publish") is None


def test_publish_without_any_table_is_not_degraded(manager, tmp_path):
    pid = manager.create_project("纯概念")["id"]
    write_object(manager.project_root(pid), "订单", "T_ORDER", ["ORDER_ID"])
    manager.publish_package(pid, tmp_path / "pkg.zip")
    audit = manager.get_project(pid)["last_package_publish"]
    assert audit["degraded"] is False and audit["unbound_count"] == 0


def test_set_object_data_source_fixes_the_gate(manager, tmp_path):
    pid, root = gated_project(manager)
    issue = manager.package_binding_check(pid)["issues"][0]
    manager.set_object_data_source(pid, "客户", "t_customer", base_revision=issue["revision"], author="alice")
    assert "# 数据来源\nT_CUSTOMER\n" in (root / "ontology" / "object" / "客户.md").read_text(encoding="utf-8")
    assert manager.package_binding_check(pid)["blocking"] == 0
    manager.publish_package(pid, tmp_path / "pkg.zip")
    assert manager.get_project(pid)["last_package_publish"]["degraded"] is False


def test_set_object_data_source_guards(manager):
    pid, root = gated_project(manager)
    with pytest.raises(WorkspaceError) as stale:
        manager.set_object_data_source(pid, "客户", "", base_revision="stale")
    assert stale.value.status == 409
    with pytest.raises(WorkspaceError, match="不在 schema 资料或评测数据源里"):
        manager.set_object_data_source(pid, "客户", "T_UNKNOWN")
    for key in ("不存在", "../客户", ""):
        with pytest.raises(WorkspaceError) as missing:
            manager.set_object_data_source(pid, key, "")
        assert missing.value.status == 404
    manager.set_object_data_source(pid, "客户", "")
    assert "# 数据来源\n无\n" in (root / "ontology" / "object" / "客户.md").read_text(encoding="utf-8")


# ---- 评测：同一门禁，确认后可降级继续 ----


def eval_router(tmp_path, monkeypatch):
    from app.routers import projects

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "MODELING_TOKEN", "")
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(projects, "workspaces", manager)
    monkeypatch.setattr(modeling_eval, "enabled", lambda: True)
    monkeypatch.setattr(modeling_eval, "reachable", lambda: True)
    pid = manager.create_project("评测门禁")["id"]
    root = manager.project_root(pid)
    write_schema(root, {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    write_object(root, "订单", "T_ORDR", ["ORDER_ID", "AMOUNT"])
    request = SimpleNamespace(state=SimpleNamespace(user=None), headers={"authorization": "Bearer t"})
    return projects, pid, request


def test_eval_run_reports_binding_problems_until_user_allows_unbound(tmp_path, monkeypatch):
    projects, pid, request = eval_router(tmp_path, monkeypatch)
    captured = {}

    async def fake_run(project_id, root_, token, cases, data_source, eval_set_name="", allow_unbound=False, refresh_token=""):
        captured["allow_unbound"] = allow_unbound

    monkeypatch.setattr(modeling_eval, "run", fake_run)
    cases = [{"question": "Q1", "groundTruth": "G1"}]

    blocked = asyncio.run(projects.eval_run(pid, projects.EvalRunBody(cases=cases), request))
    payload = json.loads(blocked.body)
    assert blocked.status_code == 400 and payload["code"] == "binding_problems"
    assert payload["issues"][0]["object"] == "订单"
    assert payload["issues"][0]["candidates"][0]["table"] == "T_ORDER"

    async def call():
        result = await projects.eval_run(pid, projects.EvalRunBody(cases=cases, allow_unbound=True), request)
        await asyncio.sleep(0)
        return result

    assert asyncio.run(call())["started"] is True
    assert captured["allow_unbound"] is True
