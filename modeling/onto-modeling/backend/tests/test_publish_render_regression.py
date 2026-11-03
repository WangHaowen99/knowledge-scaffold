"""发布渲染逐字节回归（方案 A8）：同一提交、同一版本参数下，渲染出的包与基准包逐字节相同。

基准包 `tests/fixtures/publish_baseline/*.pkg`（ZIP 字节；仓库忽略 *.zip，故换扩展名） 是发布适配器重构之前用原渲染器
（WorkspaceManager.export_package_zip）对下面的合成项目渲染的结果。重构后的适配器必须渲染出
同样的字节；有意改动包格式时，先确认下游（consumer、modeling 导入）兼容，再用
`ONTO_REGEN_PUBLISH_BASELINE=1` 重新生成基准并在提交说明里写明原因。

项目全部是合成数据；版本号与来源提交作为渲染参数传入，不依赖真实提交哈希。
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

import pytest
import yaml

from app.services import solver_contract
from app.services.publish.ontology_package import ADAPTER
from app.services.publish.spi import ReleaseSource
from app.services.publish.tree import release_tree
from app.services.workspace import WorkspaceManager, _git
from tests.text_versions import write_text_versions

openpyxl = pytest.importorskip("openpyxl")

BASELINE_DIR = Path(__file__).parent / "fixtures" / "publish_baseline"
PROJECT_ID = "render-baseline"
VERSION = "0.7.0"
SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"

FIELDS_HEADER = (
    "# 对象属性清单\n"
    "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |\n"
    "| --- | --- | --- | --- | --- | --- | --- |\n"
)
SCRIPT = (
    '"""\n关联operation：创建订单\n\n功能描述：计算客单价\n\n输入参数：\n'
    "    orders    json    订单列表，必填\n"
    "    factor    float   加权因子，默认 1.0\n\n输出：\n    return 客单价数值\n\"\"\"\n\n"
    "import argparse\n"
    "def run(orders, factor):\n    return 0\n"
    "def _cli():\n    parser = argparse.ArgumentParser()\n"
    "    parser.add_argument('--orders', required=True, type=json.loads)\n"
    "    parser.add_argument('--factor', default=1.0, type=float)\n"
    "    run(**vars(parser.parse_args()))\n"
)
SIDECARS = {
    "model.yaml": b"id: route_plan\nprofile_id: ordered_binary_route.v1\nfact_source:\n"
    b"  kind: external_snapshot\n  fail_closed: true\n",
    "input.schema.json": b'{"type": "object", "properties": {"window": {"type": "integer", "minimum": 1}}}\n',
    "result.schema.json": b'{"type": "object", "properties": {"candidates": {"type": "array"}}}\n',
    "fact-snapshot.schema.json": b'{"type": "object", "required": ["sites"]}\n',
    "presentation.yaml": b"title: Route plan\ncolumns: [site, score]\n",
}
EVALUATOR = '"""声明式评估器（合成示例）\n\n关联operation：无\n"""\n\nTABLE_SOURCES = {}\n'


def _object_doc(source: str | None, columns: list[str], extra: str = "") -> str:
    rows = "".join(f"| 字段{i} | {name} | 合成字段 | 否 | 字符串 | 无 | 无 |\n" for i, name in enumerate(columns))
    head = "# 对象描述\n合成对象。\n\n# 英文名称\nOrder\n\n"
    return head + (f"# 数据来源\n{source}\n\n" if source is not None else "") + FIELDS_HEADER + rows + extra


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_schema(root: Path, sheets: dict[str, list[str]]) -> None:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, columns in sheets.items():
        workbook.create_sheet(name).append(columns)
    (root / "resource" / "excel").mkdir(parents=True, exist_ok=True)
    workbook.save(root / "resource" / "excel" / "结构.xlsx")
    write_text_versions(root)  # 资料区的 Excel 只读文本版（F4c）
    _write(root, "resource/materials.json", json.dumps({"resource/excel/结构.xlsx": {"purpose": "schema"}}))


def _create(manager: WorkspaceManager, name: str) -> Path:
    """建项目后改成固定的项目 ID：包内路径以项目 ID 开头，基准要求它不随机。"""
    meta = manager.create_project(name)
    source = manager.project_root(meta["id"])
    root = manager.workspaces_dir / PROJECT_ID
    source.rename(root)
    meta_path = root / ".onto-web.json"
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    data["id"] = PROJECT_ID
    meta_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return root


def _ordinary(manager: WorkspaceManager) -> Path:
    root = _create(manager, "渲染基准")
    _write_schema(root, {"T_ORDER": ["ORDER_ID", "AMOUNT", "CLIENT_ID"], "T_CLIENT": ["CLIENT_ID", "NAME"]})
    _write(root, "ontology/object/订单.md", _object_doc(
        "T_ORDER", ["ORDER_ID", "AMOUNT", "CLIENT_ID"],
        "\n# 关联对象\n- 当前对象 → 属于 → [[客户]]\n",
    ))
    _write(root, "ontology/object/客户.md", _object_doc("T_CLIENT", ["CLIENT_ID", "NAME"]))
    _write(root, "ontology/logic/创建订单.md", "# 创建订单\n\n生成[[订单]]，关联[[客户]]\n")
    _write(root, "ontology/operation/取消订单.md", "# 取消订单\n\n1. 调用[[创建订单]]的逆操作\n2. 更新[[订单]]\n")
    _write(root, "ontology/rule/限购规则.md", "# 限购规则\n\n约束[[订单]]数量\n")
    _write(root, "ontology/metric/客单价.md", "# 客单价\n\n基于[[订单]]金额\n")
    _write(root, "ontology/scripts/calc_price.py", SCRIPT)
    return root


def _degraded(manager: WorkspaceManager) -> Path:
    root = _ordinary(manager)
    _write(root, "ontology/object/发票.md", _object_doc("T_INVOICE", ["INVOICE_ID"]))
    return root


def _solver(manager: WorkspaceManager) -> Path:
    root = _create(manager, "求解基准")
    _write(root, "ontology/object/站点.md", "# 站点\n")
    _write(root, "ontology/operation/规划路线.md", "# 规划路线\n\n排序[[站点]]\n")
    _write(root, "ontology/scripts/route_eval.py", EVALUATOR)
    directory = root / solver_contract.SOLVER_DIR / "plan_route"
    directory.mkdir(parents=True)
    for filename, data in SIDECARS.items():
        (directory / filename).write_bytes(data)
    binding = {
        "solver_key": "plan_route",
        "operation": "规划路线",
        "decision_model": "route_plan",
        "declaration": {"engine_requirements": {"problem_family": "binary_ordered_route.v1"}},
        "operation_frontmatter": {"execution": {"enabled": True}},
        "units": [{"field": "distance", "unit": "km"}],
    }
    (directory / "binding.yaml").write_text(yaml.safe_dump(binding, allow_unicode=True), encoding="utf-8")
    return root


# 名称 → (建项目, 渲染参数)
CASES = {
    "standard": (_ordinary, {}),
    "graph": (_ordinary, {"graph": True}),
    "degraded": (_degraded, {"allow_unbound": True}),
    "solver": (_solver, {}),
    "solver-draft": (_solver, {"draft": True}),
}


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    return WorkspaceManager(tmp_path / "data", assets)


def render(manager: WorkspaceManager, root: Path, dest: Path, options: dict) -> bytes:
    """按提交渲染：先提交，再从提交导出的只读树经发布适配器渲染（与发布同一路径）。"""
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", "test: 渲染基准")
    head = _git(root, "rev-parse", "HEAD").strip()
    with release_tree(root, head) as tree:
        src = ReleaseSource(
            project_id=PROJECT_ID, commit=SOURCE_COMMIT, tree=tree, meta=manager.get_project(PROJECT_ID),
            version=VERSION, state_root=root, variant="graph" if options.get("graph") else "standard",
            allow_unbound=options.get("allow_unbound", False), draft=options.get("draft", False),
        )
        ADAPTER.render(src, dest)
    return dest.read_bytes()


def _members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


@pytest.mark.parametrize("case", sorted(CASES))
def test_render_matches_baseline_bytes(manager: WorkspaceManager, tmp_path: Path, case: str):
    build, options = CASES[case]
    root = build(manager)
    rendered = render(manager, root, tmp_path / f"{case}.zip", options)
    baseline = BASELINE_DIR / f"{case}.pkg"
    if os.environ.get("ONTO_REGEN_PUBLISH_BASELINE") == "1":
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_bytes(rendered)
    expected = baseline.read_bytes()
    if rendered != expected:
        got, want = _members(rendered), _members(expected)
        assert sorted(got) == sorted(want), "包内文件清单与基准不同"
        changed = [name for name in sorted(want) if got[name] != want[name]]
        assert not changed, f"与基准内容不同的成员：{changed}"
        pytest.fail("成员内容相同但 ZIP 字节不同（时间戳、权限、压缩或顺序变了）")
