"""脚本入参形态：按来源对象分组，对齐 consumer 调用前的参数证据校验（evidence_record_mismatch）。

合成示例：事件、需求、设备三个对象。
"""

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

from app import config
from app.services import publish as publish_service
from app.services.toolkit import script_argument_shape as shape
from app.services.toolkit import script_contract, script_descriptor
from app.services.workspace import WorkspaceError, WorkspaceManager

ATTRIBUTES = {
    "事件": {"EVENT_ID", "EVENT_DATE", "DEVICE_ID", "EVENT_TYPE"},
    "需求": {"NEED_ID", "NEED_QTY", "EVENT_ID"},
    "设备": {"DEVICE_ID", "DEVICE_NAME"},
}


def _param(name: str, desc: str, param_type: str = "array", required: str = "否") -> dict:
    return {"name": name, "type": param_type, "multiple": "否", "desc": desc, "required": required}


def _errors(params: list[dict]) -> list[str]:
    return [message for severity, message in shape.argument_shape_issues(params, ATTRIBUTES) if severity == "错误"]


def test_record_mixing_fields_of_two_objects_is_rejected():
    mixed = _param("events", '[[事件]] 查询结果；例：[{"EVENT_ID":"E-1","DEVICE_NAME":"设备A"}]')
    errors = _errors([mixed])
    assert len(errors) == 1 and "DEVICE_NAME 属于「设备」" in errors[0]
    undeclared = _param("needs", '需求记录；例：[{"NEED_ID":"N-1","EVENT_TYPE":"巡检"}]')
    assert len(_errors([undeclared])) == 1


def test_records_grouped_by_object_pass():
    params = [
        _param("events", '[[事件]] 查询结果；例：[{"EVENT_ID":"E-1","DEVICE_ID":"D-1","EVENT_DATE":"2026-01-01"}]'),
        _param("devices", '[[设备]] 查询结果；例：[{"DEVICE_ID":"D-1","DEVICE_NAME":"设备A"}]'),
        _param("query_month", "月份", "string"),
        _param("codes", '编码列表；例：["A","B"]'),
    ]
    assert shape.argument_shape_issues(params, ATTRIBUTES) == []


def test_mixing_without_identity_field_is_left_to_consumer():
    # consumer 只在涉及标识字段时核对同一条记录
    assert _errors([_param("rows", '例：[{"NEED_QTY":2,"EVENT_DATE":"2026-01-01"}]')]) == []


def test_top_level_parameters_named_after_attributes():
    two = [_param("DEVICE_ID", "设备", "string"), _param("EVENT_DATE", "日期", "string")]
    assert len(_errors(two)) == 1 and "顶层参数" in _errors(two)[0]
    one = [_param("DEVICE_ID", "设备", "string"), _param("query_month", "月份", "string")]
    assert _errors(one) == []


def test_warnings_for_missing_source_object_and_unknown_fields():
    issues = shape.argument_shape_issues(
        [
            _param("events", '事件；例：[{"EVENT_ID":"E-1"}]'),
            _param("needs", '[[需求]] 查询结果；例：[{"need_id":"N-1"}]'),
        ],
        ATTRIBUTES,
    )
    messages = [message for severity, message in issues if severity == "警告"]
    assert any("没有写明来源对象" in message for message in messages)
    assert any("need_id 不在 [[需求]] 的属性清单里" in message for message in messages)


def test_attributes_from_workspace_and_package_documents():
    workspace_doc = (
        "# 对象属性清单\n| 中文名称 | 英文名称 | 描述 | 主键 | 类型 |\n|---|---|---|---|---|\n"
        "| 事件编号 | EVENT_ID | 编号 | 是 | 字符串 |\n"
    )
    package_doc = "# 字段\n| 字段 key | 中文名 | 类型 |\n| --- | --- | --- |\n| DEVICE_ID | 设备编号 | string |\n"
    attributes = shape.attributes_from_documents({"事件": workspace_doc, "设备": package_doc})
    assert attributes == {"事件": {"EVENT_ID"}, "设备": {"DEVICE_ID"}}


# ---- 枚举字段：智能体只看参数说明，假设场景里常自造取值 ----

ENUM_DOCS = {
    "事件": (
        "# 对象属性清单\n| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |\n|---|---|---|---|---|---|---|\n"
        "| 事件编号 | EVENT_ID | 编号 | 是 | 字符串 | 无 | 无 |\n"
        "| 事件类型 | EVENT_TYPE | 类型 | 否 | 枚举 | 无 | 取值：巡检 / 维修 / 更换；受设备类型约束 |\n"
        "| 已复核 | REVIEWED | 是否复核 | 否 | 枚举 | 无 | 取值：是 / 否 |\n"
        "| 等级 | LEVEL | 等级 | 否 | 字符串 | 无 | 取值集合未在来源资料中定义，样例仅见 一级 / 二级 |\n"
    ),
    "设备": "# 字段\n| 字段 key | 中文名 | 类型 | 规则 |\n| --- | --- | --- | --- |\n| DEVICE_KIND | 设备种类 | 枚举 | 取值：泵 / 阀 |\n",
}


def test_enum_values_from_workspace_and_package_documents():
    # 「是/否」字段由 _yes_no 兼容，不列；没写「取值：」的不算枚举
    assert shape.enum_values_from_documents(ENUM_DOCS) == {
        "事件": {"EVENT_TYPE": ["巡检", "维修", "更换"]},
        "设备": {"DEVICE_KIND": ["泵", "阀"]},
    }


def test_descriptor_lists_allowed_values_of_mentioned_enum_fields(tmp_path: Path):
    ontology = tmp_path / "ontology"
    for sub in ("operation", "object", "scripts"):
        (ontology / sub).mkdir(parents=True)
    (ontology / "operation" / "事件汇总.md").write_text("# 业务描述\n事件汇总\n", encoding="utf-8")
    for name, text in ENUM_DOCS.items():
        (ontology / "object" / f"{name}.md").write_text(text, encoding="utf-8")
    (ontology / "scripts" / "event_summary.py").write_text(
        '"""\n关联operation：事件汇总\n\n输入参数：\n'
        '    events    array    [[事件]] 查询结果，必填；例：[{"EVENT_ID":"E-1","EVENT_TYPE":"巡检"}]\n'
        '    devices    array    [[设备]] 查询结果；例：[{"DEVICE_ID":"D-1"}]\n"""\n'
        "import argparse\nimport json\nparser = argparse.ArgumentParser()\n"
        'parser.add_argument("--events", required=True, type=json.loads)\n'
        'parser.add_argument("--devices", type=json.loads)\n',
        encoding="utf-8",
    )
    text = script_descriptor.load_scripts_state(ontology).expected_descriptors()["event_summary"]
    rows = {line.split("|")[1].strip(): line for line in text.splitlines() if line.startswith("| ")}
    assert "不要自造或改写：EVENT_TYPE 取 巡检 / 维修 / 更换" in rows["events"]
    assert "DEVICE_KIND" not in rows["devices"]  # 参数说明没点到的字段不列


def test_descriptor_tells_agent_to_copy_one_record_per_item():
    code = (
        '"""\n关联operation：事件汇总\n\n输入参数：\n'
        '    events    array    [[事件]] 查询结果，必填；例：[{"EVENT_ID":"E-1"}]\n'
        '    codes    array    编码列表；例：["A"]\n"""\n'
        "import argparse\nimport json\nparser = argparse.ArgumentParser()\n"
        'parser.add_argument("--events", required=True, type=json.loads)\n'
        'parser.add_argument("--codes", type=json.loads)\n'
    )
    contract = script_contract.parse_script(code)
    text = script_descriptor.render_descriptor("event_summary", contract, "事件汇总", "event_summary.py")
    events_row = next(line for line in text.splitlines() if line.startswith("| events |"))
    codes_row = next(line for line in text.splitlines() if line.startswith("| codes |"))
    assert shape.COPY_RULE in events_row
    assert shape.COPY_RULE not in codes_row  # 字符串数组不是记录参数
    # 题面只说记录已提供、没给取值时，智能体要传 {}，否则脚本只能判记录缺失；放在最前面，智能体最容易漏传
    assert events_row.split("|")[6].strip().startswith(shape.PRESENCE_RULE)
    assert shape.PRESENCE_RULE not in codes_row
    assert "- 传参：" in text and "传 {}" in text


def test_description_of_absent_record_handling_is_warned():
    """「不传时按缺少记录输出资料不足」会让智能体在假设场景里整条不传。"""
    misleading = _param(
        "devices", '[[设备]] 查询结果；不传或没有该记录时按缺少设备记录输出资料不足；例：[{"DEVICE_ID":"D-1","DEVICE_NAME":"设备A"}]'
    )
    warnings = [message for severity, message in shape.argument_shape_issues([misleading], ATTRIBUTES) if severity == "警告"]
    assert any("不传时的处理" in message and "{}" in message for message in warnings)
    plain = _param("devices", '[[设备]] 查询结果；不传时默认空列表；例：[{"DEVICE_ID":"D-1","DEVICE_NAME":"设备A"}]')
    assert shape.argument_shape_issues([plain], ATTRIBUTES) == []


# ---- 假设场景：用户只描述情况、没给编号 ----


def test_required_identity_parameters_are_rejected():
    required_id = [_param("event_id", "事件编号", "string", required="是")]
    assert len(_errors(required_id)) == 1 and "ungrounded_identity" in _errors(required_id)[0]
    assert _errors([_param("event_id", "事件编号", "string")]) == []

    only_ids = _param("devices", '[[设备]] 查询结果；例：[{"DEVICE_ID":"D-1","DEVICE_NAME":"设备A"}]', required="是")
    assert len(_errors([only_ids])) == 1 and "只含标识字段" in _errors([only_ids])[0]
    with_facts = _param(
        "events", '[[事件]] 查询结果；例：[{"EVENT_ID":"E-1","EVENT_DATE":"2026-01-01"}]', required="是"
    )
    assert _errors([with_facts]) == []  # 编号可省略，事实字段照用户描述填
    assert _errors([{**only_ids, "required": "否"}]) == []


def test_descriptor_says_identity_values_must_not_be_made_up():
    code = (
        '"""\n关联operation：事件汇总\n\n输入参数：\n'
        '    events    array    [[事件]] 查询结果，必填；例：[{"EVENT_ID":"E-1","EVENT_DATE":"2026-01-01"}]\n'
        "    event_id    string    要汇总的事件编号\n"
        "    query_month    string    月份\n"
        '"""\n'
        "import argparse\nimport json\nparser = argparse.ArgumentParser()\n"
        'parser.add_argument("--events", required=True, type=json.loads)\n'
        'parser.add_argument("--event-id")\n'
        'parser.add_argument("--query-month")\n'
    )
    contract = script_contract.parse_script(code)
    text = script_descriptor.render_descriptor("event_summary", contract, "事件汇总", "event_summary.py")
    rows = {line.split("|")[1].strip(): line for line in text.splitlines() if line.startswith("| ")}
    assert f"其中 EVENT_ID 是编号，{shape.IDENTITY_NOTE}；只省略这些字段，省略后不剩字段就传 {{}}" in rows["events"]
    assert f"这是编号，{shape.IDENTITY_NOTE}" in rows["event_id"]
    assert shape.IDENTITY_NOTE not in rows["query_month"]


# ---- 「是/否」字段：图数据源存成布尔值 ----


@pytest.mark.parametrize(
    "source, warned",
    [
        ('def f(row):\n    return row.get("FLAG") == "是"\n', True),
        ('def f(value):\n    if value not in ("是", "否"):\n        return None\n', True),
        ('def f(row):\n    return _yes_no(row.get("FLAG")) == "是"\n', False),
        ('def f(value):\n    if isinstance(value, bool):\n        return value\n    return value == "是"\n', False),
        ('def f():\n    return {"conclusion": "是"}\n', False),  # 只是输出取值，不是判断
    ],
)
def test_yes_no_comparisons_must_accept_booleans(source: str, warned: bool):
    issues = shape.yes_no_issues(source)
    assert bool(issues) is warned
    assert all(severity == "警告" and "_yes_no" in message for severity, message in issues)


def _template():
    path = Path(config.ASSETS_DIR) / "workspace-toolkit" / "templates" / "script-template.py"
    spec = importlib.util.spec_from_file_location("script_template", path)
    template = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(template)
    return template, path


def test_template_yes_no_accepts_graph_booleans():
    template, path = _template()
    assert [template._yes_no(value) for value in (True, "true", "Yes", " 是 ", "y")] == ["是"] * 5
    assert [template._yes_no(value) for value in (False, "FALSE", "no", "否", "n")] == ["否"] * 5
    assert [template._yes_no(value) for value in (None, "", "未知", 1)] == [None] * 4
    assert shape.yes_no_issues(path.read_text(encoding="utf-8")) == []


def test_template_invalid_input_keeps_the_conversation_going(capsys):
    """consumer 遇到非 0 退出会结束本轮对话；参数错误要退出码 0、输出 ok=false，智能体才能改参数重试。"""
    template, _path = _template()
    assert template._invalid_input("events[1].EVENT_DATE 不是 YYYY-MM-DD 格式") == 0  # 返回的进程退出码
    assert json.loads(capsys.readouterr().out) == {
        "ok": False, "error": {"code": "INVALID_INPUT", "message": "events[1].EVENT_DATE 不是 YYYY-MM-DD 格式"},
    }


def test_template_public_error_returns_nonzero_exit_code(capsys):
    """脚本自身异常才非 0 退出：stderr 一行 ONTO_PUBLIC_ERROR，退出码由 _cli 返回、只在入口退出进程。"""
    template, _path = _template()
    assert template._public_error("SCRIPT_FAILED", "第 2 步出错（KeyError）") == 2
    err = capsys.readouterr().err
    assert err.startswith("ONTO_PUBLIC_ERROR:")
    assert json.loads(err[len("ONTO_PUBLIC_ERROR:"):]) == {"code": "SCRIPT_FAILED", "message": "第 2 步出错（KeyError）"}


def test_template_missing_record_tells_agent_how_to_retry():
    """智能体在假设场景里常先漏传记录参数：缺失说明要写明参数未传与改传 {} 的补救办法。"""
    template, _path = _template()
    message = template._missing_record("devices")
    assert "参数 devices 未传" in message and "devices={}" in message


def test_template_check_choice_reports_allowed_values():
    """不认识的枚举取值不能当成条件不满足：抛 ValueError 由 _cli 转成 ok=false，智能体按允许取值改正。"""
    template, _path = _template()
    allowed = ("巡检", "维修")
    assert template._check_choice(" 维修 ", allowed, "events[0].EVENT_TYPE") == "维修"
    assert template._check_choice("", allowed, "events[0].EVENT_TYPE") is None  # 缺失按资料不足处理
    with pytest.raises(ValueError) as exc:
        template._check_choice("保养", allowed, "events[1].EVENT_TYPE")
    assert str(exc.value) == "events[1].EVENT_TYPE 不是允许的取值，只能取：巡检 / 维修"  # 不回显取值


def test_template_record_key_never_treats_missing_ids_as_equal():
    """没有编号时按位置当键：拿空编号匹配、去重，两条记录会被当成同一条，集合判断就会出错。"""
    template, _path = _template()
    events = [{"EVENT_TYPE": "巡检"}, {"EVENT_ID": "", "EVENT_TYPE": "维修"}, {"EVENT_ID": " E-9 "}]
    keys = [template._record_key(event, "EVENT_ID", "events", index) for index, event in enumerate(events)]
    assert keys == ["events[0]", "events[1]", "E-9"]
    assert len(set(keys)) == len(events)


def test_identity_note_asks_to_fill_code_names_given_by_the_question():
    """题面只用代号说明归属（如分属甲、乙）时，智能体要照填进编号字段，脚本才能发现不一致。"""
    assert "原样填进对应编号字段" in shape.IDENTITY_NOTE and "不要编造" in shape.IDENTITY_NOTE


def _ontology(tmp_path: Path, script: str) -> Path:
    ontology = tmp_path / "ontology"
    for sub in ("operation", "object", "scripts"):
        (ontology / sub).mkdir(parents=True)
    (ontology / "operation" / "事件汇总.md").write_text("# 业务描述\n事件汇总\n", encoding="utf-8")
    for name, fields in {"事件": ("EVENT_ID", "DEVICE_ID", "EVENT_DATE"), "设备": ("DEVICE_ID", "DEVICE_NAME")}.items():
        rows = "".join(f"| {field} | {field} | 字段 | 否 | 字符串 |\n" for field in fields)
        (ontology / "object" / f"{name}.md").write_text(
            "# 对象属性清单\n| 中文名称 | 英文名称 | 描述 | 主键 | 类型 |\n|---|---|---|---|---|\n" + rows,
            encoding="utf-8",
        )
    (ontology / "scripts" / "event_summary.py").write_text(script, encoding="utf-8")
    return ontology


MIXED_SCRIPT = (
    '"""\n关联operation：事件汇总\n\n功能描述：汇总事件\n\n输入参数：\n'
    '    events    array    [[事件]] 查询结果，必填；例：[{"EVENT_ID":"E-1","DEVICE_NAME":"设备A"}]\n\n'
    '输出：\n    total    integer    事件数\n"""\n'
    "import argparse\nimport json\n\n\ndef _cli():\n    parser = argparse.ArgumentParser()\n"
    '    parser.add_argument("--events", required=True, type=json.loads)\n'
    "    print(json.dumps({'total': 0}))\n\n\nif __name__ == \"__main__\":\n    _cli()\n"
)


def test_checker_issues_include_shape_errors_and_missing_public_error(tmp_path: Path):
    ontology = _ontology(tmp_path, MIXED_SCRIPT)
    script_descriptor.sync_workspace_descriptors(ontology)
    issues = script_descriptor.descriptor_issues(ontology)
    errors = [message for file, severity, message in issues if severity == "错误"]
    warnings = [message for file, severity, message in issues if severity == "警告"]
    assert any("混入了不同对象的字段" in message for message in errors)
    assert any("ONTO_PUBLIC_ERROR" in message for message in warnings)


def test_checker_warns_yes_no_comparison(tmp_path: Path):
    script = MIXED_SCRIPT.replace(
        "    print(json.dumps({'total': 0}))\n",
        "    args = parser.parse_args()\n"
        "    print(json.dumps({'total': sum(1 for e in args.events if e.get('FLAG') == '是')}))\n",
    )
    ontology = _ontology(tmp_path, script)
    warnings = [
        message for file, severity, message in script_descriptor.descriptor_issues(ontology)
        if severity == "警告" and file == "scripts/event_summary.py"
    ]
    assert any("_yes_no" in message for message in warnings)


def test_checker_warns_invalid_input_exit_that_ends_the_conversation(tmp_path: Path):
    script = MIXED_SCRIPT.replace(
        "    print(json.dumps({'total': 0}))\n",
        "    print(json.dumps({'total': 0}))\n\n\n"
        "def _public_error(code, message):\n    raise SystemExit(2)\n\n\n"
        "def _check(events):\n    if not events:\n        _public_error(\"INVALID_INPUT\", \"events 为空\")\n",
    )
    ontology = _ontology(tmp_path, script)
    warnings = [
        message for file, severity, message in script_descriptor.descriptor_issues(ontology)
        if severity == "警告" and file == "scripts/event_summary.py"
    ]
    assert any("_invalid_input" in message and "结束本轮对话" in message for message in warnings)


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    return WorkspaceManager(tmp_path / "data", assets)


def test_export_rejects_scripts_consumer_would_refuse(manager: WorkspaceManager, tmp_path: Path):
    meta = manager.create_project("事件汇总")
    root = manager.project_root(meta["id"])
    source = _ontology(tmp_path / "src", MIXED_SCRIPT)
    for sub in ("operation", "object", "scripts"):
        for path in (source / sub).glob("*.*"):
            (root / "ontology" / sub / path.name).write_bytes(path.read_bytes())
    with pytest.raises(WorkspaceError) as exc:
        publish_service.export_package_zip(manager, meta["id"], tmp_path / "pkg.zip", allow_unbound=True)
    assert exc.value.code == "script_argument_shape"
    assert not (tmp_path / "pkg.zip").exists()

    fixed = MIXED_SCRIPT.replace('"DEVICE_NAME":"设备A"', '"EVENT_DATE":"2026-01-01"')
    (root / "ontology" / "scripts" / "event_summary.py").write_text(fixed, encoding="utf-8")
    publish_service.export_package_zip(manager, meta["id"], tmp_path / "pkg.zip", allow_unbound=True)
    with zipfile.ZipFile(tmp_path / "pkg.zip") as zf:
        descriptor = zf.read(f"{meta['id']}/scripts/event_summary.md").decode("utf-8")
    assert shape.COPY_RULE in descriptor
