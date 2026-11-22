"""Studio 写路径（草稿/确定性分流）、报告条目解析与回收站的测试。"""

import shutil
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


def test_drafts_no_longer_take_a_lease(env):
    """30 分钟硬租约随写入范围锁取消（方案 C10）：存草稿不占锁，助手在改的条目由写入范围锁挡
    （见 test_write_scope.py）；只是存草稿不会让别处的写入失败。"""
    studio.save_draft("p1", "ontology/object/订单.md", "甲的草稿", "", "alice")
    studio.save_draft("p1", "ontology/object/订单.md", "乙的草稿", "", "bob")
    assert studio.get_draft("p1", "ontology/object/订单.md")["content"] == "乙的草稿"
    assert studio._load("p1", "leases") == {}
    assert not hasattr(studio, "acquire_lease")


def test_draft_roundtrip_and_discard(env):
    studio.save_draft("p2", "ontology/object/订单.md", "内容", "rev1", "alice")
    draft = studio.get_draft("p2", "ontology/object/订单.md")
    assert draft["content"] == "内容" and draft["base_revision"] == "rev1"
    studio.discard_draft("p2", "ontology/object/订单.md", "alice")
    assert studio.get_draft("p2", "ontology/object/订单.md") is None


def test_save_draft_rejects_obsolete_baseline(env):
    path = "ontology/object/订单.md"
    with pytest.raises(studio.StudioError) as exc:
        studio.save_draft(
            "p2", path, "旧表单内容", "old-revision", "alice",
            current_revision="new-revision",
        )
    assert exc.value.status == 409
    assert "最新内容" in str(exc.value)
    assert studio.get_draft("p2", path) is None
    assert studio._load("p2", "leases") == {}


def test_submit_guard_rejects_obsolete_draft_baseline():
    with pytest.raises(studio.StudioError) as exc:
        studio.ensure_draft_current(
            {"base_revision": "old-revision"}, "new-revision"
        )
    assert exc.value.status == 409
    assert "旧版本" in str(exc.value)


def test_reconcile_draft_removes_content_already_applied_to_workspace(env):
    path = "ontology/object/订单.md"
    target = env / path
    target.write_text(OBJECT_OK, encoding="utf-8")
    studio.save_draft("p2", path, OBJECT_OK, "old-revision", "alice")
    staged = env / studio.REVIEW_DIR / "订单.md"
    staged.parent.mkdir(parents=True)
    staged.write_text(OBJECT_OK, encoding="utf-8")

    draft = studio.reconcile_draft("p2", env, path, OBJECT_OK.encode(), "new-revision")

    assert draft is None
    assert studio.get_draft("p2", path) is None
    assert not staged.exists()


def test_reconcile_draft_marks_old_baseline_without_deleting_content(env):
    path = "ontology/object/订单.md"
    studio.save_draft("p2", path, "未应用草稿", "old-revision", "alice")

    draft = studio.reconcile_draft("p2", env, path, b"new workspace content", "new-revision")

    assert draft["state"] == "stale"
    assert draft["content"] == "未应用草稿"
    assert studio.get_draft("p2", path)["content"] == "未应用草稿"
    assert studio.get_active_draft("p2", path) is None
    assert studio.draft_paths("p2") == []  # 失效的旧草稿不挡版本回退

    restored = studio.reconcile_draft(
        "p2", env, path, b"restored workspace content", "old-revision"
    )
    assert restored["state"] == "active"
    assert studio.get_active_draft("p2", path)["state"] == "active"


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
    # F3：链接只提示
    assert any("不存在的对象" in r for r in checks["hints"])
    assert not any("不存在的对象" in r for r in checks["review"])


def test_deterministic_check_does_not_report_unchanged_existing_extra_sections(env):
    path = env / "ontology" / "object" / "订单.md"
    baseline = OBJECT_OK + "\n# 业务备注\n\n合成备注\n\n# 历史说明\n\n合成说明\n"
    path.write_text(baseline, encoding="utf-8")
    draft = baseline.replace("订单是交易的核心record。", "订单是交易的核心合成记录。")

    checks = studio.deterministic_check(env, "ontology/object/订单.md", draft)

    assert checks["errors"] == []
    assert not any("业务备注" in item or "历史说明" in item for item in checks["review"])


def test_deterministic_check_reports_changed_existing_extra_section(env):
    path = env / "ontology" / "object" / "订单.md"
    baseline = OBJECT_OK + "\n# 业务备注\n\n合成备注\n"
    path.write_text(baseline, encoding="utf-8")

    checks = studio.deterministic_check(
        env,
        "ontology/object/订单.md",
        baseline.replace("合成备注", "另一合成备注"),
    )

    assert "修改了模板外的章节「业务备注」" in checks["review"]


def test_sections_serialize_roundtrip():
    sections = studio.parse_sections(OBJECT_OK)
    rebuilt = studio.serialize_sections(sections)
    assert studio.parse_sections(rebuilt) == sections


def test_graph_projects_semantic_edges_and_rich_node_details(env):
    (env / "ontology" / "object" / "订单.md").write_text(
        OBJECT_OK,
        encoding="utf-8",
    )
    (env / "ontology" / "operation").mkdir(parents=True)
    (env / "ontology" / "operation" / "创建订单.md").write_text(
        """# 业务描述

创建并持久化订单。

# 与operation的关联

- 调用operation > [[支付订单]]

# 与object的关联

- 输入数据的对象：[[用户]]
- 输出数据的对象：[[订单]]
- 支撑数据的对象：[[规则配置]]

# 规则与指标

- 使用规则：[[订单有效性]]

# 逻辑描述

1. 读取用户信息
2. 创建订单记录
3. 调用订单计算.py
引用正文中的 [[用户]] 不应生成第二条对象关系
""",
        encoding="utf-8",
    )
    (env / "ontology" / "operation" / "支付订单.md").write_text(
        "# 业务描述\n\n完成订单支付。\n# 逻辑描述\n\n1. 校验支付信息\n",
        encoding="utf-8",
    )
    (env / "ontology" / "object" / "规则配置.md").write_text(
        "# 对象描述\n\n规则配置。\n", encoding="utf-8"
    )
    (env / "ontology" / "object" / "订单.md").write_text(
        OBJECT_OK + "\n# 属性规则\n\n派生：由 [[创建订单]] 写入；详见 [[订单数]]\n",
        encoding="utf-8",
    )
    (env / "ontology" / "rule").mkdir(parents=True)
    (env / "ontology" / "rule" / "订单有效性.md").write_text(
        "# 规则描述\n\n订单必须有效。\n# 适用对象\n\n- [[订单]]\n"
        "# 来源\n\n- [[创建订单]] 的步骤 2\n",
        encoding="utf-8",
    )
    (env / "ontology" / "object" / "订单.md").write_text(
        OBJECT_OK + "\n# 属性规则\n\n派生：由 [[创建订单]] 写入；详见 [[订单数]]、[[订单有效性]]\n",
        encoding="utf-8",
    )
    (env / "ontology" / "metric").mkdir(parents=True)
    (env / "ontology" / "metric" / "订单数.md").write_text(
        "# 指标描述\n\n统计订单数量。\n# 数据来源\n\n- 输入：[[订单]]\n- 产出方：[[创建订单]]\n",
        encoding="utf-8",
    )
    (env / "ontology" / "scripts").mkdir(parents=True)
    (env / "ontology" / "scripts" / "订单计算.py").write_text(
        '"""关联operation：创建订单\n\n功能描述：计算订单字段\n"""\n',
        encoding="utf-8",
    )

    data = studio.graph(env)
    nodes = {node["id"]: node for node in data["nodes"]}
    edges = {(edge["source"], edge["target"], edge["kind"], edge["label"])
             for edge in data["edges"]}

    assert nodes["订单"]["description"] == "订单是交易的核心record。"
    assert nodes["订单"]["fields"][0]["key"] == "id"
    assert nodes["订单"]["fields"][0]["cnName"] == "编号"
    assert nodes["创建订单"]["description"] == "创建并持久化订单。"
    assert ("订单", "用户", "object_relation", "属于") in edges
    assert ("用户", "创建订单", "operation_input_object", "输入对象") in edges
    assert ("创建订单", "订单", "operation_output_object", "输出对象") in edges
    assert ("规则配置", "创建订单", "operation_support_object", "支撑对象") in edges
    assert not any(edge[0] == "创建订单" and edge[1] == "用户" for edge in edges)
    assert not any(edge[0] == "订单" and edge[1] == "创建订单" for edge in edges)
    assert ("创建订单", "支付订单", "calls_operation", "调用操作") in edges
    assert ("订单有效性", "订单", "applies_to_object", "适用于对象") in edges
    assert ("创建订单", "订单有效性", "uses_rule", "使用规则") in edges
    assert not any(edge[0] == "订单有效性" and edge[1] == "创建订单" for edge in edges)
    assert not any(edge[0] == "订单" and edge[1] == "订单有效性" for edge in edges)
    assert ("订单数", "订单", "uses_object", "使用对象") in edges
    assert ("创建订单", "订单数", "uses_metric", "使用指标") in edges
    assert not any(edge[0] == "订单" and edge[1] == "订单数" for edge in edges)
    assert ("创建订单", "订单计算", "uses_script", "使用脚本") in edges
    assert not any(edge[0] == "订单计算" and edge[1] == "创建订单" for edge in edges)
    assert data["workingRevision"]
    assert all(edge["id"].startswith("edge-") for edge in data["edges"])


@pytest.mark.parametrize("role, kind, object_is_source", [
    ("输入数据的对象", "operation_input_object", True),
    ("输出数据的对象", "operation_output_object", False),
    ("支撑数据的对象", "operation_support_object", True),
])
@pytest.mark.parametrize("association_first", [True, False])
def test_graph_reference_dedup_is_independent_of_section_order(
    tmp_path, role, kind, object_is_source, association_first
):
    """Synthetic documents retain semantic edges and unrelated legacy references."""
    objects = tmp_path / "ontology/object"
    operations = tmp_path / "ontology/operation"
    objects.mkdir(parents=True)
    operations.mkdir(parents=True)
    for name in ("synthetic_object", "synthetic_other"):
        (objects / f"{name}.md").write_text(
            "# 对象描述\n\nsynthetic description\n", encoding="utf-8"
        )
    association = f"# 与object的关联\n\n- {role}：[[synthetic_object]]\n"
    logic = (
        "# 逻辑描述\n\n1. 使用 [[synthetic_object]]\n"
        "2. 引用 [[synthetic_other]]\n3. 再次使用 [[synthetic_object]]\n"
    )
    sections = [association, logic] if association_first else [logic, association]
    (operations / "synthetic_operation.md").write_text(
        "# 业务描述\n\nsynthetic operation\n\n" + "\n".join(sections),
        encoding="utf-8",
    )

    graph = studio.graph(tmp_path)
    source, target = (
        ("synthetic_object", "synthetic_operation") if object_is_source
        else ("synthetic_operation", "synthetic_object")
    )
    edges = graph["edges"]
    assert len(edges) == 2
    assert {(edge["source"], edge["target"], edge["kind"]) for edge in edges} == {
        (source, target, kind),
        ("synthetic_operation", "synthetic_other", "references"),
    }
    for node in graph["nodes"]:
        assert node["in"] == sum(edge["target"] == node["id"] for edge in edges)
        assert node["out"] == sum(edge["source"] == node["id"] for edge in edges)


def test_graph_drops_fallback_object_relation_when_explicit_relation_exists(env):
    (env / "ontology" / "object" / "客户.md").write_text(
        "# 对象描述\n\n客户\n# 关联对象\n\n"
        "- 当前对象 → 关联企业 → [[企业]]\n"
        "- [[企业]]\n",
        encoding="utf-8",
    )
    (env / "ontology" / "object" / "企业.md").write_text(
        "# 对象描述\n\n企业\n",
        encoding="utf-8",
    )

    edges = studio.graph(env)["edges"]

    assert [
        (edge["source"], edge["target"], edge["label"])
        for edge in edges
        if edge["source"] == "客户" and edge["target"] == "企业"
    ] == [("客户", "企业", "关联企业")]


def test_graph_keeps_each_distinct_relation_between_two_objects(env):
    def fields(*keys):
        rows = "".join(f"| {key} | {key} | 合成字段 | 否 | 字符串 |\n" for key in keys)
        return "# 对象属性清单\n\n| 中文名称 | 英文名称 | 描述 | 主键 | 类型 |\n|---|---|---|---|---|\n" + rows + "\n"

    objects = env / "ontology" / "object"
    (objects / "客户.md").write_text(
        fields("customer_id", "owner_company_id", "payer_company_id")
        + "# 关联对象\n\n"
        "- 当前对象 → 关联企业（owner_company_id → company_id） → [[企业]]\n"
        "- 当前对象 → 关联企业（payer_company_id → company_id） → [[企业]]\n"
        "- 当前对象 → 隶属 → [[企业]]\n"
        "- [[企业]]\n",
        encoding="utf-8",
    )
    (objects / "企业.md").write_text(
        fields("company_id")
        + "# 关联对象\n\n- [[客户]] → 关联企业（owner_company_id → company_id） → 当前对象\n",
        encoding="utf-8",
    )
    # 旧产物「# 文档名 + ## 章节」的声明导出不认，Studio 仍按逐行扫描显示
    (objects / "供应商.md").write_text(
        "# 供应商\n\n## 关联对象\n\n- 当前对象 → 供货给 → [[企业]]\n", encoding="utf-8"
    )

    data = studio.graph(env)
    pairs = {}
    for edge in data["edges"]:
        pairs.setdefault((edge["source"], edge["target"]), []).append(edge["label"])

    # 与导出同一身份：名称或字段不同的各成一条，同名多条在标签里注明字段；
    # 两端重复声明只出一条，裸写的 [[企业]] 给有名字的关系让位
    assert sorted(pairs.get(("客户", "企业"), [])) == [
        "关联企业（owner_company_id → company_id）",
        "关联企业（payer_company_id → company_id）",
        "隶属",
    ]
    assert ("企业", "客户") not in pairs
    assert pairs.get(("供应商", "企业")) == ["供货给"]
    nodes = {node["id"]: node for node in data["nodes"]}
    assert nodes["客户"]["out"] == 3 and nodes["企业"]["in"] == 4


def test_operation_flow_projects_numbered_logic_steps(env):
    (env / "ontology" / "operation").mkdir(parents=True)
    (env / "ontology" / "operation" / "创建订单.md").write_text(
        """# 业务描述

创建订单。

# 逻辑描述

1. 校验输入数据
2. 生成订单编号
3. 保存订单
""",
        encoding="utf-8",
    )

    flow = studio.operation_flow(env, "创建订单")

    assert flow["operationId"] == "创建订单"
    assert flow["steps"] == ["校验输入数据", "生成订单编号", "保存订单"]
    assert flow["defaultView"] == "operation-steps"
    assert flow["diagrams"] == []
    assert flow["workingRevision"]


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
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "resource-template").mkdir(parents=True)
    return WorkspaceManager(tmp_path / "data", assets)


def _legacy_trash(manager: WorkspaceManager, project_id: str) -> None:
    """模拟旧版软删除留下的存量条目（现在的删除已是永久删除，不再产生回收站条目）。"""
    manager.trash_dir().mkdir(parents=True, exist_ok=True)
    shutil.move(
        str(manager.project_root(project_id)),
        str(manager.trash_dir() / f"{project_id}-{int(time.time())}"),
    )


def test_trash_list_and_restore(manager: WorkspaceManager):
    meta = manager.create_project("待恢复")
    _legacy_trash(manager, meta["id"])
    entries = manager.list_trash()
    assert entries and entries[0]["project_name"] == "待恢复"
    restored = manager.restore_trash(entries[0]["name"])
    assert restored["name"] == "待恢复"
    assert manager.get_project(restored["id"])["name"] == "待恢复"
    assert manager.list_trash() == []


def test_restore_conflicting_id_gets_new_id(manager: WorkspaceManager):
    meta = manager.create_project("重名")
    _legacy_trash(manager, meta["id"])
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

共扫描 6 条 operation：✅ 适合 1 / ⚠️ 部分适合 1 / ❌ 不适合 1 / 📦 已生成 1

## ✅ 适合（1）

| # | 流程（生成脚本的 operation） | 覆盖的子操作 | 类型 | 输入 | 输出 | 已有脚本 |
|---|---|---|---|---|---|---|
| 1 | 订单审核 | 库存校验；折扣计算（单独脚本） | 流程编排 | `orders, level` | `conclusion, steps` | — |

## ⚠️ 部分适合（1；先拆子操作再生成）

| 逻辑 | 拆分建议 | 备注 |
|---|---|---|
| 活动归因 | 步骤1-3 拆成子操作「活动归因计算」 | 步骤4 为人工分桶 |

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
    assert parsed["summary"].startswith("共扫描 6 条")
    # 部分适合要先拆子操作，不编号、不进候选
    assert [c["id"] for c in parsed["candidates"]] == [1]
    first = parsed["candidates"][0]
    assert first["operation"] == "订单审核" and first["covers"] == "库存校验；折扣计算（单独脚本）"
    assert first["verdict"] == "适合" and first["inputs"] == "orders, level"
    assert parsed["unsuitable"][0]["operation"] == "上架审批"
    assert "、" in parsed["generated"][0]["scripts"]


def test_generic_report_not_misdetected():
    parsed = report_items.parse("## 规则检查结果\n\n- 缺少主键")
    assert "type" not in parsed


def test_report_items_skips_stat_and_pass_lines():
    from app.services import report_items

    text = (
        "## 测试设计覆盖率\n"
        "- 判断分支总数：8\n"
        "- 推演通过率：100%\n"
        "- 未覆盖分支：无\n"
        "- 场景A 主流程：pass ✅\n"
        "## 问题清单\n"
        "- `创建订单`：缺少『与operation的关联』章节\n"
    )
    parsed = report_items.parse(text)
    assert [i["text"] for i in parsed["items"]] == [
        "`创建订单`：缺少『与operation的关联』章节"
    ]
