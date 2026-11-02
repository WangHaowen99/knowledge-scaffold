"""I1：Studio 结构化编辑的草稿（契约 ST-07 `form`）——表单按当前原文渲染成 Markdown，没动的部分逐字节保留。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.services import auth, streams, studio_form
from app.services.workspace import WorkspaceManager

STORE = """# 对象描述
合成场景里的门店，按 [[门店等级]] 分级。

补充：门店由区域经理管理。

# 对象别名
无

# 英文名称
Store

# 数据来源

# 对象属性清单
| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |
| --- | --- | --- | --- | --- | --- | --- |
| 门店编号 | store_id | 主键编号 | 是 | 字符串 | 店号 | 无 |
| 门店名称 | store_name | 展示名 | 否 | 字符串 | 无 | 必填 |

# 关联对象
- 当前对象 → 属于（level_code → level_code） → [[门店等级]]
"""

LEVEL = "# 门店等级\n\n## 对象描述\n\n合成的门店等级。\n\n## 对象属性清单\n\n| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |\n| --- | --- | --- | --- | --- | --- | --- |\n| 等级编码 | level_code | 编码 | 是 | 字符串 | 无 | 无 |\n\n## 关联对象\n\n无\n"
REGION = LEVEL.replace("门店等级", "区域").replace("level_code", "region_code").replace("等级编码", "区域编码")

RESTOCK = """# 补货下单

## 业务描述

按安全库存给门店生成补货单。

## 与operation的关联

无

## 与object的关联

- 输入数据的对象：[[门店]]

## 输入

无

## 输出

无

## 逻辑描述

1. 读门店库存。
2. 低于安全库存时下单。
"""


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    project_id = workspaces.create_project("表单合成", owner="employee_0001")["id"]
    root = workspaces.project_root(project_id)
    for rel, text in (("ontology/object/门店.md", STORE), ("ontology/object/门店等级.md", LEVEL),
                      ("ontology/object/区域.md", REGION), ("ontology/operation/补货下单.md", RESTOCK)):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "web: 合成条目")
    streams.reset_all()
    with TestClient(app) as client:
        yield client, project_id, root
    streams.reset_all()


def entry(client, pid, entry_id):
    return client.get(f"/api/v1/projects/{pid}/studio/entries/{entry_id}").json()


def put_form(client, pid, entry_id, form, base_revision=""):
    return client.put(f"/api/v1/projects/{pid}/studio/entries/{entry_id}/draft",
                      json={"base_revision": base_revision, "form": form})


def test_round_trip_keeps_bytes(api):
    """ST-04 读出来、原样当表单存回去，渲染出的原文和文件逐字节相同。"""
    client, pid, root = api
    for entry_id, rel in (("object:门店", "ontology/object/门店.md"), ("operation:补货下单", "ontology/operation/补货下单.md")):
        current = entry(client, pid, entry_id)
        form = studio_form.form_from_entry(current)
        saved = put_form(client, pid, entry_id, form, current["revision"])
        assert saved.status_code == 200, saved.text
        assert saved.json()["draft"]["content"] == (root / rel).read_text(encoding="utf-8")
        assert saved.json()["draft"]["form"]["key"] == current["key"]


def test_object_fields_description_and_data_source(api):
    client, pid, root = api
    current = entry(client, pid, "object:门店")
    form = studio_form.form_from_entry(current)
    form["description"] = "合成门店，按等级分级。"
    form["fields"] = [
        {"name": "门店编号", "column": "store_id", "type": "字符串"},              # 不变：整行原样
        {"name": "门店简称", "column": "store_name", "type": "字符串"},            # 改中文名：别的列保留
        {"name": "开业日期", "column": "opened_on", "type": "日期", "unit": None},  # 新增
    ]
    form["data_source"] = {"kind": "table", "value": "store"}
    saved = put_form(client, pid, "object:门店", form, current["revision"])
    assert saved.status_code == 200, saved.text
    text = saved.json()["draft"]["content"]
    assert "合成门店，按等级分级。\n\n补充：门店由区域经理管理。" in text
    assert "| 门店编号 | store_id | 主键编号 | 是 | 字符串 | 店号 | 无 |" in text
    assert "| 门店简称 | store_name | 展示名 | 否 | 字符串 | 无 | 必填 |" in text
    assert "| 开业日期 | opened_on |  | 否 | 日期 | 无 | 无 |" in text
    assert "# 数据来源\nstore\n" in text
    # 「# 文档名 + ## 章节」写法的条目改不了数据来源（加一级标题会打乱章节）
    level = studio_form.form_from_entry(entry(client, pid, "object:门店等级"))
    refused = put_form(client, pid, "object:门店等级", {**level, "data_source": {"kind": "table", "value": "level"}})
    assert refused.status_code == 400 and refused.json()["details"]["fields"] == {"data_source": "unsupported"}
    # 草稿带回表单（编辑器重新打开时还原），ST-04 的 draft 也有
    again = client.get(f"/api/v1/projects/{pid}/studio/entries/object:门店/draft").json()["draft"]
    assert again["form"]["fields"][2]["column"] == "opened_on"
    assert entry(client, pid, "object:门店")["draft"]["form"]["description"] == "合成门店，按等级分级。"
    # 渲染结果按 ST-04 同一口径读回来就是表单里的内容
    (root / "ontology/object/门店.md").write_text(text, encoding="utf-8")
    after = entry(client, pid, "object:门店")
    assert [f["column"] for f in after["fields"]] == ["store_id", "store_name", "opened_on"]
    assert after["description"] == "合成门店，按等级分级。" and after["data_source"] == {"kind": "table", "value": "store", "label": "store"}


def test_submit_commits_rendered_draft(api):
    """ST-08 照旧用草稿原文走确定性检查：通过时文件就是渲染出来的内容。"""
    client, pid, root = api
    current = entry(client, pid, "object:门店等级")
    form = studio_form.form_from_entry(current)
    form["description"] = "合成的门店等级，分 A、B、C 三档。"
    form["fields"] = [*form["fields"], {"name": "等级名称", "column": "level_name", "type": "字符串"}]
    text = put_form(client, pid, "object:门店等级", form, current["revision"]).json()["draft"]["content"]
    result = client.post(f"/api/v1/projects/{pid}/studio/entries/object:门店等级/submit", json={}).json()
    assert result["result"] == "committed", result
    assert (root / "ontology/object/门店等级.md").read_text(encoding="utf-8") == text
    after = entry(client, pid, "object:门店等级")
    assert after["description"] == "合成的门店等级，分 A、B、C 三档。" and after["draft"] is None
    assert [f["column"] for f in after["fields"]] == ["level_code", "level_name"]


def test_delete_field_and_relations(api):
    client, pid, root = api
    current = entry(client, pid, "object:门店")
    form = studio_form.form_from_entry(current)
    assert form["relations"] == ["object:门店等级"]
    form["fields"] = form["fields"][:1]
    form["relations"] = ["object:区域"]
    text = put_form(client, pid, "object:门店", form).json()["draft"]["content"]
    assert "store_name" not in text and "store_id" in text
    assert "[[门店等级]]" in text.split("# 关联对象")[0]  # 描述里的链接是正文，不动
    relations = text.split("# 关联对象")[1]
    assert "门店等级" not in relations and "- 当前对象 → 关联 → [[区域]]" in relations
    # 关联全删掉时写「无」
    form["relations"] = []
    text = put_form(client, pid, "object:门店", form).json()["draft"]["content"]
    assert text.split("# 关联对象")[1].strip() == "无"


def test_operation_sections_and_name(api):
    client, pid, root = api
    current = entry(client, pid, "operation:补货下单")
    form = studio_form.form_from_entry(current)
    assert set(form["sections"]) >= {"业务描述", "逻辑描述"}
    form["sections"]["逻辑描述"] = "1. 读门店库存。\n2. 低于安全库存时下单。\n3. 下单后通知店长。"
    form["name"] = "门店补货下单"
    text = put_form(client, pid, "operation:补货下单", form).json()["draft"]["content"]
    assert text.startswith('---\nreport_name: "门店补货下单"\n---\n# 门店补货下单\n')
    assert "3. 下单后通知店长。" in text and "- 输入数据的对象：[[门店]]" in text
    result = client.post(f"/api/v1/projects/{pid}/studio/entries/operation:补货下单/submit", json={}).json()
    assert result["result"] == "committed", result
    after = entry(client, pid, "operation:补货下单")
    assert after["name"] == "门店补货下单" and "3. 下单后通知店长。" in next(
        s["content_md"] for s in after["sections"] if s["title"] == "逻辑描述")


def test_template_entry_description(api):
    """模板写法（一级标题当章节、描述是占位）：描述直接换掉占位。"""
    client, pid, root = api
    created = client.post(f"/api/v1/projects/{pid}/studio/entries", json={"kind": "object", "name": "借阅证"}).json()
    form = studio_form.form_from_entry(created)
    form["description"] = "合成：读者借书时出示的凭证。"
    text = put_form(client, pid, "object:借阅证", form, created["revision"]).json()["draft"]["content"]
    assert text.startswith("# 对象描述\n合成：读者借书时出示的凭证。\n\n# 对象别名")
    assert "{待填写：该对象对应的真实数据表名" in text  # 数据来源没动，占位原样


def test_rejects_key_rename_and_bad_input(api):
    client, pid, _root = api
    form = studio_form.form_from_entry(entry(client, pid, "object:门店"))
    bad = put_form(client, pid, "object:门店", {**form, "key": "store"})
    assert bad.status_code == 400 and bad.json()["details"]["fields"] == {"key": "rename_unsupported"}
    assert put_form(client, pid, "object:门店", {**form, "name": ""}).json()["details"]["fields"] == {"name": "required"}
    assert put_form(client, pid, "object:门店", {**form, "fields": "x"}).json()["details"]["fields"] == {"fields": "invalid"}
    none = client.put(f"/api/v1/projects/{pid}/studio/entries/object:门店/draft", json={"base_revision": ""})
    assert none.status_code == 400 and none.json()["details"]["fields"] == {"content": "required"}
    # 原文方式照旧，草稿没有 form
    raw = client.put(f"/api/v1/projects/{pid}/studio/entries/object:门店/draft", json={"base_revision": "", "content": STORE})
    assert raw.status_code == 200 and raw.json()["draft"]["form"] is None
