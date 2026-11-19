"""公共本体插件（plugins/common-ontology，上传型插件）：清单、上传包静态检查、清单脚本的预分组、核对脚本。

输入是 tests/fixtures/common-ontology/ 下三个完全合成的知识包（图书借阅、工具借用、会议室预约），按平台上传位的落盘方式
（ext/common-ontology/uploads/inputs/<上传 id>/ + upload.json）放进临时项目，直接跑包里的脚本——和装配后助手跑的是同一份。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

from app.kernel.artifacts import parsers
from app.kernel.artifacts import registry as artifact_registry
from app.services import package_compare
from app.services.packs import manifest as mf
from tests import proc

APP_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = APP_ROOT / "plugins" / "common-ontology"
SCRIPTS = PLUGIN / "scripts"
FIXTURES = Path(__file__).parent / "fixtures" / "common-ontology"
TOOLKIT_SCRIPTS = APP_ROOT / "assets" / "workspace-toolkit" / "scripts"
UPLOADS = Path("ext/common-ontology/uploads/inputs")
RESULT = Path("ext/common-ontology/result")
LIBRARY, TOOLS, ROOMS = "library-lending", "tool-lending", "room-booking"
ORDER = (LIBRARY, TOOLS, ROOMS)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upload_id(package_id: str) -> str:
    """同内核上传位：可读部分 + 包 id 的摘要（services/packs/files._upload_id）。"""
    slug = re.sub(r"[^a-z0-9]+", "-", package_id.lower()).strip("-")[:40] or "package"
    return f"{slug}-{hashlib.sha256(package_id.encode('utf-8')).hexdigest()[:6]}"


def put_package(root: Path, name: str) -> str:
    """把一个合成知识包按上传位的样子放进项目：原样文本 + upload.json（不留 index.md）。"""
    source = FIXTURES / name
    manifest = yaml.safe_load((source / "manifest.yaml").read_text(encoding="utf-8"))
    target_id = upload_id(manifest["id"])
    target = root / UPLOADS / target_id
    shutil.copytree(source, target)
    counts = {sub: len(list((source / sub).glob("*.md"))) if (source / sub).is_dir() else 0
              for sub in ("objects", "operations", "rules", "metrics", "scripts")}
    meta = {"id": target_id, "slot": "inputs", "filename": f"{name}.zip",
            "package": {"id": manifest["id"], "name": manifest["name"], "version": str(manifest["version"])},
            "counts": counts, "files": sum(counts.values()) + 1, "size": 1, "digest": f"synthetic-{name}",
            "uploaded_at": 1_700_000_000 + ORDER.index(name) * 60, "uploaded_by": "tester"}
    (target / "upload.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return target_id


def make_project(tmp_path: Path, packs=ORDER, *, checker: bool = False) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    for name in packs:
        put_package(root, name)
    if checker:
        # 装配后 .claude/scripts 里有 core 的 ontology_checker：co_check.py 会顺带跑它
        (root / ".claude").mkdir()
        (root / ".claude" / "scripts").symlink_to(TOOLKIT_SCRIPTS, target_is_directory=True)
    return root


def run(root: Path, script: str, *args: str):
    return proc.run([sys.executable, str(SCRIPTS / script), *args], cwd=root, capture_output=True, text=True,
                    timeout=180, env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})


def merge(root: Path) -> dict:
    return yaml.safe_load((root / RESULT / "merge.yaml").read_text(encoding="utf-8"))


def by_members(data: dict) -> dict[frozenset, dict]:
    """组 → 成员（包 id 前缀, 条目名）集合，方便断言。"""
    out = {}
    for group in data["groups"]:
        key = frozenset((member["pack"].rsplit("-", 1)[0], member["entry"]) for member in group["members"])
        out[key] = group
    return out


def fill_decisions(root: Path) -> None:
    """模拟助手的语义那一步：术语写定义，规则模板的参数起个有含义的名字。"""
    path = root / RESULT / "merge.yaml"
    text = path.read_text(encoding="utf-8")
    text = text.replace('definition: ""', 'definition: "可以被借走或预约、用完要归还的东西"')
    text = text.replace("{参数1}", "{上限天数}").replace('"参数1"', '"上限天数"')
    path.write_text(text, encoding="utf-8")


# ---- 清单与上传包 ----


def test_manifest_declares_page_editor_and_upload_slot():
    manifest = mf.load(PLUGIN)
    assert (manifest.name, manifest.kind, manifest.label) == ("common-ontology", "capability", "公共本体")
    assert mf.satisfies(mf.PAGE_API, manifest.page_api) and manifest.page_api == ">=1.1 <2.0"
    assert manifest.engines == ["claude", "jiuwen"]
    assert manifest.items("commands") == ["common-ontology-extract"]
    assert manifest.items("scripts") == ["co_inventory.py", "co_check.py"]
    assert manifest.editors() == [{"id": "page", "title": "公共本体", "ui_api": ">=1.1 <2.0"}]
    page = manifest.pages()[0]
    assert (page["id"], page["ui"], page["nav"]) == ("common", "page", {"title": "公共本体", "icon": "layers", "order": 3})
    assert manifest.uploads() == [{"id": "inputs", "title": "输入知识包", "kind": "knowledge-package", "max": 10}]


def test_upload_pack_passes_static_checks_and_builds_under_20_mib(tmp_path):
    build = _load("common_ontology_build_pack", APP_ROOT / "plugins" / "build_pack.py")
    staged = build.stage(PLUGIN, tmp_path / "staged")
    manifest, items = build.check(staged)
    failed = [item for item in items if item["result"] == "fail"]
    assert manifest is not None and not failed, failed
    assert {"frontend", "ui", "entity_types", "views", "names", "official_commands"} <= {item["id"] for item in items}
    target, items = build.build(PLUGIN, tmp_path / "dist")
    assert target is not None, [item for item in items if item["result"] == "fail"]
    assert target.stat().st_size <= 20 * 1024 * 1024
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
    assert {"manifest.yaml", "checksums.sha256", "ui/page/index.html", "scripts/co_check.py"} <= names
    assert not any("__pycache__" in name for name in names)
    assert not (PLUGIN / "checksums.sha256").exists(), "打包不往源目录写校验和"


def test_page_is_static_and_never_injects_platform_data_as_html():
    html = (PLUGIN / "ui" / "page" / "index.html").read_text(encoding="utf-8")
    script = (PLUGIN / "ui" / "page" / "page.js").read_text(encoding="utf-8")
    assert '<script src="page.js"></script>' in html and "<script>" not in html and " on" not in html.split("<body>")[1]
    assert "fetch(" not in script and "XMLHttpRequest" not in script and "eval(" not in script
    assert "onto_ui: UI_API, connect: true" in script and "var UI_API = '1.1'" in script
    # innerHTML 只用来放写死的 SVG 图标
    assert script.count("innerHTML") == 1 and "ICONS[name]" in script


def test_fixture_packages_pass_knowledge_package_validation(tmp_path):
    for name in ORDER:
        archive = tmp_path / f"{name}.zip"
        with zipfile.ZipFile(archive, "w") as writer:
            for path in sorted((FIXTURES / name).rglob("*")):
                if path.is_file():
                    writer.write(path, f"{name}/{path.relative_to(FIXTURES / name).as_posix()}")
        index = package_compare._archive_index_core(archive)
        assert index["package_id"] == name
        categories = {item["category"] for item in index["files"].values()}
        assert {"objects", "operations", "rules", "metadata"} <= categories


# ---- 清单脚本：统一清单与预分组 ----


def test_inventory_groups_synthetic_packages(tmp_path):
    root = make_project(tmp_path)
    result = run(root, "co_inventory.py", "min=2")
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(result.stdout.splitlines()) <= 30
    data = merge(root)
    assert data["min"] == 2
    groups = by_members(data)

    def group(*members):
        return groups[frozenset(members)]

    # 结构一致的对象：三个包都有，属性完全一样
    assert group((LIBRARY, "通知消息"), (TOOLS, "通知消息"), (ROOMS, "通知消息"))["class"] == "real"
    # 叫法不同、含义相同、主键以外有共有属性：实类，组名取用得最多的叫法
    borrower = group((LIBRARY, "读者"), (TOOLS, "借用人"), (ROOMS, "预约人"))
    assert (borrower["class"], borrower["name"]) == ("real", "借用人")
    record = group((LIBRARY, "借阅记录"), (TOOLS, "借用记录"))
    assert (record["class"], record["name"]) == ("real", "借用记录")
    assert group((TOOLS, "押金账户"), (ROOMS, "押金账户"))["class"] == "real"
    # 概念相同、结构不同（只有主键对得上）：虚类，进术语表
    item = group((LIBRARY, "图书"), (TOOLS, "工具"), (ROOMS, "会议室"))
    assert (item["class"], item["name"]) == ("virtual", "借用物品")
    assert group((ROOMS, "预约记录"))["class"] == "none"
    # 步骤一致的操作是实类；同名但步骤不一致的没抽出
    assert group((LIBRARY, "归还"), (TOOLS, "归还"))["class"] == "real"
    assert group((LIBRARY, "发送到期提醒"), (TOOLS, "发送到期提醒"))["class"] == "real"
    lend = group((LIBRARY, "借出"), (TOOLS, "借出"))
    assert lend["class"] == "none" and "不一致" in lend["note"]
    assert "不一致" in group((ROOMS, "发送到期提醒"))["note"]
    # 逻辑相同的规则（各包对象叫法不同、名字不同也认）是实类
    assert group((LIBRARY, "逾期未还禁止再借"), (TOOLS, "逾期未还禁止再借"))["class"] == "real"
    deposit = group((TOOLS, "押金不足禁止借出"), (ROOMS, "押金不足禁止预约"))
    assert (deposit["class"], deposit["name"]) == ("real", "押金不足禁止借出")
    # 只差参数的规则：规则模板，记各包的参数值
    limit = group((LIBRARY, "借用期限上限"), (TOOLS, "借用期限上限"))
    assert limit["class"] == "virtual" and "{参数1}" in limit["template"]
    assert sorted(member["values"]["参数1"] for member in limit["members"]) == ["30", "7"]
    # 每个输入条目都在且只在一组里（指标不在抽取范围）
    members = [(m["pack"], g["kind"], m["entry"]) for g in data["groups"] for m in g["members"]]
    assert len(members) == len(set(members)) == 32
    assert (root / "tmp" / "common-ontology" / "inventory.json").is_file()


def test_inventory_drafts_real_entries_with_intersection_and_aliases(tmp_path):
    root = make_project(tmp_path)
    result = run(root, "co_inventory.py", "--min", "2", "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    borrower = (root / "ontology" / "object" / "借用人.md").read_text(encoding="utf-8")
    assert "| 借用人编号 | borrower_id |" in borrower and "读者编号、预约人编号" in borrower
    assert "借阅证类型" not in borrower and "所属部门" not in borrower
    record = (root / "ontology" / "object" / "借用记录.md").read_text(encoding="utf-8")
    assert "当前对象 → 属于（borrower_id → borrower_id） → [[借用人]]" in record
    assert "[[工具]]" not in record and "[[图书]]" not in record
    rule = (root / "ontology" / "rule" / "逾期未还禁止再借.md").read_text(encoding="utf-8")
    assert "[[借用人]] 名下存在 [[借用记录]]" in rule
    assert sorted(path.stem for path in (root / "ontology").rglob("*.md")) == sorted(
        ["借用人", "借用记录", "押金账户", "通知消息", "归还", "发送到期提醒", "逾期未还禁止再借", "休眠账户须先确认",
         "押金不足禁止借出"])
    # 已有条目不覆盖
    (root / "ontology" / "object" / "通知消息.md").write_text("# 对象描述\n手写的\n", encoding="utf-8")
    run(root, "co_inventory.py", "--apply")
    assert (root / "ontology" / "object" / "通知消息.md").read_text(encoding="utf-8") == "# 对象描述\n手写的\n"


def test_inventory_parses_threshold_arguments():
    module = _load("co_inventory_for_args", SCRIPTS / "co_inventory.py")
    assert module.parse_min(["min=3"]) == 3
    assert module.parse_min(["--min", "4"]) == 4
    assert module.parse_min(["--min=5"]) == 5
    assert module.parse_min(["2"]) == 2
    assert module.parse_min([], 2) == 2


def test_inventory_refuses_without_two_packages(tmp_path):
    root = make_project(tmp_path, packs=(LIBRARY,))
    result = run(root, "co_inventory.py")
    assert result.returncode == 1 and "至少要 2 个" in result.stdout


# ---- 核对脚本 ----


def _extract(root: Path) -> None:
    assert run(root, "co_inventory.py", "min=2").returncode == 0
    fill_decisions(root)
    assert run(root, "co_inventory.py", "--apply").returncode == 0


def test_check_passes_and_renders_registered_artifacts(tmp_path):
    root = make_project(tmp_path, checker=True)
    _extract(root)
    result = run(root, "co_check.py")
    assert result.returncode == 0, result.stdout
    assert result.stdout.startswith("公共本体核对：通过") and len(result.stdout.splitlines()) <= 40
    # 产物按本包登记的类型能被内核解析（页面经 artifact.list 读到的就是这些字段）
    parsed: dict[str, list] = {}
    for path in sorted((PLUGIN / "artifacts").glob("*.yaml")):
        atype = artifact_registry.parse_registration(
            yaml.safe_load(path.read_text(encoding="utf-8")), pack="common-ontology", pack_version="0.1.0",
            official=False, directory=PLUGIN)
        items = []
        for file in sorted((root / atype.glob_root()).glob("*")):
            rel = file.relative_to(root).as_posix()
            if atype.key_from_path(rel) is not None:
                items += parsers.parse_file(atype, root, rel)
        parsed[atype.type] = items
    real = {item.key: item.fields for item in parsed["common-ontology.real"]}
    assert len(real) == 9
    assert real["借用人"]["kind"] == "object" and real["借用人"]["summary"].startswith("属性 4")
    assert real["借用人"]["sources"] == ["图书借阅管理", "工具借用管理", "会议室预约"]
    assert {m["entry"]: m["extra"] for m in real["借用人"]["members"]}["读者"] == "属性：借阅证类型"
    assert real["归还"]["summary"] == "步骤 3"
    term = parsed["common-ontology.term"][0].fields
    assert term["title"] == "借用物品" and [n["name"] for n in term["names"]] == ["图书", "工具", "会议室"]
    template = parsed["common-ontology.template"][0].fields
    assert template["params"][0]["name"] == "上限天数" and len(template["instances"]) == 2
    mapping = {item.fields["package_id"]: item.fields for item in parsed["common-ontology.mapping"]}
    assert sum(len(fields["rows"]) for fields in mapping.values()) == 32
    library = {row["entry"]: row for row in mapping[LIBRARY]["rows"]}
    assert (library["读者"]["common"], library["读者"]["class"]) == ("借用人", "real")
    assert library["借阅记录"]["extra"] == "关系：借阅→图书"
    assert library["借用期限上限"]["extra"] == "参数：上限天数=30"
    assert library["借出"]["class"] == "none" and library["借出"]["note"]
    status = parsed["common-ontology.status"][0].fields
    assert (status["state"], status["min"], status["real_count"], status["entry_count"]) == ("passed", 2, 9, 32)


def test_check_catches_missing_mapping(tmp_path):
    root = make_project(tmp_path)
    _extract(root)
    path = root / RESULT / "merge.yaml"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(line for line in lines if 'entry: "预约人"' not in line), encoding="utf-8")
    result = run(root, "co_check.py")
    assert result.returncode == 1
    assert "没有映射：会议室预约/预约人（对象）" in result.stdout
    status = json.loads((root / RESULT / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "failed" and status["errors"] >= 1


def test_check_catches_attribute_outside_intersection_and_stale_entries(tmp_path):
    root = make_project(tmp_path)
    _extract(root)
    assert run(root, "co_check.py").returncode == 0
    path = root / "ontology" / "object" / "借用人.md"
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n") + "\n| 会员等级 | level | 等级 | 否 | 枚举 | 无 | 无 |\n",
                    encoding="utf-8")
    result = run(root, "co_check.py")
    assert result.returncode == 1 and "「会员等级」在 图书借阅管理/读者 里对不上" in result.stdout
    # 门槛升到 3：只有两个包有的实类自动改成没抽出，它们在 ontology/ 里的条目要删
    path.write_text("\n".join(path.read_text(encoding="utf-8").splitlines()[:-1]) + "\n", encoding="utf-8")
    assert run(root, "co_inventory.py", "min=3").returncode == 0
    result = run(root, "co_check.py")
    assert result.returncode == 1 and "ontology/object/借用记录.md 上次是实类、这次不是了：删掉它" in result.stdout
    assert run(root, "co_check.py").returncode == 1, "没删之前一直报"
    # 门槛降回 2：原来的实类、规则模板连同定义原样恢复
    assert run(root, "co_inventory.py", "min=2").returncode == 0
    assert run(root, "co_check.py").returncode == 0
    assert "{上限天数}" in (root / RESULT / "merge.yaml").read_text(encoding="utf-8")


def test_new_package_joins_existing_decisions(tmp_path):
    root = make_project(tmp_path, packs=(LIBRARY, TOOLS))
    _extract(root)
    assert run(root, "co_check.py").returncode == 0
    put_package(root, ROOMS)
    result = run(root, "co_inventory.py", "--apply")
    assert result.returncode == 0, result.stdout
    data = merge(root)
    groups = by_members(data)
    borrower = groups[frozenset({(LIBRARY, "读者"), (TOOLS, "借用人"), (ROOMS, "预约人")})]
    assert borrower["class"] == "real"
    item = groups[frozenset({(LIBRARY, "图书"), (TOOLS, "工具"), (ROOMS, "会议室")})]
    assert item["definition"] == "可以被借走或预约、用完要归还的东西"
    # 只在一个包里时没抽出的押金账户，第三个包也有了：重新判成实类
    assert groups[frozenset({(TOOLS, "押金账户"), (ROOMS, "押金账户")})]["class"] == "real"
    assert "新加入：会议室预约/预约人" in (root / RESULT / "merge.yaml").read_text(encoding="utf-8")
    result = run(root, "co_check.py")
    assert result.returncode == 0, result.stdout


def test_rerun_splits_identical_operations_out_of_a_none_group(tmp_path):
    """助手把逐字一致的操作并进了不一致的组、整组判成没抽出（还写了自己的原因）：重跑时按公共叫法再比，
    一致又够门槛的部分拆出来定为实类，其余留在原组。"""
    root = make_project(tmp_path)
    assert run(root, "co_inventory.py", "min=2").returncode == 0
    fill_decisions(root)
    path = root / RESULT / "merge.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    groups = data["groups"]
    reminders = [g for g in groups if g["kind"] == "operation" and any(m["entry"] == "发送到期提醒" for m in g["members"])]
    merged = {**reminders[0], "class": "none", "name": "发送到期提醒", "note": "查找条件和目标对象不同",
              "members": [m for g in reminders for m in g["members"]]}
    returns = next(g for g in groups if g["kind"] == "operation" and any(m["entry"] == "归还" for m in g["members"]))
    returns.update({"class": "none", "note": "前置条件不一致"})
    data["groups"] = [g for g in groups if g not in reminders] + [merged]
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    result = run(root, "co_inventory.py", "min=2", "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "拆出实类「发送到期提醒」" in result.stdout and "拆出实类「归还」" in result.stdout
    after = by_members(merge(root))
    assert after[frozenset({(LIBRARY, "发送到期提醒"), (TOOLS, "发送到期提醒")})]["class"] == "real"
    rest = after[frozenset({(ROOMS, "发送到期提醒")})]
    assert rest["class"] == "none" and rest["note"] == "查找条件和目标对象不同"
    assert after[frozenset({(LIBRARY, "归还"), (TOOLS, "归还")})]["class"] == "real"
    assert (root / "ontology" / "operation" / "发送到期提醒.md").is_file()
