"""Studio 写路径（草稿/租约/确定性分流）、报告条目解析与回收站的测试。"""

import time
from pathlib import Path

import pytest

from app.services import report_items, studio
from app.services.workspace import WorkspaceError, WorkspaceManager

OBJECT_OK = """# 对象描述

订单是交易的核心record。

# 对象属性清单

| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |
|--------|--------|-----|----|-----|--------|--------|
| 编号 | id | 唯一编号 | 是 | 字符串 | 无 | 唯一 |

# 关联对象

- 当前对象 → 属于 → [[用户]]
"""


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    root = tmp_path / "ws"
    (root / "ontology" / "object").mkdir(parents=True)
    (root / "ontology" / "logic").mkdir(parents=True)
    (root / "ontology" / "object" / "用户.md").write_text("# 对象描述\n\n用户\n", encoding="utf-8")
    return root


def test_lease_conflict_and_expiry(env, monkeypatch):
    studio.acquire_lease("p1", "ontology/object/订单.md", "alice")
    with pytest.raises(studio.StudioError) as exc:
        studio.acquire_lease("p1", "ontology/object/订单.md", "bob")
    assert exc.value.status == 409
    # 同人可续期
    studio.acquire_lease("p1", "ontology/object/订单.md", "alice")
    # 过期后他人可取得
    leases = studio._load("p1", "leases")
    leases["ontology/object/订单.md"]["expires"] = int(time.time()) - 1
    studio._save("p1", "leases", leases)
    assert studio.acquire_lease("p1", "ontology/object/订单.md", "bob")["owner"] == "bob"


def test_draft_roundtrip_and_discard(env):
    studio.save_draft("p2", "ontology/object/订单.md", "内容", "rev1", "alice")
    draft = studio.get_draft("p2", "ontology/object/订单.md")
    assert draft["content"] == "内容" and draft["base_revision"] == "rev1"
    studio.discard_draft("p2", "ontology/object/订单.md", "alice")
    assert studio.get_draft("p2", "ontology/object/订单.md") is None
    assert studio.lease_status("p2", "ontology/object/订单.md") is None


def test_deterministic_check_pass(env):
    checks = studio.deterministic_check(env, "ontology/object/订单.md", OBJECT_OK)
    assert checks["errors"] == []
    assert checks["review"] == []


def test_deterministic_check_rejects_missing_section(env):
    bad = "# 对象描述\n\n只有描述\n"
    checks = studio.deterministic_check(env, "ontology/object/订单.md", bad)
    assert any("对象属性清单" in e for e in checks["errors"])


def test_deterministic_check_routes_to_review(env):
    content = OBJECT_OK + "\n# 自定义章节\n\n内容含[[不存在的对象]]\n"
    checks = studio.deterministic_check(env, "ontology/object/订单.md", content)
    assert checks["errors"] == []
    assert any("自定义章节" in r for r in checks["review"])
    assert any("不存在的对象" in r for r in checks["review"])


def test_sections_serialize_roundtrip():
    sections = studio.parse_sections(OBJECT_OK)
    rebuilt = studio.serialize_sections(sections)
    assert studio.parse_sections(rebuilt) == sections


def test_report_items_parse():
    report = """## 规则检查结果 2026-01-01

共发现问题若干。

### 订单.md

- 缺少主键标记
- 警告：属性别名为空
1. [[支付]] 链接失效

### 无问题文件.md

- 无问题
"""
    parsed = report_items.parse(report)
    texts = [i["text"] for i in parsed["items"]]
    assert "缺少主键标记" in texts
    assert any(i["severity"] == "warning" for i in parsed["items"])
    assert all("无问题" not in t for t in texts)
    assert parsed["items"][0]["group"] == "订单.md"
    assert parsed["items"][0]["id"] == 1


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    assets = tmp_path / "assets"
    (assets / "claude-workspace-dotclaude" / "templates").mkdir(parents=True)
    (assets / "resource-template").mkdir(parents=True)
    return WorkspaceManager(tmp_path / "data", assets)


def test_trash_list_and_restore(manager: WorkspaceManager):
    meta = manager.create_project("待恢复")
    manager.delete_project(meta["id"])
    entries = manager.list_trash()
    assert entries and entries[0]["project_name"] == "待恢复"
    restored = manager.restore_trash(entries[0]["name"])
    assert restored["name"] == "待恢复"
    assert manager.get_project(restored["id"])["name"] == "待恢复"
    assert manager.list_trash() == []


def test_restore_conflicting_id_gets_new_id(manager: WorkspaceManager):
    meta = manager.create_project("重名")
    manager.delete_project(meta["id"])
    entry = manager.list_trash()[0]
    # 人为占用原 id
    (manager.workspaces_dir / meta["id"]).mkdir(parents=True)
    restored = manager.restore_trash(entry["name"])
    assert restored["id"] != meta["id"]
    assert manager.get_project(restored["id"])["name"] == "重名"


def test_delete_file_only_knowledge(manager: WorkspaceManager):
    meta = manager.create_project("知识")
    manager.write_file(meta["id"], "knowledge/规则一.md", "# 规则一\n\n内容")
    manager.delete_file(meta["id"], "knowledge/规则一.md", author="alice")
    with pytest.raises(WorkspaceError):
        manager.delete_file(meta["id"], "research/x.md")


CODEGEN_REPORT = """# 脚本化候选扫描 — 2026-01-01 10:00 Asia/Shanghai

共扫描 4 条 logic：✅ 适合 1 / ⚠️ 部分适合 1 / ❌ 不适合 1 / 📦 已生成 1

## ✅ 适合（1）

| # | 逻辑 | 片段 | 类型 | 输入 | 输出 | 已有脚本 |
|---|---|---|---|---|---|---|
| 1 | 客群聚类 | 整段 | 算法计算 | `factors, k` | `DataFrame[user_id]` | — |

## ⚠️ 部分适合（1）

| # | 逻辑 | 片段 | 类型 | 输入 | 输出 | 备注 |
|---|---|---|---|---|---|---|
| 2 | 活动归因 | 步骤1-3 | SQL查询封装 | `activity_id` | `DataFrame[score]` | 步骤4 留 logic |

## ❌ 不适合（1）

| 逻辑 | 原因 |
|---|---|
| 上架审批 | 依赖人工评审 |

## 📦 已生成（1）

| 逻辑 | 已有脚本 |
|---|---|
| 历史聚类 | `a.py`<br>`b.py` |
"""


def test_parse_codegen_report():
    parsed = report_items.parse(CODEGEN_REPORT)
    assert parsed["type"] == "codegen"
    assert parsed["summary"].startswith("共扫描 4 条")
    assert [c["id"] for c in parsed["candidates"]] == [1, 2]
    first = parsed["candidates"][0]
    assert first["logic"] == "客群聚类" and first["fragment"] == "整段"
    assert first["verdict"] == "适合" and first["inputs"] == "factors, k"
    second = parsed["candidates"][1]
    assert second["verdict"] == "部分适合" and second["fragment"] == "步骤1-3"
    assert parsed["unsuitable"][0]["logic"] == "上架审批"
    assert "、" in parsed["generated"][0]["scripts"]


def test_generic_report_not_misdetected():
    parsed = report_items.parse("## 规则检查结果\n\n- 缺少主键")
    assert "type" not in parsed
