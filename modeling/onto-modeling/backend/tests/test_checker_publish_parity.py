"""规则检查与发布检查同口径：发布会拦的，检查器报「错误」；发布不拦的，检查器也不报错。

发布侧走 package_binding_check 的「知识包结构」门禁（consumer 导入规则副本，导出后的包）。
每条规则一例违规、一例按发布口径不拦的写法，外加一份两边都通过的干净样例。
"""

import dataclasses
import importlib
from pathlib import Path

import pytest

from app import config
from app.kernel.changesets import gates
from app.services import publish as publish_service
from app.services.workspace import _git
from tests.publish_fixtures import complete_publish_stages
from tests.test_publish_binding_gate import commit_all, manager, write_schema  # noqa: F401  （夹具按名字注入）

pytest.importorskip("openpyxl")

SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"
PARITY_CATEGORIES = {"命名约束", "容量上限", "字段名称冲突"}

OBJECT = """# 对象描述
合成订单。

# 对象别名
无

# 英文名称
Order

# 数据来源
{source}

{heading} 对象属性清单
| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |
| --- | --- | --- | --- | --- | --- | --- |
{rows}
# 关联对象
无
"""

OPERATION = """# 业务描述
按订单号查询订单金额。

# 与operation的关联
- 被operation调用 > 无
- 调用operation > 无

# 与object的关联
- 输入数据的对象：[[订单]]
- 输出数据的对象：无
- 支撑数据的对象：无

# 输入
| 参数名称 | 描述 |
| --- | --- |
{inputs}
# 输出
| 参数名称 | 描述 |
| --- | --- |
| 订单金额 | 订单的金额 |

# 逻辑描述
按订单号读取[[订单]]，返回订单金额。
"""

CLEAN_FIELDS = [("订单号", "ORDER_ID", "是"), ("金额", "AMOUNT", "否")]


def object_doc(fields, source="T_ORDER", heading="#") -> str:
    rows = "".join(f"| {cn} | {key} | 合成字段 | {primary} | 字符串 | 无 | 无 |\n" for cn, key, primary in fields)
    return OBJECT.format(source=source, heading=heading, rows=rows)


def numbered_fields(count: int, unnamed: int = 0) -> list:
    """count 个属性，前 unnamed 个不填英文名称，最后一个是主键。"""
    return [(f"字段{i}", "" if i < unnamed else f"F{i}", "是" if i == count - 1 else "否") for i in range(count)]


def operation_doc(inputs=("订单号",)) -> str:
    return OPERATION.format(inputs="".join(f"| {name} | 合成参数 |\n" for name in inputs))


def padded_operation(chars: int) -> str:
    """原文正好 chars 个字符的操作文档。"""
    base = operation_doc()
    return base + "读" * (chars - len(base) - 1) + "\n"


def clean():
    return {"订单": object_doc(CLEAN_FIELDS)}, {"查询订单": operation_doc()}


def with_object(text):
    def build():
        _, operations = clean()
        return {"订单": text}, operations
    return build


def many_objects(count):
    def build():
        _, operations = clean()
        objects = {f"对象{i:03d}": object_doc(numbered_fields(1), source="无") for i in range(count - 1)}
        return {"订单": object_doc(CLEAN_FIELDS), **objects}, operations
    return build


def many_operations(count, inputs=("订单号",)):
    def build():
        objects, _ = clean()
        return objects, {f"操作{i:03d}": operation_doc(inputs) for i in range(count)}
    return build


def with_operation(text):
    def build():
        objects, _ = clean()
        return objects, {"查询订单": text}
    return build


LONG_INPUTS = tuple(f"很长很长的输入参数名称第{j:02d}号" for j in range(12))

# (场景, 写法, 检查器的错误类别, 发布检查的拦截原因片段)
BLOCKED = [
    ("英文名称重复（未绑表）",
     with_object(object_doc([("订单号", "ORDER_ID", "是"), ("订单编号", "ORDER_ID", "否")], source="无")),
     "命名约束", "字段 key“ORDER_ID”重复"),
    ("英文名称只差大小写（绑表）",
     with_object(object_doc([("订单号", "ORDER_ID", "是"), ("金额", "AMOUNT", "否"), ("订单编号", "order_id", "否")])),
     "命名约束", "字段 key“ORDER_ID”重复"),
    ("英文名称全空：导出时原表照发",
     with_object(object_doc(numbered_fields(2, unnamed=2), source="无")),
     "命名约束", "存在未填写字段 key 的字段定义"),
    ("属性清单不是一级标题：导出时原表照发",
     with_object(object_doc(numbered_fields(3, unnamed=1), source="无", heading="##")),
     "命名约束", "存在未填写字段 key 的字段定义"),
    ("对象 101 个：对象目录也按属性上限查", many_objects(101),
     "容量上限", "index.md: 单实体属性数量超过上限: 101 / 100"),
    ("操作 101 个", many_operations(101), "容量上限", "单本体流程数量已达上限: 101 / 100"),
    ("70 个操作的目录超过 15000 字符", many_operations(70, LONG_INPUTS),
     "容量上限", "index.md: 单个逻辑长度超过上限"),
    ("操作原文不到 15000 字符、加上发布补的文件头超过", with_operation(padded_operation(14990)),
     "容量上限", "查询订单.md: 单个逻辑长度超过上限"),
    ("单个对象 101 个属性", with_object(object_doc(numbered_fields(101), source="无")),
     "容量上限", "订单.md: 单实体属性数量超过上限: 101 / 100"),
]

# 发布不拦的写法：检查器也不能报这几类错误
PASSED = [
    ("部分英文名称为空（未绑表）：导出丢掉这一行",
     with_object(object_doc([("订单号", "ORDER_ID", "是"), ("备注", "", "否")], source="无"))),
    ("部分英文名称为空（绑表）：导出丢掉这一行",
     with_object(object_doc([*CLEAN_FIELDS, ("备注", "", "否")]))),
    ("对象正好 100 个", many_objects(100)),
    ("102 行属性、2 行没填英文名称：发布包里 100 个",
     with_object(object_doc(numbered_fields(102, unnamed=2), source="无"))),
    ("操作正好 100 个", many_operations(100)),
    ("操作发布后不到 15000 字符", with_operation(padded_operation(14900))),
]


def _project(manager, build):
    objects, operations = build()
    pid = manager.create_project("发布口径")["id"]
    root = manager.project_root(pid)
    write_schema(root, {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    for folder, docs in (("object", objects), ("operation", operations)):
        (root / "ontology" / folder).mkdir(parents=True, exist_ok=True)
        for key, text in docs.items():
            (root / "ontology" / folder / f"{key}.md").write_text(text, encoding="utf-8")
    complete_publish_stages(root, commit=False)
    commit_all(root)
    return pid, root


def _structure(manager, pid) -> dict:
    gates_found = publish_service.package_binding_check(manager, pid)["gates"]
    gate = next(item for item in gates_found if item["id"] == "package_structure")
    return {"status": gate["status"], "messages": [item["message"] for item in gate.get("findings") or []]}


@pytest.fixture
def checker(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    module = importlib.import_module("ontology_checker")

    def errors(root: Path) -> list[dict]:
        issues = [dataclasses.asdict(issue) for issue in module.run_checks(root / "ontology")]
        return [issue for issue in issues if issue["severity"] == "错误"]
    return errors


def test_clean_sample_passes_both(manager, checker):
    pid, root = _project(manager, clean)
    assert checker(root) == []
    assert _structure(manager, pid) == {"status": "pass", "messages": []}


@pytest.mark.parametrize("build,category,reason", [case[1:] for case in BLOCKED], ids=[case[0] for case in BLOCKED])
def test_publish_blocks_and_checker_reports_error(manager, checker, build, category, reason):
    pid, root = _project(manager, build)
    structure = _structure(manager, pid)
    assert structure["status"] == "fail"
    assert any(reason in message for message in structure["messages"]), structure
    found = [issue for issue in checker(root) if issue["category"] == category]
    assert found, f"发布拦了（{structure['messages']}），检查器却没报「{category}」错误"
    assert all("发布会被拒" in issue["message"] for issue in found)


@pytest.mark.parametrize("build", [case[1] for case in PASSED], ids=[case[0] for case in PASSED])
def test_publish_passes_and_checker_stays_quiet(manager, checker, build):
    pid, root = _project(manager, build)
    assert _structure(manager, pid) == {"status": "pass", "messages": []}
    assert [issue for issue in checker(root) if issue["category"] in PARITY_CATEGORIES] == []


def test_merge_gate_checks_script_contract(tmp_path):
    """合入检查带 --include-scripts：脚本契约与知识包导出同一份实现，合入时就报，不等发布。"""
    scripts = tmp_path / "ontology" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "calc.py").write_text(
        '"""\n关联operation：无\n输入参数：\n    order_id    string    订单号，必填\n"""\n'
        "import argparse\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--other-id', required=True)\n",
        encoding="utf-8",
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.name=synthetic", "-c", "user.email=synthetic@example.com",
         "commit", "-qm", "test: synthetic script")
    issues = gates.run(tmp_path, _git(tmp_path, "rev-parse", "HEAD^{tree}").strip())
    assert any(item["category"] == "脚本契约" and item["severity"] == "错误" for item in issues), issues
