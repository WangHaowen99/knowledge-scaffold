"""脚本描述符：与 consumer 调用口径对齐的参数类型、按流程的对外绑定规则、工作区同步与导出。

合成示例：订单审核（主操作）调用 库存校验、折扣计算 两个子操作。
"""

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from app.services import publish as publish_service
from app.services.toolkit import operation_relations, script_contract, script_descriptor
from app.services.workspace import WorkspaceError, WorkspaceManager

ScriptContractError = script_contract.ScriptContractError
GIT = shutil.which("git")

REVIEW_SCRIPT = '''"""
关联operation：订单审核

功能描述：校验库存并计算折扣，给出订单审核结论

输入参数：
    orders    array    订单记录，必填；JSON 数组，例：[{"order_id":"A-001","qty":2}]
    level    integer    客户等级，默认 1
    rate    number    折扣率，默认 0.9
    vip    boolean    是否 VIP；默认 false
    tags    string    标签，可多次传入

输出：
    conclusion    string    审核结论：通过 / 不通过 / 资料不足
    steps    object    各子操作的结论，键为子操作名
"""
import argparse
import json


def review(orders, level=1, rate=0.9, vip=False, tags=None):
    return {"conclusion": "通过", "steps": {}}


def _cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--orders", required=True, type=json.loads)
    parser.add_argument("--level", type=int, default=1)
    parser.add_argument("--rate", type=float, default=0.9)
    parser.add_argument("--vip", action="store_true")
    parser.add_argument("--tags", action="append")
    print(json.dumps(review(**vars(parser.parse_args())), ensure_ascii=False))


if __name__ == "__main__":
    _cli()
'''


def _script(operation: str, extra_arguments: str = "", header_params: str = "") -> str:
    return (
        f'"""\n关联operation：{operation}\n\n功能描述：{operation}的结论\n\n'
        f"输入参数：\n{header_params or '    无'}\n\n"
        "输出：\n    conclusion    string    结论\n\"\"\"\n"
        "import argparse\nimport json\n\n"
        "def _cli():\n    parser = argparse.ArgumentParser()\n"
        f"{extra_arguments}"
        "    print(json.dumps({'conclusion': 'ok'}))\n"
    )


def _operation(name: str, callers: str = "无", callees: str = "无", standalone: str = "") -> str:
    lines = [
        f"# 业务描述\n{name}\n",
        "# 与operation的关联",
        f"- 被operation调用 > {callers}",
        f"- 调用operation > {callees}",
    ]
    if standalone:
        lines.append(f"- 可单独调用 > {standalone}")
    lines += [
        "",
        "# 输入\n| 参数名称 | 描述 | 输入源 |\n|---|---|---|\n| orders | 订单记录 | [[订单]] |\n",
        "# 输出\n| 参数名称 | 描述 |\n|---|---|\n| conclusion | 结论 |\n",
        "# 逻辑描述\n1. 处理\n",
    ]
    return "\n".join(lines)


def _flow_ontology(tmp_path: Path, *, discount_standalone: str = "") -> Path:
    ontology = tmp_path / "ontology"
    (ontology / "operation").mkdir(parents=True)
    (ontology / "scripts").mkdir()
    docs = {
        "订单审核": _operation("订单审核", callees="[[库存校验]]、[[折扣计算]]"),
        "库存校验": _operation("库存校验", callers="[[订单审核]]"),
        "折扣计算": _operation("折扣计算", callers="[[订单审核]]", standalone=discount_standalone),
    }
    for name, text in docs.items():
        (ontology / "operation" / f"{name}.md").write_text(text, encoding="utf-8")
    return ontology


def _report(ontology: Path):
    return script_descriptor.load_scripts_state(ontology).report


# ---- 参数类型与 consumer 调用口径对齐 ----


def test_param_types_use_consumer_vocabulary():
    contract = script_contract.parse_script(REVIEW_SCRIPT)
    params = {item["name"]: item for item in contract.params}
    assert params["orders"]["type"] == "array" and params["orders"]["required"] == "是"
    # type=int 必须标 integer：标成 number 时 consumer 会传 5.0，argparse 解析失败
    assert params["level"]["type"] == "integer" and params["level"]["default"] == "1"
    assert params["rate"]["type"] == "number" and params["rate"]["default"] == "0.9"
    assert params["vip"]["type"] == "boolean"
    assert params["vip"]["action"] == "store_true" and params["vip"]["default"] == "false"
    assert params["tags"]["multiple"] == "是" and params["tags"]["action"] == ""
    assert contract.warnings == ()
    assert [item["name"] for item in contract.outputs] == ["conclusion", "steps"]


@pytest.mark.parametrize(
    "argument, message",
    [
        ('parser.add_argument("--ids", nargs="+")', "只保留最后一组"),
        ('parser.add_argument("--flag", type=bool)', 'action="store_true"'),
        ('parser.add_argument("--items", type=list)', "type=json.loads"),
        ('parser.add_argument("records")', "位置参数"),
        ('parser.add_argument("--verbose", action="count")', "传不进去"),
        ('parser.add_argument("--dry-run", action="store_true", required=True)', "不能设为必填"),
    ],
)
def test_contract_rejects_arguments_consumer_cannot_pass(argument: str, message: str):
    code = f'"""\n关联operation：订单审核\n"""\nimport argparse\nparser = argparse.ArgumentParser()\n{argument}\n'
    with pytest.raises(ScriptContractError, match=message):
        script_contract.parse_script(code)


def test_header_type_incompatible_with_argparse_is_rejected():
    # integer 与 number 互写只是精度描述不同、以 argparse 为准（见 test_script_contract_checker）；类型真正不兼容才拒绝
    code = (
        '"""\n关联operation：订单审核\n输入参数：\n    level    boolean    等级\n"""\n'
        "import argparse\nparser = argparse.ArgumentParser()\n"
        'parser.add_argument("--level", type=int)\n'
    )
    with pytest.raises(ScriptContractError, match="头部为 boolean，argparse 为 integer"):
        script_contract.parse_script(code)


def test_defaults_are_json_literals_consumer_can_send_back():
    code = (
        '"""\n关联operation：订单审核\n"""\nimport argparse\nparser = argparse.ArgumentParser()\n'
        'parser.add_argument("--mode", default="true")\n'
        'parser.add_argument("--region", default="east")\n'
        'parser.add_argument("--codes", type=json.loads, default=["a", "b"])\n'
        "parser.add_argument('--limit', type=int, default=DEFAULT_LIMIT)\n"
    )
    params = {item["name"]: item for item in script_contract.parse_script(code).params}
    assert params["mode"]["default"] == '"true"'  # 不加引号 consumer 会解析成布尔
    assert params["region"]["default"] == "east"
    assert json.loads(params["codes"]["default"]) == ["a", "b"]
    assert params["limit"]["default"] == ""  # 引用常量：留空让脚本自己的默认值生效


def test_legacy_json_type_warns_to_declare_object_or_array():
    code = (
        '"""\n关联operation：订单审核\n输入参数：\n    orders    json    订单列表，必填\n"""\n'
        "import argparse\nimport json\nparser = argparse.ArgumentParser()\n"
        'parser.add_argument("--orders", required=True, type=json.loads)\n'
    )
    contract = script_contract.parse_script(code)
    assert contract.params[0]["type"] == "json"
    assert any("object" in warning and "array" in warning for warning in contract.warnings)


def test_fragment_binding_is_parsed_but_kept_apart():
    contract = script_contract.parse_script('"""\n关联operation：订单审核 / 步骤2-3\n"""\n')
    assert (contract.bound, contract.fragment) == ("订单审核", "步骤2-3")
    assert script_contract.parse_script_contract('"""\n关联operation：订单审核 / 步骤2\n"""\n')[0] == "订单审核"
    assert script_contract.parse_script('"""\n关联operation：无（公共模块）\n"""\n').bound == ""


# ---- 描述符渲染：对齐 consumer 读法 ----


def test_render_descriptor_matches_consumer_contract():
    contract = script_contract.parse_script(REVIEW_SCRIPT)
    text = script_descriptor.render_descriptor("order_review", contract, "订单审核", "order_review.py")
    assert text.startswith("---\nresource: script\n")
    assert 'bound_operation: "订单审核"' in text and "entrypoint: order_review.py\n" in text
    assert 'title: "校验库存并计算折扣，给出订单审核结论"' in text
    body = text.split("---\n", 2)[2]
    headings = [line for line in body.splitlines() if line.startswith("#")]
    assert headings == ["# 脚本用途", "# 输入参数", "# 输出", "# 执行边界"]
    tables = [line for line in body.splitlines() if line.startswith("|")]
    # consumer 把正文第一张表当参数表
    assert tables[0] == script_descriptor.PARAM_HEADER
    assert "| level | --level | integer | 否 | 1 | 客户等级，默认 1 | 否 |  |" in tables
    assert "| vip | --vip | boolean | 否 | false | 是否 VIP；默认 false | 否 | store_true |" in tables
    assert "| steps | object | 各子操作的结论，键为子操作名 |" in tables
    assert "ONTO_PUBLIC_ERROR:" in body
    # consumer 遇到非 0 退出会结束本轮对话：参数错误走退出码 0 的 ok=false，智能体才能改参数重试
    assert '参数错误：退出码 0，stdout 输出 {"ok":false' in body and "直接结束本轮对话" in body


def test_render_descriptor_without_params_keeps_parameter_table_first():
    contract = script_contract.parse_script(_script("库存校验"))
    text = script_descriptor.render_descriptor("stock_check", contract, "库存校验", "stock_check.py")
    tables = [line for line in text.splitlines() if line.startswith("|")]
    assert tables[:2] == [script_descriptor.PARAM_HEADER, script_descriptor.PARAM_DIVIDER]
    assert tables[2] == script_descriptor.OUTPUT_HEADER


# ---- operation 调用关系 ----


def test_relations_follow_relation_section_and_call_sites(tmp_path: Path):
    ontology = _flow_ontology(tmp_path, discount_standalone="是")
    relations = operation_relations.load_operation_relations(ontology)
    assert relations["订单审核"].callees == ("库存校验", "折扣计算")
    assert relations["库存校验"].callers == ("订单审核",)
    assert operation_relations.standalone_flag(relations["折扣计算"]) is True
    assert operation_relations.standalone_flag(relations["库存校验"]) is False
    parents = operation_relations.parent_map(relations)
    assert operation_relations.ancestors("折扣计算", parents) == ["订单审核"]
    logic_only = operation_relations.parse_relation("# 逻辑描述\n1. 调用[[库存校验]]逻辑文档\n")
    assert logic_only.callees == ("库存校验",)


def test_stops_before_call_detects_preceding_checks():
    parent = (
        "# 逻辑描述\n1. 前置校验：不满足前提时终止流程并输出异常说明。\n"
        "2. 依次调用[[库存校验]]、[[折扣计算]]逻辑文档。\n"
    )
    assert operation_relations.stops_before_call(parent, "库存校验")
    assert operation_relations.stops_before_call(parent, "折扣计算")  # 列表写法按首次出现定位
    after = "# 逻辑描述\n1. 调用[[库存校验]]逻辑文档。\n2. 结束流程。\n"
    assert not operation_relations.stops_before_call(after, "库存校验")
    field_name = "# 逻辑描述\n1. 读取订单的结束日期。\n2. 调用[[库存校验]]逻辑文档。\n"
    assert not operation_relations.stops_before_call(field_name, "库存校验")


def test_sub_operation_descriptor_states_its_scope(tmp_path: Path):
    ontology = _flow_ontology(tmp_path, discount_standalone="是")
    (ontology / "scripts" / "order_review.py").write_text(REVIEW_SCRIPT, encoding="utf-8")
    (ontology / "scripts" / "discount.py").write_text(_script("折扣计算"), encoding="utf-8")
    script_descriptor.sync_workspace_descriptors(ontology)
    sub = (ontology / "scripts" / "discount.md").read_text(encoding="utf-8")
    assert 'title: "折扣计算的结论（只覆盖「订单审核」中的这一步）"' in sub
    assert "不含「订单审核」在这一步之前的输入检查" in sub
    assert "只覆盖" not in (ontology / "scripts" / "order_review.md").read_text(encoding="utf-8")


# ---- 对外绑定规则 ----


def test_main_script_covers_stage_only_sub_operations(tmp_path: Path):
    ontology = _flow_ontology(tmp_path)
    (ontology / "scripts" / "order_review.py").write_text(REVIEW_SCRIPT, encoding="utf-8")
    report = _report(ontology)
    assert report.bindings == {"order_review": "订单审核"}
    assert report.errors == []


def test_stage_only_sub_operation_cannot_keep_its_own_script(tmp_path: Path):
    ontology = _flow_ontology(tmp_path)
    (ontology / "scripts" / "order_review.py").write_text(REVIEW_SCRIPT, encoding="utf-8")
    (ontology / "scripts" / "stock_check.py").write_text(_script("库存校验"), encoding="utf-8")
    report = _report(ontology)
    assert [file for file, _ in report.errors] == ["scripts/stock_check.py"]
    assert "可单独调用 > 是" in report.errors[0][1]


def test_standalone_sub_operation_keeps_its_own_script(tmp_path: Path):
    ontology = _flow_ontology(tmp_path, discount_standalone="是")
    (ontology / "scripts" / "order_review.py").write_text(REVIEW_SCRIPT, encoding="utf-8")
    (ontology / "scripts" / "discount.py").write_text(_script("折扣计算"), encoding="utf-8")
    report = _report(ontology)
    assert report.errors == []
    assert report.bindings["discount"] == "折扣计算"


def test_standalone_sub_operation_without_script_is_a_warning(tmp_path: Path):
    ontology = _flow_ontology(tmp_path, discount_standalone="是")
    (ontology / "scripts" / "order_review.py").write_text(REVIEW_SCRIPT, encoding="utf-8")
    report = _report(ontology)
    assert report.errors == []
    assert [file for file, _ in report.warnings] == ["operation/折扣计算.md"]


def test_sub_operation_scripts_are_fine_when_parent_has_no_script(tmp_path: Path):
    ontology = _flow_ontology(tmp_path)
    (ontology / "scripts" / "stock_check.py").write_text(_script("库存校验"), encoding="utf-8")
    assert _report(ontology).errors == []


def test_one_operation_one_script_and_no_fragments(tmp_path: Path):
    ontology = _flow_ontology(tmp_path)
    (ontology / "scripts" / "order_review.py").write_text(REVIEW_SCRIPT, encoding="utf-8")
    (ontology / "scripts" / "order_review_v2.py").write_text(_script("订单审核"), encoding="utf-8")
    (ontology / "scripts" / "order_review_step2.py").write_text(
        _script("订单审核 / 步骤2"), encoding="utf-8"
    )
    (ontology / "scripts" / "typo.py").write_text(_script("订单审批"), encoding="utf-8")
    report = _report(ontology)
    messages = [message for _, message in report.errors]
    assert any("绑定了多个脚本" in message for message in messages)
    assert any("片段脚本" in message for message in messages)
    # 关联到不存在的 operation：按库模块处理，与导出一致；由 ontology_checker 的「脚本绑定」检查报错
    assert "typo" not in report.bindings
    assert not any("typo.py" in message for message in messages)


# ---- 工作区同步 ----


def test_sync_workspace_descriptors(tmp_path: Path):
    ontology = _flow_ontology(tmp_path)
    scripts = ontology / "scripts"
    (scripts / "order_review.py").write_text(REVIEW_SCRIPT, encoding="utf-8")
    (scripts / "shared_math.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (scripts / "shared_math.md").write_text("stale", encoding="utf-8")
    (scripts / "broken.py").write_text('"""\n关联operation：订单审核\n"""\n(', encoding="utf-8")
    (scripts / "broken.md").write_text("last good", encoding="utf-8")

    changed = script_descriptor.sync_workspace_descriptors(ontology)
    assert {path.name for path in changed} == {"order_review.md", "shared_math.md"}
    descriptor = (scripts / "order_review.md").read_text(encoding="utf-8")
    assert 'bound_operation: "订单审核"' in descriptor
    assert not (scripts / "shared_math.md").exists()  # 库模块没有描述符
    assert (scripts / "broken.md").read_text(encoding="utf-8") == "last good"  # 契约无效时不动
    assert script_descriptor.sync_workspace_descriptors(ontology) == []  # 幂等


def test_descriptor_issues_report_missing_and_stale_descriptors(tmp_path: Path):
    ontology = _flow_ontology(tmp_path)
    (ontology / "scripts" / "order_review.py").write_text(REVIEW_SCRIPT, encoding="utf-8")
    issues = script_descriptor.descriptor_issues(ontology)
    assert ("scripts/order_review.md", "错误") in {(file, severity) for file, severity, _ in issues}
    script_descriptor.sync_workspace_descriptors(ontology)
    assert [i for i in script_descriptor.descriptor_issues(ontology) if i[1] == "错误"] == []
    (ontology / "scripts" / "order_review.md").write_text("hand edited", encoding="utf-8")
    stale = [message for file, _, message in script_descriptor.descriptor_issues(ontology) if file.endswith(".md")]
    assert stale and "不一致" in stale[0]


# ---- 后端：刷新、保存与导出 ----


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    return WorkspaceManager(tmp_path / "data", assets)


def _flow_project(manager: WorkspaceManager, *, discount_standalone: str = "") -> tuple[str, Path]:
    meta = manager.create_project("订单流程")
    root = manager.project_root(meta["id"])
    for name, text in {
        "订单审核": _operation("订单审核", callees="[[库存校验]]、[[折扣计算]]"),
        "库存校验": _operation("库存校验", callers="[[订单审核]]"),
        "折扣计算": _operation("折扣计算", callers="[[订单审核]]", standalone=discount_standalone),
    }.items():
        (root / "ontology" / "operation" / f"{name}.md").write_text(text, encoding="utf-8")
    return meta["id"], root


def test_package_layout_refresh_writes_descriptors(manager: WorkspaceManager):
    pid, root = _flow_project(manager)
    (root / "ontology" / "scripts" / "order_review.py").write_text(REVIEW_SCRIPT, encoding="utf-8")
    manager.ensure_package_layout(pid)  # 文件树只读（契约 F-01），描述符由目录规范化刷新
    assert (root / "ontology" / "scripts" / "order_review.md").is_file()
    index = (root / "ontology" / "scripts" / "index.md").read_text(encoding="utf-8")
    assert "| order_review.md | 校验库存并计算折扣，给出订单审核结论 |" in index


@pytest.mark.skipif(GIT is None, reason="需要 git 查看保存产生的提交")
def test_web_save_commits_script_with_descriptor_and_rejects_descriptor_edits(manager: WorkspaceManager):
    pid, root = _flow_project(manager)
    result = manager.write_file(pid, "ontology/scripts/order_review.py", REVIEW_SCRIPT, author="alice")
    descriptor = root / "ontology" / "scripts" / "order_review.md"
    assert descriptor.is_file()
    # 安全审查：argv 是 shutil.which 解析出的 git 路径加写死的只读子命令与保存返回的提交号；
    # 以序列传入、不经 shell 解析，只读取 tmp_path 下的临时工作区仓库。
    committed = subprocess.run(  # NOSONAR(S4721)
        [GIT, "show", "--name-only", "--format=", result["commit"]],
        cwd=root, capture_output=True, text=True, check=True, shell=False, timeout=60,
    ).stdout.split()
    assert {"ontology/scripts/order_review.py", "ontology/scripts/order_review.md"} <= set(committed)
    with pytest.raises(WorkspaceError, match="自动生成"):
        manager.write_file(pid, "ontology/scripts/order_review.md", "hand edited")


def test_export_declares_sub_operations_and_real_operation_index(manager: WorkspaceManager, tmp_path: Path):
    pid, root = _flow_project(manager)
    (root / "ontology" / "scripts" / "order_review.py").write_text(REVIEW_SCRIPT, encoding="utf-8")
    dest = tmp_path / "pkg.zip"
    publish_service.export_package_zip(manager, pid, dest)
    with zipfile.ZipFile(dest) as zf:
        main = zf.read(f"{pid}/operations/订单审核.md").decode("utf-8")
        assert 'operations: ["库存校验", "折扣计算"]' in main
        stage = zf.read(f"{pid}/operations/库存校验.md").decode("utf-8")
        assert "operations:" not in stage.split("---")[1]
        index = zf.read(f"{pid}/operations/index.md").decode("utf-8")
        assert "| 订单审核 | 订单审核 | orders | conclusion |" in index
        assert "| 库存校验 | 库存校验 | orders | conclusion（「订单审核」流程中的一步） |" in index
        descriptor = zf.read(f"{pid}/scripts/order_review.md").decode("utf-8")
        workspace_copy = (root / "ontology" / "scripts" / "order_review.md")
        manager.ensure_package_layout(pid)  # 文件树只读（契约 F-01），描述符由目录规范化刷新
        assert descriptor == workspace_copy.read_text(encoding="utf-8")  # 工作区看到的就是导出的
        assert f"{pid}/scripts/stock_check.md" not in zf.namelist()


def test_export_rejects_stage_only_sub_operation_script(manager: WorkspaceManager, tmp_path: Path):
    pid, root = _flow_project(manager)
    (root / "ontology" / "scripts" / "order_review.py").write_text(REVIEW_SCRIPT, encoding="utf-8")
    (root / "ontology" / "scripts" / "stock_check.py").write_text(_script("库存校验"), encoding="utf-8")
    with pytest.raises(WorkspaceError) as exc:
        publish_service.export_package_zip(manager, pid, tmp_path / "pkg.zip")
    assert exc.value.code == "script_binding_conflict"
    assert not (tmp_path / "pkg.zip").exists()  # 写包之前拦下，不留半成品
