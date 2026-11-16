"""领域包扩展的脚本（ADR-0099）：锁定底座、底座清单、新增记录、底座核对与撤回。

在合成的 git 仓库里直接跑包里的脚本，和装配后助手跑的是同一份。底座是一次合成的「导入已发布知识包」提交，
条目全是合成的订单、下单。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from app import config
from tests import proc
from tests.proc import git

ASSETS = Path(config.ASSETS_DIR)
SCRIPT = ASSETS / "packs" / "domain-extend" / "scripts" / "domain_extend.py"
BIND, ADD, CHECK = "domain-extend-bind", "domain-extend-add", "domain-extend-check"
SCOPE = "ext/domain-extend/scope.json"

ORDER = "ontology/object/订单.md"
PLACE = "ontology/operation/下单.md"
LIMIT = "ontology/rule/下单金额上限.md"
DAILY = "ontology/metric/日订单量.md"
CODE = "ontology/scripts/下单.py"
BASE = {
    ORDER: "# 订单\n\n## 对象描述\n\n合成订单\n",
    PLACE: "## 逻辑描述\n\n读取[[订单]]。\n",
    LIMIT: "# 下单金额上限\n\n限制[[下单]]的金额。\n",
    DAILY: "# 日订单量\n\n统计[[订单]]。\n",
    CODE: "print('合成')\n",
    # 派生物：不算底座条目
    "ontology/object/index.md": "| 对象 |\n| --- |\n| [[订单]] |\n",
}
MANIFEST = 'kind: ontology-package\nschema_version: \'0.1\'\nid: synthetic-orders\nname: "合成订单领域"\nversion: 1.2.0\n'


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit(root: Path, message: str = "合成提交") -> str:
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD").strip()


def make_repo(root: Path, *, imported: bool = True, extra: int = 0) -> Path:
    root.mkdir(parents=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "tester@example.com")
    git(root, "config", "user.name", "tester")
    write(root, ".gitignore", ".ontology-state/\n")
    write(root, "README.md", "# 合成项目\n")
    commit(root, "init")
    if imported:
        write(root, ".imported-package/manifest.yaml", MANIFEST)
    for rel, text in BASE.items():
        write(root, rel, text)
    for index in range(extra):
        write(root, f"ontology/object/合成对象{index:02d}.md", f"# 合成对象{index:02d}\n")
    commit(root, "import: 导入已发布知识包")
    return root


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "workspace")


def lock(root: Path, command: str) -> None:
    write(root, f".ontology-state/lock-{command}.json", "{}")


def run(root: Path, *args: str):
    return proc.run([sys.executable, str(SCRIPT), *args], cwd=root, capture_output=True, text=True,
                    env=dict(os.environ), timeout=60)


def ok(root: Path, *args: str) -> str:
    done = run(root, *args)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def read_json(root: Path, rel: str) -> dict:
    return json.loads((root / rel).read_text(encoding="utf-8"))


def bound(root: Path) -> dict:
    """锁定并合入主线（范围文件要在主线上才算数）。"""
    lock(root, BIND)
    ok(root, "bind")
    commit(root, "锁定底座")
    return read_json(root, SCOPE)


def checked(root: Path) -> tuple[str, dict]:
    lock(root, CHECK)
    out = ok(root, "check")
    return out, read_json(root, "ext/domain-extend/check.json")


# ---- 锁定 ----


def test_writing_commands_need_their_lock(repo):
    for action, command in (("bind", BIND), ("added", ADD), ("check", CHECK), ("restore", CHECK)):
        done = run(repo, action)
        assert done.returncode == 1 and f"/{command}" in done.stderr
    assert not (repo / "ext").exists()


def test_bind_locks_every_imported_entry_and_nothing_else(repo):
    imported = git(repo, "rev-parse", "HEAD").strip()
    lock(repo, BIND)
    out = ok(repo, "bind")
    assert "合成订单领域" in out and "1.2.0" in out and "底座条目 5 个" in out
    scope = read_json(repo, SCOPE)
    assert scope["status"] == "active" and scope["editable"] == []
    assert scope["baseline"]["commit"] == imported == scope["base"]["import_commit"]
    assert scope["base"]["package"] == {"id": "synthetic-orders", "name": "合成订单领域", "version": "1.2.0"}
    base = read_json(repo, "ext/domain-extend/base.json")
    assert sorted(item["path"] for item in base["entries"]) == sorted(set(BASE) - {"ontology/object/index.md"})
    assert {item["kind"] for item in base["entries"]} == {"对象", "操作", "规则", "指标", "脚本"}
    text = (repo / "ext/domain-extend/base.md").read_text(encoding="utf-8")
    assert "对象 订单（ontology/object/订单.md）" in text and "[ ]" not in text


def test_bind_refuses_a_project_that_was_not_imported(tmp_path):
    root = make_repo(tmp_path / "plain", imported=False)
    lock(root, BIND)
    done = run(root, "bind")
    assert done.returncode == 1 and "不是从已发布的领域本体包导入的" in done.stderr
    assert not (root / SCOPE).exists()


def test_rebinding_after_a_release_keeps_only_the_base_read_only(repo):
    bound(repo)
    write(repo, "ontology/object/订单配送扩展.md", "# 订单配送扩展\n\n关联[[订单]]。\n")
    released = commit(repo, "新增扩展")
    git(repo, "tag", "-a", "release/v0.1.0", "-m", "发布 0.1.0", released)
    _, report = checked(repo)
    assert report["locked"] is False and "发布之后" in report["lock_reason"]

    scope = bound(repo)
    assert scope["baseline"]["commit"] == released
    assert scope["editable"] == ["ontology/object/订单配送扩展.md"]
    _, report = checked(repo)
    assert report["locked"] is True


# ---- 只读查询 ----


def test_status_and_catalog(repo):
    out = ok(repo, "status")
    assert "底座条目 5 个" in out and "还没锁定" in out and "底座被改动：0 处" in out
    bound(repo)
    assert "已锁定" in ok(repo, "status")
    out = ok(repo, "catalog", "--kind", "对象")
    assert "订单（ontology/object/订单.md）" in out and "下单" not in out
    assert "下单金额上限" in ok(repo, "catalog", "--kind", "rule")
    assert json.loads(ok(repo, "catalog", "--q", "下单", "--json"))[0]["path"] in {PLACE, LIMIT, CODE}
    done = run(repo, "catalog", "--kind", "表格")
    assert done.returncode == 1 and "类型只能是" in done.stderr


def test_catalog_output_is_capped(tmp_path):
    root = make_repo(tmp_path / "big", extra=40)
    out = ok(root, "catalog")
    assert len([line for line in out.splitlines() if line.startswith("- ")]) == 20
    assert "还有 25 条没列出" in out


# ---- 新增 ----


def test_added_lists_new_entries_and_their_links_to_the_base(repo):
    bound(repo)
    lock(repo, ADD)
    out = ok(repo, "added")
    assert "还没有新增条目" in out and not (repo / "ext/domain-extend/added.json").exists()
    write(repo, "ontology/object/订单配送扩展.md", "# 订单配送扩展\n\n关联[[订单|订单号]]，见[[不存在的对象]]。\n")
    write(repo, "ontology/operation/限时配送.md", "## 逻辑描述\n\n调用[[下单]]逻辑文档，再读取[[订单配送扩展]]。\n")
    write(repo, "ontology/scripts/限时配送.py", "print('合成')\n")
    write(repo, "ontology/scripts/限时配送.md", "# 限时配送\n\n脚本描述符\n")
    out = ok(repo, "added")
    # 脚本和同名描述符算一个条目
    assert "新增条目 3 个（对象 1、操作 1、脚本 1）" in out
    assert "订单配送扩展（ontology/object/订单配送扩展.md）→ 订单" in out
    entries = {item["path"]: item for item in read_json(repo, "ext/domain-extend/added.json")["entries"]}
    assert entries["ontology/object/订单配送扩展.md"]["refs_to_base"] == ["订单"]
    assert entries["ontology/object/订单配送扩展.md"]["unresolved"] == ["不存在的对象"]
    operation = entries["ontology/operation/限时配送.md"]
    assert operation["refs_to_base"] == ["下单"] and operation["unresolved"] == [] and operation["shadows"] is None


# ---- 核对与撤回 ----


def test_check_passes_when_the_base_is_untouched(repo):
    bound(repo)
    write(repo, "ontology/rule/超时配送.md", "# 超时配送\n\n引用[[订单]]。\n")
    out, report = checked(repo)
    assert "被改动 0 处" in out and report["violations"] == [] and report["locked"] is True
    text = (repo / "ext/domain-extend/check.md").read_text(encoding="utf-8")
    assert "[ ]" not in text and "可以发布" in text and "新增条目：1 个" in text


def test_check_lists_every_change_to_the_base_including_uncommitted_ones(repo):
    bound(repo)
    write(repo, ORDER, BASE[ORDER] + "\n## 补充\n\n人工加的一段。\n")      # 改了（没提交也算）
    (repo / DAILY).unlink()                                            # 删了
    (repo / LIMIT).rename(repo / "ontology/rule/金额上限.md")          # 改名
    write(repo, "ontology/operation/订单.md", "## 逻辑描述\n\n合成\n")   # 新增条目和底座对象同名
    out, report = checked(repo)
    changes = {item["path"]: (item["change"], item["to"]) for item in report["violations"]}
    assert changes == {ORDER: ("modified", None), DAILY: ("deleted", None),
                       LIMIT: ("moved", "ontology/rule/金额上限.md")}
    text = (repo / "ext/domain-extend/check.md").read_text(encoding="utf-8")
    assert text.count("- [ ] 底座") == 3 and "被移动或改名到 ontology/rule/金额上限.md" in text
    assert "- [ ] ontology/operation/订单.md 和底座条目 ontology/object/订单.md 同名" in text
    assert "被改动 3 处" in out and "和底座重名 1 个" in out


def test_scripts_cases_and_mocks_named_after_base_entries_are_not_shadows(repo):
    """脚本、用例、测试数据按惯例和它服务的操作、对象同名：给底座操作补脚本、给底座对象补测试数据不算重名。"""
    bound(repo)
    write(repo, "ontology/scripts/派送.py", "print('合成')\n")
    write(repo, "ontology/testcase/下单.md", "# 下单\n\n合成用例\n")
    write(repo, "ontology/data-mock/订单.md", "# 订单\n\n| 编号 |\n| --- |\n| D001 |\n")
    _, report = checked(repo)
    assert [item["shadows"] for item in report["extension"]] == [None, None, None]
    assert "[ ]" not in (repo / "ext/domain-extend/check.md").read_text(encoding="utf-8")


def test_restore_puts_the_imported_bytes_back_and_deletes_nothing(repo):
    bound(repo)
    write(repo, ORDER, "# 订单\n\n改坏了\n")
    (repo / DAILY).unlink()
    write(repo, "ontology/object/订单配送扩展.md", "# 订单配送扩展\n")
    lock(repo, CHECK)
    out = ok(repo, "restore")
    assert "已把 2 个底座条目写回" in out
    assert (repo / ORDER).read_text(encoding="utf-8") == BASE[ORDER]
    assert (repo / DAILY).read_text(encoding="utf-8") == BASE[DAILY]
    assert (repo / "ontology/object/订单配送扩展.md").is_file()
    _, report = checked(repo)
    assert report["violations"] == []
    assert "不用撤回" in ok(repo, "restore")


def test_check_wants_the_lock_back_after_a_release(repo):
    scope = bound(repo)
    git(repo, "tag", "release/v0.1.0", scope["base"]["import_commit"])
    _, report = checked(repo)
    assert report["locked"] is True  # 标签就在基线上：还没「基线之后发布」
    git(repo, "tag", "-d", "release/v0.1.0")  # 同一秒打两个标签时排序不定（内核同一口径），这里只留后一个
    write(repo, "ontology/object/订单配送扩展.md", "# 订单配送扩展\n")
    git(repo, "tag", "release/v0.2.0", commit(repo))
    _, report = checked(repo)
    text = (repo / "ext/domain-extend/check.md").read_text(encoding="utf-8")
    assert report["locked"] is False and "- [ ] 底座没有锁定（发布之后锁定自动解除了" in text


def test_check_says_unlocked_when_the_scope_never_reached_the_mainline(repo):
    lock(repo, BIND)
    ok(repo, "bind")  # 没合入主线
    _, report = checked(repo)
    assert report["locked"] is False and "还没锁定" in report["lock_reason"]


def test_check_output_is_capped(tmp_path):
    root = make_repo(tmp_path / "big", extra=30)
    bound(root)
    for index in range(30):
        write(root, f"ontology/object/合成对象{index:02d}.md", "改过\n")
    out, report = checked(root)
    assert len(report["violations"]) == 30
    assert len([line for line in out.splitlines() if line.startswith("- [ ]")]) == 20
    assert "还有 10 条没列出" in out
    text = (root / "ext/domain-extend/check.md").read_text(encoding="utf-8")
    assert text.count("- [ ] 底座") == 30
