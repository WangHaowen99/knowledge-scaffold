import json
from pathlib import Path

from ontology.ontology_catalog import find_script_entry, load_script_catalog
from skill_parser.modeling_template import rewrite_business_data_client_imports
from skill_parser.skill_ontology_parser import build_file_mapping, run


def _script_parameters(target_root: Path, operation_key: str) -> list[dict]:
    entry = find_script_entry(load_script_catalog(target_root), operation_key)
    assert entry is not None
    parameters = []
    for raw in entry.get("parameters") or []:
        parameter = dict(raw)
        if not parameter.get("multiple"):
            parameter.pop("multiple", None)
        parameters.append(parameter)
    return parameters


def _write_object(
    path: Path,
    *,
    desc: str = "物料对象描述",
    desc_title: str = "对象描述",
    alias_title: str = "对象别名",
    attributes_title: str = "对象属性清单",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"# {desc_title}",
                desc,
                "",
                f"# {alias_title}",
                "物料号,零件",
                "",
                "# 英文名称",
                "Material",
                "",
                f"# {attributes_title}",
                "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |",
                "| --- | --- | --- | --- | --- | --- | --- |",
                "| 物料编号 | material_code | 物料唯一编号 | 是 | 字符串 | 料号 | 非空 |",
                "| 物料名称 | material_name | 物料名称 | 否 | 字符串 | 名称 | 无 |",
                "",
                "# 关联对象",
                "- 当前对象 -> 包含 -> [[供应商]]",
            ]
        ),
        encoding="utf-8",
    )


def _write_logic(
    path: Path,
    *,
    desc: str = "流程描述",
    parent_relation: str = "无",
    trigger_condition: str = "用户需要查询物料供应商时触发",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = (
        [
                "# 业务描述",
                desc,
                "",
                "# 与logic的关联",
                "- 被logic调用 > {}".format(parent_relation),
                "- 调用logic > [[子流程A]]",
                "",
                "# 与object的关联",
                "- 输入数据的对象：[[物料]]",
                "- 输出数据的对象：[[供应商]]",
                "- 支撑数据的对象：无",
                "",
                "# 与object的语义关联",
                "| 业务对象 | 语义关联关系 |",
                "| --- | --- |",
                "| 物料 | 使用物料编号定位供应商 |",
                "| 供应商 | 输出命中的供应商信息 |",
                "",
                "# 触发条件",
                trigger_condition,
                "",
                "# 输入",
                "| 参数名称 | 描述 |",
                "| --- | --- |",
                "| 物料编号 | 需要查询的物料编号 |",
                "",
                "# 输出",
                "| 参数名称 | 描述 |",
                "| --- | --- |",
                "| 供应商编号 | 命中的供应商编号 |",
                "",
                "# 逻辑描述",
                "```",
                "执行 supplier_lookup 脚本",
                "python cleanup.py",
                "```",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_script_with_docstring(
    path: Path,
    *,
    related_logic: str = "供应商查询",
    body: str = "print('supplier')",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "#查数脚本（涉及mindsdb查数的脚本才会加这个标签）",
                '"""',
                f"关联logic：{related_logic}",
                "",
                "功能描述：根据物料编号查询供应商。",
                "",
                "用法：",
                "    CLI    ：python3 supplier_lookup.py MAT-001",
                "",
                "输入参数：",
                "    物料编号    字符串    必填，待查询的物料编号",
                "    最大数量    整型    可选，默认10，最多返回的供应商数量",
                "",
                "输出：",
                "    return {supplier_code: str}",
                '"""',
                "",
                body,
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_build_file_mapping_uses_new_modeling_layout(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(source_root / "logic" / "供应商查询.md")
    (source_root / "scripts" / "supplier_lookup.py").parent.mkdir(parents=True, exist_ok=True)
    (source_root / "scripts" / "supplier_lookup.py").write_text("print('ok')\n", encoding="utf-8")
    (source_root / "action" / "legacy.py").parent.mkdir(parents=True, exist_ok=True)
    (source_root / "action" / "legacy.py").write_text("print('legacy')\n", encoding="utf-8")
    (source_root / "asset" / "common.conf").parent.mkdir(parents=True, exist_ok=True)
    (source_root / "asset" / "common.conf").write_text("threshold=3\n", encoding="utf-8")
    (source_root / "SKILL.md").write_text("---\nid: demo\nname: Demo\n---\n", encoding="utf-8")

    mapping = build_file_mapping(source_root, target_root)

    assert mapping["objects"] == [{"source": "object/物料.md", "target": "objects/物料.md"}]
    assert mapping["operations"] == [{"source": "logic/供应商查询.md", "target": "operations/供应商查询.md"}]
    assert mapping["scripts"] == [{"source": "scripts/supplier_lookup.py", "target": "scripts/supplier_lookup.py"}]
    assert mapping["assets"] == [{"source": "asset/common.conf", "target": "assets/common.conf"}]
    assert mapping["warnings"] == []


def test_build_file_mapping_falls_back_to_legacy_action_layout(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(source_root / "logic" / "供应商查询.md")
    (source_root / "action" / "supplier_lookup.py").parent.mkdir(parents=True, exist_ok=True)
    (source_root / "action" / "supplier_lookup.py").write_text("print('ok')\n", encoding="utf-8")

    mapping = build_file_mapping(source_root, target_root)

    assert mapping["scripts"] == [{"source": "action/supplier_lookup.py", "target": "scripts/supplier_lookup.py"}]
    assert mapping["scripts_index"]["generator"] == "from action/**/*.py"


def test_run_generates_agent_ontology_from_new_modeling_template(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "制造域" / "物料.md")
    _write_object(source_root / "object" / "制造域" / "供应商.md", desc="供应商对象描述")
    _write_logic(source_root / "logic" / "供应商查询.md", desc="根据物料查询供应商")
    _write_logic(
        source_root / "logic" / "子流程A.md",
        desc="子流程描述",
        parent_relation="[[供应商查询]]，前置流程",
    )
    _write_script_with_docstring(source_root / "scripts" / "tools" / "supplier_lookup.py")
    _write_script_with_docstring(
        source_root / "scripts" / "nested" / "cleanup.py",
        related_logic="无",
        body="print('cleanup')",
    )
    (source_root / "scripts" / "nested" / "config.json").write_text(
        '{"mode":"cleanup"}\n',
        encoding="utf-8",
    )
    (source_root / "asset" / "common.conf").parent.mkdir(parents=True, exist_ok=True)
    (source_root / "asset" / "common.conf").write_text("threshold=3\n", encoding="utf-8")
    (source_root / "asset" / "rules" / "limits.ini").parent.mkdir(parents=True, exist_ok=True)
    (source_root / "asset" / "rules" / "limits.ini").write_text("[limits]\nmax=10\n", encoding="utf-8")

    result = run(source_root=source_root, target_root=target_root)

    objects_index = (target_root / "objects" / "index.md").read_text(encoding="utf-8")
    assert "| 中文名称 | 英文名称 | 别名 | 业务定义 | 领域分类 |" in objects_index
    assert "| 物料 | Material | 物料号、零件 | 物料对象描述 |  |" in objects_index
    assert result["objects_index"]["columns"] == ["中文名称", "英文名称", "别名", "业务定义", "领域分类"]

    object_doc = (target_root / "objects" / "物料.md").read_text(encoding="utf-8")
    assert "resource: object" in object_doc
    assert "# 实体属性清单" in object_doc
    assert "| 中文名称 | 英文名称 | 描述 | 属性别名 |" in object_doc
    assert "| 物料编号 | material_code | 物料唯一编号 | 料号 |" in object_doc
    assert "主键" not in object_doc
    assert "属性规则" not in object_doc

    operations_index = (target_root / "operations" / "index.md").read_text(encoding="utf-8")
    assert "| 名称 | 标签 | 业务描述 | 触发条件 |" in operations_index
    assert "| 供应商查询 | 工具流程 | 根据物料查询供应商 | 用户需要查询物料供应商时触发 |" in operations_index
    assert "| 子流程A | 子流程 | 子流程描述 | 用户需要查询物料供应商时触发 |" in operations_index

    operation_doc = (target_root / "operations" / "供应商查询.md").read_text(encoding="utf-8")
    assert "# 管理标签\n#工具流程" in operation_doc
    assert "| 参数名称 | 描述 | 输入源 |" in operation_doc
    assert "| 物料编号 | 需要查询的物料编号 | 父流程传递 |" in operation_doc
    assert "| 供应商编号 | 命中的供应商编号 |" in operation_doc
    assert "# 业务动作关联" not in operation_doc
    assert "# 与object的关联" not in operation_doc

    sub_operation_doc = (target_root / "operations" / "子流程A.md").read_text(encoding="utf-8")
    assert "| 物料编号 | 需要查询的物料编号 | 父流程传递 |" in sub_operation_doc

    scripts_index = (target_root / "scripts" / "index.md").read_text(encoding="utf-8")
    script_descriptor = (target_root / "scripts" / "supplier_lookup.md").read_text(encoding="utf-8")
    assert "| supplier_lookup | supplier_lookup | 供应商查询 | tools/supplier_lookup.py |" in scripts_index
    assert "bound_operation: 供应商查询" in script_descriptor
    assert "cleanup" not in scripts_index
    assert "关联logic：供应商查询" in (
        target_root / "scripts" / "tools" / "supplier_lookup.py"
    ).read_text(encoding="utf-8")
    assert "print('cleanup')" in (target_root / "scripts" / "nested" / "cleanup.py").read_text(encoding="utf-8")
    assert (target_root / "scripts" / "nested" / "config.json").read_text(encoding="utf-8") == '{"mode":"cleanup"}\n'
    assert not (target_root / "scripts" / "cleanup.py").exists()
    assert not (target_root / "operations" / "supplier_lookup.md").exists()
    assert (target_root / "assets" / "common.conf").read_text(encoding="utf-8") == "threshold=3\n"
    assert (target_root / "assets" / "rules" / "limits.ini").read_text(encoding="utf-8") == "[limits]\nmax=10\n"
    assert (target_root / "rules" / "index.md").is_file()
    assert (target_root / "metrics" / "index.md").is_file()
    assert (target_root / "mappings" / "object-data-mappings.json").is_file()
    assert not list(target_root.glob("*-list.md"))
    assert not (target_root / "ONTOLOGY.md").exists()

    assert result["objects_trimmed"]["count"] == 2
    assert result["operations_count"] == 2
    assert result["scripts"]["count"] == 3
    assert result["assets"]["count"] == 2
    assert result["graph"]["nodes_count"] == 4
    assert result["graph"]["edges_count"] == 4

    graph_nodes = json.loads((target_root / "graph" / "nodes.json").read_text(encoding="utf-8"))
    graph_edges = json.loads((target_root / "graph" / "edges.json").read_text(encoding="utf-8"))
    assert {"id": "物料", "type": "entity", "description": "物料对象描述", "source": "objects/物料.md"} in graph_nodes
    assert {"id": "供应商查询", "type": "tool", "description": "根据物料查询供应商", "source": "operations/供应商查询.md"} in graph_nodes
    assert {
        "source": "供应商查询",
        "relation": "使用物料编号定位供应商",
        "target": "物料",
        "cardinality": "",
        "domain": "logic-object",
        "description": "供应商查询 与 物料 的语义关联：使用物料编号定位供应商",
    } in graph_edges


def test_run_reads_compatible_entity_titles_for_object_summary(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(
        source_root / "object" / "物料.md",
        desc="实体标题下的描述",
        desc_title="实体描述",
        alias_title="实体别名",
        attributes_title="实体属性清单",
    )

    result = run(source_root=source_root, target_root=target_root)

    objects_index = (target_root / "objects" / "index.md").read_text(encoding="utf-8")
    assert "| 物料 | Material | 物料号、零件 | 实体标题下的描述 |  |" in objects_index
    object_doc = (target_root / "objects" / "物料.md").read_text(encoding="utf-8")
    assert "| 物料编号 | material_code | 物料唯一编号 | 料号 |" in object_doc
    assert "未找到对象属性清单" not in "\n".join(result["objects_trimmed"]["warnings"])


def test_run_infers_operation_tags_from_new_modeling_template_markers(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(source_root / "logic" / "主流程.md", desc="主流程描述")
    _write_logic(
        source_root / "logic" / "子流程.md",
        desc="子流程描述",
        parent_relation="[[主流程]]，前置流程",
    )
    _write_logic(
        source_root / "logic" / "查数工具.md",
        desc="查数工具描述",
    )
    _write_logic(
        source_root / "logic" / "说明主流程.md",
        desc="带说明的主流程描述",
        parent_relation="无（由用户直接触发）",
    )

    run(source_root=source_root, target_root=target_root)

    operations_index = (target_root / "operations" / "index.md").read_text(encoding="utf-8")
    assert "| 主流程 | 主流程 | 主流程描述 | 用户需要查询物料供应商时触发 |" in operations_index
    assert "| 子流程 | 子流程 | 子流程描述 | 用户需要查询物料供应商时触发 |" in operations_index
    assert "| 查数工具 | 主流程 | 查数工具描述 | 用户需要查询物料供应商时触发 |" in operations_index
    assert "| 说明主流程 | 主流程 | 带说明的主流程描述 | 用户需要查询物料供应商时触发 |" in operations_index

    main_doc = (target_root / "operations" / "主流程.md").read_text(encoding="utf-8")
    sub_doc = (target_root / "operations" / "子流程.md").read_text(encoding="utf-8")
    tool_doc = (target_root / "operations" / "查数工具.md").read_text(encoding="utf-8")
    explained_main_doc = (target_root / "operations" / "说明主流程.md").read_text(encoding="utf-8")
    assert "# 管理标签\n#主流程" in main_doc
    assert "# 管理标签\n#子流程" in sub_doc
    assert "# 管理标签\n#主流程" in tool_doc
    assert "# 管理标签\n#主流程" in explained_main_doc
    assert "| 物料编号 | 需要查询的物料编号 | 用户输入 |" in main_doc
    assert "| 物料编号 | 需要查询的物料编号 | 父流程传递 |" in sub_doc
    assert "| 物料编号 | 需要查询的物料编号 | 用户输入 |" in tool_doc


def test_run_preserves_nested_headings_inside_logic_description(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(
        source_root / "logic" / "嵌套标题流程.md",
        desc="包含二级标题的流程",
    )
    logic_path = source_root / "logic" / "嵌套标题流程.md"
    content = logic_path.read_text(encoding="utf-8")
    logic_path.write_text(
        content.replace(
            "# 逻辑描述\n```\n执行 supplier_lookup 脚本\npython cleanup.py\n```",
            "\n".join([
                "# 逻辑描述",
                "## 执行步骤",
                "- 步骤1：结构化解析",
                "## 规则说明",
                "- 规则1：保留二级标题内容",
            ]),
        ),
        encoding="utf-8",
    )

    run(source_root=source_root, target_root=target_root)

    operation_doc = (target_root / "operations" / "嵌套标题流程.md").read_text(encoding="utf-8")
    assert "## 执行步骤" in operation_doc
    assert "- 步骤1：结构化解析" in operation_doc
    assert "## 规则说明" in operation_doc
    assert "- 规则1：保留二级标题内容" in operation_doc


def test_run_uses_script_docstring_relation_and_skips_private_helpers(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(
        source_root / "logic" / "供应商查询.md",
        desc="根据物料查询供应商",
    )
    scripts_dir = source_root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    _write_script_with_docstring(scripts_dir / "supplier_lookup.py")
    (scripts_dir / "_script_common.py").write_text(
        "def helper():\n    return 'ok'\n",
        encoding="utf-8",
    )

    run(source_root=source_root, target_root=target_root)

    scripts_index = (target_root / "scripts" / "index.md").read_text(encoding="utf-8")
    assert "| supplier_lookup | supplier_lookup | 供应商查询 | supplier_lookup.py |" in scripts_index
    assert "_script_common" not in scripts_index
    assert "关联logic：供应商查询" in (target_root / "scripts" / "supplier_lookup.py").read_text(encoding="utf-8")
    assert (target_root / "scripts" / "_script_common.py").exists()


def test_run_rewrites_generated_scripts_to_provider_aware_client(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(
        source_root / "logic" / "供应商查询.md",
        desc="根据物料查询供应商",
    )
    scripts_dir = source_root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "supplier_lookup.py").write_text(
        "\n".join(
            [
                '"""',
                "关联logic：供应商查询",
                "输入参数：",
                "    物料编号    字符串    必填，待查询的物料编号",
                '"""',
                "from agent.tools.business_data.providers.clients.mindsdb import MindsDBClient",
                "",
                "client = MindsDBClient()",
                "print(client.query_sql('SELECT 1'))",
            ]
        ),
        encoding="utf-8",
    )

    run(source_root=source_root, target_root=target_root)

    generated_script = (target_root / "scripts" / "supplier_lookup.py").read_text(encoding="utf-8")
    assert "from agent.tools.business_data.providers.clients.data_provider_client import DataProviderClient" in generated_script
    assert "from agent.tools.business_data.providers.clients.mindsdb import MindsDBClient" not in generated_script
    assert "client = DataProviderClient()" in generated_script


def test_rewrite_business_data_client_imports_handles_python_code_tokens():
    source = "\n".join(
        [
            "from agent.tools.business_data.providers.clients.mindsdb import MindsDBClient",
            "",
            "client = MindsDBClient()",
            "def use_client(value: MindsDBClient) -> MindsDBClient:",
            "    return value",
            "if isinstance(client, MindsDBClient):",
            "    pass",
            "class WrappedClient(MindsDBClient):",
            "    pass",
            "# client = MindsDBClient()",
            "text = 'MindsDBClient() in docs'",
            "",
        ]
    )

    rewritten = rewrite_business_data_client_imports(source)

    assert "from agent.tools.business_data.providers.clients.data_provider_client import DataProviderClient" in rewritten
    assert "client = DataProviderClient()" in rewritten
    assert "def use_client(value: DataProviderClient) -> DataProviderClient:" in rewritten
    assert "isinstance(client, DataProviderClient)" in rewritten
    assert "class WrappedClient(DataProviderClient):" in rewritten
    assert "# client = MindsDBClient()" in rewritten
    assert "text = 'MindsDBClient() in docs'" in rewritten


def test_rewrite_business_data_client_imports_preserves_alias_imports():
    source = "\n".join(
        [
            "from agent.tools.business_data.providers.clients.mindsdb import MindsDBClient as MDB",
            "",
            "client = MDB()",
            "",
        ]
    )

    rewritten = rewrite_business_data_client_imports(source)

    assert "from agent.tools.business_data.providers.clients.data_provider_client import DataProviderClient as MDB" in rewritten
    assert "client = MDB()" in rewritten


def test_rewrite_business_data_client_imports_ignores_text_without_import():
    source = "\n".join(
        [
            "# client = MindsDBClient()",
            "text = 'MindsDBClient() in docs'",
            "client = MindsDBClient()",
            "",
        ]
    )

    assert rewrite_business_data_client_imports(source) == source


def test_run_copies_script_env_files_without_registering_them(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(
        source_root / "logic" / "供应商查询.md",
        desc="根据物料查询供应商",
    )
    scripts_dir = source_root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    _write_script_with_docstring(scripts_dir / "supplier_lookup.py")
    (scripts_dir / ".env").write_text(
        "API_TOKEN=demo-token\n",
        encoding="utf-8",
    )

    result = run(source_root=source_root, target_root=target_root)

    scripts_index = (target_root / "scripts" / "index.md").read_text(encoding="utf-8")
    assert "| supplier_lookup | supplier_lookup | 供应商查询 | supplier_lookup.py |" in scripts_index
    assert ".env" not in scripts_index
    assert (target_root / "scripts" / ".env").read_text(encoding="utf-8") == "API_TOKEN=demo-token\n"
    assert str(target_root / "scripts" / ".env") in result["scripts"]["files"]


def test_run_does_not_register_docstring_only_input_parameters(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(
        source_root / "logic" / "供应商查询.md",
        desc="根据物料查询供应商",
    )
    _write_script_with_docstring(source_root / "scripts" / "supplier_lookup.py")

    run(source_root=source_root, target_root=target_root)

    script_descriptor = (target_root / "scripts" / "supplier_lookup.md").read_text(encoding="utf-8")
    assert "bound_operation: 供应商查询" in script_descriptor
    assert "| 物料编号 |" not in script_descriptor


def test_run_merges_docstring_business_names_with_cli_flags(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "报修单.md")
    _write_logic(
        source_root / "logic" / "故障报修闭环.md",
        desc="根据报修渠道处理故障报修",
    )
    scripts_dir = source_root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "fault_repair_closed_loop.py").write_text(
        "\n".join([
            '"""',
            "关联logic：故障报修闭环",
            "功能描述：处理故障报修闭环。",
            "",
            "用法：",
            "    CLI    ：python3 fault_repair_closed_loop.py --report_source 400电话 --ticket-id R001",
            "",
            "输入参数：",
            "    report_source    字符串    必填，报修渠道(400电话 / 微信 / APP)",
            "    ticket_id    字符串    报修单号",
            "    equipment_data    dict    设备对象数据，默认从数据层读取",
            "",
            "输出：",
            "    return {status: str}",
            '"""',
            "import argparse",
            "",
            "def parse_args():",
            "    parser = argparse.ArgumentParser()",
            "    parser.add_argument('--report_source', required=True, help='报修渠道(400电话 / 微信 / APP)')",
            "    parser.add_argument('--ticket-id', required=True, help='报修单号')",
            "    return parser.parse_args()",
        ]),
        encoding="utf-8",
    )

    run(source_root=source_root, target_root=target_root)

    assert _script_parameters(target_root, "故障报修闭环") == [
        {
            "name": "报修渠道",
            "flag": "--report_source",
            "description": "必填，报修渠道(400电话 / 微信 / APP)",
            "required": True,
            "type": "string",
            "aliases": ["report_source"],
        },
        {
            "name": "报修单号",
            "flag": "--ticket-id",
            "description": "报修单号",
            "required": True,
            "type": "string",
            "aliases": ["ticket_id"],
        },
    ]


def test_run_reads_docstring_usage_parameter_flags_without_argparse(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "报修单.md")
    _write_logic(
        source_root / "logic" / "故障派单.md",
        desc="根据报修单号派单",
    )
    scripts_dir = source_root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "dispatch_ticket.py").write_text(
        "\n".join([
            '"""',
            "关联logic：故障派单",
            "功能描述：根据报修单号派单。",
            "",
            "用法参数：",
            "    python3 dispatch_ticket.py --ticket-id R001 --dispatch_group A组",
            "",
            "输入参数：",
            "    ticket_id    字符串    报修单号",
            "    dispatch_group    字符串    派单班组",
            "",
            "输出：",
            "    return {status: str}",
            '"""',
            "",
            "def run(ticket_id: str, dispatch_group: str) -> dict:",
            "    return {'status': 'ok'}",
        ]),
        encoding="utf-8",
    )

    run(source_root=source_root, target_root=target_root)

    assert _script_parameters(target_root, "故障派单") == [
        {
            "name": "报修单号",
            "flag": "--ticket-id",
            "description": "报修单号",
            "required": True,
            "type": "string",
            "aliases": ["ticket_id"],
        },
        {
            "name": "派单班组",
            "flag": "--dispatch_group",
            "description": "派单班组",
            "required": True,
            "type": "string",
            "aliases": ["dispatch_group"],
        },
    ]


def test_run_ignores_script_without_related_logic_for_catalog(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(
        source_root / "logic" / "供应商查询.md",
        desc="根据物料查询供应商",
    )
    _write_script_with_docstring(source_root / "scripts" / "supplier_lookup.py", related_logic="无")

    result = run(source_root=source_root, target_root=target_root)

    assert (target_root / "scripts" / "index.md").is_file()
    assert not (target_root / "scripts" / "supplier_lookup.md").exists()
    assert result["scripts"]["warnings"] == []


def test_run_falls_back_to_argparse_parameters(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(
        source_root / "logic" / "安装判断.md",
        desc="判断物料是否安装",
    )
    scripts_dir = source_root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "check_install.py").write_text(
        "\n".join([
            '"""',
            "关联logic：安装判断",
            "功能描述：判断物料是否安装。",
            '"""',
            "import argparse",
            "",
            "def parse_args():",
            "    parser = argparse.ArgumentParser()",
            "    parser.add_argument('--target_item-code', required=True, help='TargetItem编码')",
            "    parser.add_argument('--batch-code', action='append', required=True, help='在制设备批次，可重复传入多个')",
            "    parser.add_argument('--quantity', type=float, help='需求数量')",
            "    parser.add_argument('--equipment-model', help='Optional 设备型号 for STRUCTURE_LIST existence check')",
            "    return parser.parse_args()",
        ]),
        encoding="utf-8",
    )

    run(source_root=source_root, target_root=target_root)

    assert _script_parameters(target_root, "安装判断") == [
        {
            "name": "TargetItem编码",
            "flag": "--target_item-code",
            "description": "TargetItem编码",
            "required": True,
            "type": "string",
            "aliases": ["target_item_code"],
        },
        {
            "name": "在制设备批次",
            "flag": "--batch-code",
            "description": "在制设备批次，可重复传入多个",
            "required": True,
            "type": "string",
            "multiple": True,
            "aliases": ["batch_code"],
        },
        {
            "name": "需求数量",
            "flag": "--quantity",
            "description": "需求数量",
            "required": False,
            "type": "number",
            "aliases": ["quantity"],
        },
        {
            "name": "设备型号",
            "flag": "--equipment-model",
            "description": "Optional 设备型号 for STRUCTURE_LIST existence check",
            "required": False,
            "type": "string",
            "aliases": ["equipment_model"],
        },
    ]


def test_run_uses_script_mapping_comment_and_skips_private_helpers(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(
        source_root / "logic" / "供应商查询.md",
        desc="根据物料查询供应商",
    )
    action_dir = source_root / "action"
    action_dir.mkdir(parents=True, exist_ok=True)
    (action_dir / "supplier_lookup.py").write_text(
        "# 工具流程: 供应商查询\nprint('supplier')\n",
        encoding="utf-8",
    )
    (action_dir / "_script_common.py").write_text(
        "def helper():\n    return 'ok'\n",
        encoding="utf-8",
    )

    run(source_root=source_root, target_root=target_root)

    scripts_index = (target_root / "scripts" / "index.md").read_text(encoding="utf-8")
    assert "| supplier_lookup | supplier_lookup | 供应商查询 | supplier_lookup.py |" in scripts_index
    assert "_script_common" not in scripts_index
    assert (target_root / "scripts" / "supplier_lookup.py").read_text(
        encoding="utf-8"
    ).startswith("# 工具流程: 供应商查询")
    assert (target_root / "scripts" / "_script_common.py").exists()


def test_run_copies_action_env_files_without_registering_them(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(
        source_root / "logic" / "供应商查询.md",
        desc="根据物料查询供应商",
    )
    action_dir = source_root / "action"
    action_dir.mkdir(parents=True, exist_ok=True)
    (action_dir / "supplier_lookup.py").write_text(
        "# 工具流程: 供应商查询\nprint('supplier')\n",
        encoding="utf-8",
    )
    (action_dir / ".env").write_text("API_TOKEN=demo-token\n", encoding="utf-8")

    result = run(source_root=source_root, target_root=target_root)

    scripts_index = (target_root / "scripts" / "index.md").read_text(encoding="utf-8")
    assert "| supplier_lookup | supplier_lookup | 供应商查询 | supplier_lookup.py |" in scripts_index
    assert ".env" not in scripts_index
    assert (target_root / "scripts" / ".env").read_text(encoding="utf-8") == "API_TOKEN=demo-token\n"
    assert str(target_root / "scripts" / ".env") in result["scripts"]["files"]


def test_run_uses_structured_script_parameter_comment(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(
        source_root / "logic" / "供应商查询.md",
        desc="根据物料查询供应商",
    )
    action_dir = source_root / "action"
    action_dir.mkdir(parents=True, exist_ok=True)
    (action_dir / "supplier_lookup.py").write_text(
        "\n".join(
            [
                "# 工具流程: 供应商查询",
                '# parameters: [{"name":"物料编号","flag":"--material-code","description":"物料编号","required":true,"type":"string","aliases":["material_code"]}]',
                "print('supplier')",
            ]
        ),
        encoding="utf-8",
    )

    run(source_root=source_root, target_root=target_root)

    assert _script_parameters(target_root, "供应商查询") == [
        {
            "name": "物料编号",
            "flag": "--material-code",
            "description": "物料编号",
            "required": True,
            "type": "string",
            "aliases": ["material_code"],
        }
    ]


def test_run_uses_yaml_script_parameter_comment_block(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(
        source_root / "logic" / "供应商查询.md",
        desc="根据物料查询供应商",
    )
    action_dir = source_root / "action"
    action_dir.mkdir(parents=True, exist_ok=True)
    (action_dir / "supplier_lookup.py").write_text(
        "\n".join(
            [
                "# 工具流程: 供应商查询",
                "# 参数:",
                "# - name: 物料编号",
                "#   flag: --material-code",
                "#   description: 物料编号",
                "#   required: true",
                "#   type: string",
                "#查数脚本 （涉及mindsdb查数的脚本会加这个标签）",
                "print('supplier')",
            ]
        ),
        encoding="utf-8",
    )

    result = run(source_root=source_root, target_root=target_root)

    assert result["scripts"]["warnings"] == []
    assert _script_parameters(target_root, "供应商查询") == [
        {
            "name": "物料编号",
            "flag": "--material-code",
            "description": "物料编号",
            "required": True,
            "type": "string",
            "aliases": ["material_code"],
        }
    ]
