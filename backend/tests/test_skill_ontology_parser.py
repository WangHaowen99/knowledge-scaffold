import json
import re
from pathlib import Path

from skill_parser.modeling_template import rewrite_business_data_client_imports
from skill_parser.skill_ontology_parser import build_file_mapping, run


def _script_parameters(scripts_list: str, operation_key: str) -> list[dict]:
    match = re.search(
        r"- `{}` -> `[^`]+`\n  `parameters`: `([^`]+)`".format(re.escape(operation_key)),
        scripts_list,
    )
    assert match is not None
    return json.loads(match.group(1))


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
    is_tool_flow: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if is_tool_flow:
        lines.extend([
            "#工具流程 （部分计算类/请求接口类的流程会加这个标签）",
            "",
        ])
    lines.extend(
        [
                "# 业务描述",
                desc,
                "",
                "# 与logic的关联",
                "- 被流程使用 > {}".format(parent_relation),
                "- 使用流程 > [[子流程A]]",
                "",
                "# 与object的关联",
                "- 输入数据的对象：[[物料]]",
                "- 输出数据的对象：[[供应商]]",
                "- 支撑数据的对象：无",
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


def test_build_file_mapping_uses_new_modeling_layout(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(source_root / "logic" / "供应商查询.md")
    (source_root / "action" / "supplier_lookup.py").parent.mkdir(parents=True, exist_ok=True)
    (source_root / "action" / "supplier_lookup.py").write_text("print('ok')\n", encoding="utf-8")
    (source_root / "SKILL.md").write_text("---\nid: demo\nname: Demo\n---\n", encoding="utf-8")

    mapping = build_file_mapping(source_root, target_root)

    assert mapping["objects"] == [{"source": "object/物料.md", "target": "objects/物料.md"}]
    assert mapping["operations"] == [{"source": "logic/供应商查询.md", "target": "operations/供应商查询.md"}]
    assert mapping["scripts"] == [{"source": "action/supplier_lookup.py", "target": "scripts/supplier_lookup.py"}]
    assert "assets" not in mapping
    assert mapping["warnings"] == []


def test_run_generates_agent_ontology_from_new_modeling_template(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "制造域" / "物料.md")
    _write_logic(source_root / "logic" / "供应商查询.md", desc="根据物料查询供应商")
    _write_logic(
        source_root / "logic" / "子流程A.md",
        desc="子流程描述",
        parent_relation="[[供应商查询]]，前置流程",
    )
    (source_root / "action" / "supplier_lookup.py").parent.mkdir(parents=True, exist_ok=True)
    (source_root / "action" / "supplier_lookup.py").write_text("print('supplier')\n", encoding="utf-8")
    (source_root / "action" / "nested" / "cleanup.py").parent.mkdir(parents=True, exist_ok=True)
    (source_root / "action" / "nested" / "cleanup.py").write_text("print('cleanup')\n", encoding="utf-8")

    result = run(source_root=source_root, target_root=target_root)

    objects_list = (target_root / "objects-list.md").read_text(encoding="utf-8")
    assert "| 中文名称 | 别名 | 业务定义 | 领域分类 |" in objects_list
    assert "| 物料 | 物料号、零件 | 物料对象描述 |  |" in objects_list

    object_doc = (target_root / "objects" / "物料.md").read_text(encoding="utf-8")
    assert object_doc.startswith("# 实体属性清单")
    assert "| 中文名称 | 英文名称 | 描述 | 属性别名 |" in object_doc
    assert "| 物料编号 | material_code | 物料唯一编号 | 料号 |" in object_doc
    assert "主键" not in object_doc
    assert "属性规则" not in object_doc

    operations_list = (target_root / "operations-list.md").read_text(encoding="utf-8")
    assert "| 供应商查询 | 主流程 | 根据物料查询供应商 |" in operations_list
    assert "| 子流程A | 子流程 | 子流程描述 |" in operations_list

    operation_doc = (target_root / "operations" / "供应商查询.md").read_text(encoding="utf-8")
    assert "# 管理标签\n#主流程" in operation_doc
    assert "| 参数名称 | 描述 | 输入源 |" in operation_doc
    assert "| 物料编号 | 需要查询的物料编号 | 用户输入 |" in operation_doc
    assert "| 供应商编号 | 命中的供应商编号 |" in operation_doc
    assert "# 业务动作关联" not in operation_doc
    assert "# 与object的关联" not in operation_doc

    sub_operation_doc = (target_root / "operations" / "子流程A.md").read_text(encoding="utf-8")
    assert "| 物料编号 | 需要查询的物料编号 | 父流程传递 |" in sub_operation_doc

    scripts_list = (target_root / "scripts-list.md").read_text(encoding="utf-8")
    assert "- `supplier_lookup` -> `scripts/supplier_lookup.py`" in scripts_list
    assert "- `cleanup` -> `scripts/cleanup.py`" in scripts_list
    assert (target_root / "scripts" / "supplier_lookup.py").read_text(encoding="utf-8") == "print('supplier')\n"
    assert (target_root / "scripts" / "cleanup.py").read_text(encoding="utf-8") == "print('cleanup')\n"
    assert not (target_root / "operations" / "supplier_lookup.md").exists()
    assert not (target_root / "assets").exists()

    assert result["objects_trimmed"]["count"] == 1
    assert result["operations_count"] == 2
    assert result["scripts"]["count"] == 2
    assert "assets" not in result


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

    objects_list = (target_root / "objects-list.md").read_text(encoding="utf-8")
    assert "| 物料 | 物料号、零件 | 实体标题下的描述 |  |" in objects_list
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
        is_tool_flow=True,
    )

    run(source_root=source_root, target_root=target_root)

    operations_list = (target_root / "operations-list.md").read_text(encoding="utf-8")
    assert "| 主流程 | 主流程 | 主流程描述 |" in operations_list
    assert "| 子流程 | 子流程 | 子流程描述 |" in operations_list
    assert "| 查数工具 | 工具流程 | 查数工具描述 |" in operations_list

    main_doc = (target_root / "operations" / "主流程.md").read_text(encoding="utf-8")
    sub_doc = (target_root / "operations" / "子流程.md").read_text(encoding="utf-8")
    tool_doc = (target_root / "operations" / "查数工具.md").read_text(encoding="utf-8")
    assert "# 管理标签\n#主流程" in main_doc
    assert "# 管理标签\n#子流程" in sub_doc
    assert "# 管理标签\n#工具流程" in tool_doc
    assert "| 物料编号 | 需要查询的物料编号 | 用户输入 |" in main_doc
    assert "| 物料编号 | 需要查询的物料编号 | 父流程传递 |" in sub_doc
    assert "| 物料编号 | 需要查询的物料编号 | 父流程传递 |" in tool_doc


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


def test_run_uses_script_mapping_comment_and_skips_private_helpers(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(
        source_root / "logic" / "供应商查询.md",
        desc="根据物料查询供应商",
        is_tool_flow=True,
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

    scripts_list = (target_root / "scripts-list.md").read_text(encoding="utf-8")
    assert "- `供应商查询` -> `scripts/supplier_lookup.py`" in scripts_list
    assert "`supplier_lookup`" not in scripts_list
    assert "`_script_common`" not in scripts_list
    assert (target_root / "scripts" / "supplier_lookup.py").read_text(encoding="utf-8").startswith("# 工具流程: 供应商查询")
    assert (target_root / "scripts" / "_script_common.py").exists()


def test_run_rewrites_generated_scripts_to_provider_aware_client(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(
        source_root / "logic" / "供应商查询.md",
        desc="根据物料查询供应商",
        is_tool_flow=True,
    )
    action_dir = source_root / "action"
    action_dir.mkdir(parents=True, exist_ok=True)
    (action_dir / "supplier_lookup.py").write_text(
        "\n".join(
            [
                "# 工具流程: 供应商查询",
                "from agentic.services.mindsdb import MindsDBClient",
                "",
                "client = MindsDBClient()",
                "print(client.query_sql('SELECT 1'))",
            ]
        ),
        encoding="utf-8",
    )

    run(source_root=source_root, target_root=target_root)

    generated_script = (target_root / "scripts" / "supplier_lookup.py").read_text(encoding="utf-8")
    assert "from agentic.services.data_provider_client import DataProviderClient" in generated_script
    assert "from agentic.services.mindsdb import MindsDBClient" not in generated_script
    assert "client = DataProviderClient()" in generated_script


def test_rewrite_business_data_client_imports_handles_python_code_tokens():
    source = "\n".join(
        [
            "from agentic.services.mindsdb import MindsDBClient",
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

    assert "from agentic.services.data_provider_client import DataProviderClient" in rewritten
    assert "client = DataProviderClient()" in rewritten
    assert "def use_client(value: DataProviderClient) -> DataProviderClient:" in rewritten
    assert "isinstance(client, DataProviderClient)" in rewritten
    assert "class WrappedClient(DataProviderClient):" in rewritten
    assert "# client = MindsDBClient()" in rewritten
    assert "text = 'MindsDBClient() in docs'" in rewritten


def test_rewrite_business_data_client_imports_preserves_alias_imports():
    source = "\n".join(
        [
            "from agentic.services.mindsdb import MindsDBClient as MDB",
            "",
            "client = MDB()",
            "",
        ]
    )

    rewritten = rewrite_business_data_client_imports(source)

    assert "from agentic.services.data_provider_client import DataProviderClient as MDB" in rewritten
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


def test_run_copies_action_env_files_without_registering_them(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(
        source_root / "logic" / "供应商查询.md",
        desc="根据物料查询供应商",
        is_tool_flow=True,
    )
    action_dir = source_root / "action"
    action_dir.mkdir(parents=True, exist_ok=True)
    (action_dir / "supplier_lookup.py").write_text(
        "# 工具流程: 供应商查询\nprint('supplier')\n",
        encoding="utf-8",
    )
    (action_dir / ".env").write_text(
        "API_TOKEN=demo-token\n",
        encoding="utf-8",
    )

    result = run(source_root=source_root, target_root=target_root)

    scripts_list = (target_root / "scripts-list.md").read_text(encoding="utf-8")
    assert "- `供应商查询` -> `scripts/supplier_lookup.py`" in scripts_list
    assert ".env" not in scripts_list
    assert (target_root / "scripts" / ".env").read_text(encoding="utf-8") == "API_TOKEN=demo-token\n"
    assert str(target_root / "scripts" / ".env") in result["scripts"]["files"]


def test_run_uses_structured_script_parameter_comment(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(
        source_root / "logic" / "供应商查询.md",
        desc="根据物料查询供应商",
        is_tool_flow=True,
    )
    action_dir = source_root / "action"
    action_dir.mkdir(parents=True, exist_ok=True)
    (action_dir / "supplier_lookup.py").write_text(
        "\n".join([
            "# 工具流程: 供应商查询",
            "# parameters: [{\"name\":\"物料编号\",\"flag\":\"--material-code\",\"description\":\"物料编号\",\"required\":true,\"type\":\"string\",\"aliases\":[\"material_code\"]}]",
            "print('supplier')",
        ]),
        encoding="utf-8",
    )

    run(source_root=source_root, target_root=target_root)

    scripts_list = (target_root / "scripts-list.md").read_text(encoding="utf-8")
    assert _script_parameters(scripts_list, "供应商查询") == [
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
        is_tool_flow=True,
    )
    action_dir = source_root / "action"
    action_dir.mkdir(parents=True, exist_ok=True)
    (action_dir / "supplier_lookup.py").write_text(
        "\n".join([
            "# 工具流程: 供应商查询",
            "# 参数:",
            "# - name: 物料编号",
            "#   flag: --material-code",
            "#   description: 物料编号",
            "#   required: true",
            "#   type: string",
            "#查数脚本 （涉及mindsdb查数的脚本会加这个标签）",
            "print('supplier')",
        ]),
        encoding="utf-8",
    )

    result = run(source_root=source_root, target_root=target_root)

    scripts_list = (target_root / "scripts-list.md").read_text(encoding="utf-8")
    assert result["scripts"]["warnings"] == []
    assert _script_parameters(scripts_list, "供应商查询") == [
        {
            "name": "物料编号",
            "flag": "--material-code",
            "description": "物料编号",
            "required": True,
            "type": "string",
            "aliases": ["material_code"],
        }
    ]


def test_run_falls_back_to_argparse_parameters(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_object(source_root / "object" / "物料.md")
    _write_logic(
        source_root / "logic" / "安装判断.md",
        desc="判断物料是否安装",
        is_tool_flow=True,
    )
    action_dir = source_root / "action"
    action_dir.mkdir(parents=True, exist_ok=True)
    (action_dir / "check_install.py").write_text(
        "\n".join([
            "# 工具流程: 安装判断",
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

    scripts_list = (target_root / "scripts-list.md").read_text(encoding="utf-8")
    assert _script_parameters(scripts_list, "安装判断") == [
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
