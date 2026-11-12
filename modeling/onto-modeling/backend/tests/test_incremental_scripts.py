"""增量构建包的脚本（ADR-0085）：传播链、范围文件、传播核对。

在合成的 git 仓库里直接跑包里的脚本，和装配后助手跑的是同一份。条目全是合成的订单、配送单。
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app import config
from tests import proc
from tests.proc import git

ASSETS = Path(config.ASSETS_DIR)
TOOLKIT = ASSETS / "workspace-toolkit" / "scripts"
SCRIPTS = ASSETS / "packs" / "incremental" / "scripts"
SCOPE, APPLY, CHECK = "incremental-scope", "incremental-apply", "incremental-check"
SCOPE_FILE = "incremental/scope.json"

ORDER = "ontology/object/订单.md"
DELIVERY = "ontology/object/配送单.md"
WAREHOUSE = "ontology/object/仓库.md"
PLACE = "ontology/operation/下单.md"
STOCK = "ontology/operation/校验库存.md"
DISPATCH = "ontology/operation/派送.md"
LIMIT = "ontology/rule/下单金额上限.md"
APPROVAL = "ontology/rule/超额审批.md"
DAILY = "ontology/metric/日订单量.md"
SMS = "ontology/action/发送通知.md"
CASE = "ontology/testcase/下单.md"
MOCK = "ontology/data-mock/订单.md"
DISPATCH_MOCK = "ontology/data-mock/派送记录.md"
SCRIPT = "ontology/scripts/下单.py"


def _object(name: str, rows: str = "") -> str:
    return (f"# {name}\n\n## 对象描述\n\n合成{name}\n\n## 对象属性清单\n\n"
            "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            f"| 编号 | code | 主键编号 | 是 | 字符串 | 无 | 无 |\n{rows}")


def _operation(body: str, actions: str = "") -> str:
    return f"## 逻辑描述\n\n{body}\n{actions}"


ENTRIES = {
    ORDER: _object("订单"),
    DELIVERY: _object("配送单", "| 关联订单 | order_code | [[订单]] | 否 | 对象引用 | 无 | 无 |\n"),
    WAREHOUSE: _object("仓库"),
    PLACE: _operation("读取[[订单]]，调用[[校验库存]]逻辑文档。"),
    STOCK: _operation("核对[[仓库]]的库存。"),
    DISPATCH: _operation("按[[配送单]]派送。", "\n## 与动作(action)的关联\n\n| 动作名称 |\n| --- |\n| [[发送通知]] |\n"),
    LIMIT: "# 下单金额上限\n\n限制[[下单]]的金额。\n",
    APPROVAL: "# 超额审批\n\n超过[[下单金额上限]]要审批。\n",
    DAILY: "# 日订单量\n\n统计[[订单]]。\n",
    SMS: "# 发送通知\n\n合成动作\n",
    CASE: "# 下单\n\n合成场景\n",
    MOCK: "# 订单\n\n| 编号 |\n| --- |\n| D001 |\n",
    DISPATCH_MOCK: "# 派送记录\n\n| 编号 | 来源 |\n| --- | --- |\n| P001 | 配送单 |\n",
    SCRIPT: "print('合成')\n",
    # 不进包的沉淀区：链接到订单也不算传播链，也不算基线条目
    "ontology/knowledge/备注.md": "# 备注\n\n提到[[订单]]\n",
}


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit(root: Path, message: str = "合成提交") -> str:
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD").strip()


def make_repo(root: Path, entries: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "tester@example.com")
    git(root, "config", "user.name", "tester")
    write(root, ".gitignore", ".ontology-state/\n")
    for rel, text in entries.items():
        write(root, rel, text)
    commit(root)
    return root


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "workspace", ENTRIES)


def lock(root: Path, command: str) -> None:
    write(root, f".ontology-state/lock-{command}.json", "{}")


def run(root: Path, script: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(TOOLKIT)}
    return proc.run([sys.executable, str(SCRIPTS / script), *args], cwd=root, capture_output=True, text=True,
                    env=env, timeout=60)


def scope_tool(root: Path, *args: str) -> subprocess.CompletedProcess:
    return run(root, "incremental_scope.py", *args)


def check_tool(root: Path, *args: str) -> subprocess.CompletedProcess:
    return run(root, "incremental_check.py", *args)


def start_round(root: Path, *seeds: tuple[str, str], goal: str = "合成目标", depth: str | None = None) -> dict:
    """起草并确认一轮范围；seeds 是（动作, 条目）。"""
    lock(root, SCOPE)
    flags = [item for action, path in seeds for item in ("--seed", action, path, "合成理由")]
    extra = ["--depth", depth] if depth else []
    drafted = scope_tool(root, "draft", "--goal", goal, *flags, *extra)
    assert drafted.returncode == 0, drafted.stdout + drafted.stderr
    confirmed = scope_tool(root, "confirm")
    assert confirmed.returncode == 0, confirmed.stdout + confirmed.stderr
    return read_json(root, SCOPE_FILE)


def read_json(root: Path, rel: str) -> dict:
    return json.loads((root / rel).read_text(encoding="utf-8"))


def read_text(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


@pytest.fixture()
def chain(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.syspath_prepend(str(TOOLKIT))
    return importlib.import_module("incremental_chain")


# ---- 传播链 ----


def test_chain_goes_backwards_along_references_and_explains_each_item(repo, chain):
    items = {item["path"]: item for item in chain.Graph(repo).walk([ORDER])}
    assert {path: item["depth"] for path, item in items.items()} == {
        DELIVERY: 1, PLACE: 1, DAILY: 1, MOCK: 1, DISPATCH: 2, LIMIT: 2, CASE: 2, SCRIPT: 2, DISPATCH_MOCK: 2}
    limit = items[LIMIT]
    assert limit["trail"] == "订单 → 下单（引用对象）→ 下单金额上限（引用操作）"
    assert (limit["via"], limit["relation"], limit["include"]) == (PLACE, "引用操作", True)
    # 沉淀区、和订单没有引用关系的条目都不在链上
    assert WAREHOUSE not in items and "ontology/knowledge/备注.md" not in items


def test_every_relation_kind_has_its_own_name(repo, chain):
    graph = chain.Graph(repo)
    assert graph.referrers[ORDER][DELIVERY] == "引用对象（字段：关联订单）"
    assert graph.referrers[ORDER][PLACE] == "引用对象"
    assert graph.referrers[ORDER][DAILY] == "引用对象"
    assert graph.referrers[ORDER][MOCK] == "测试数据"
    assert graph.referrers[STOCK][PLACE] == "调用子操作"
    assert graph.referrers[SMS][DISPATCH] == "调用动作"
    assert graph.referrers[PLACE][LIMIT] == "引用操作"
    assert graph.referrers[PLACE][CASE] == "场景验证"
    assert graph.referrers[PLACE][SCRIPT] == "脚本实现"
    assert graph.referrers[DELIVERY][DISPATCH_MOCK] == "测试数据"


def test_depth_defaults_to_two_and_has_a_cap(tmp_path, repo, chain):
    graph = chain.Graph(repo)
    assert APPROVAL not in {item["path"] for item in graph.walk([ORDER])}
    assert APPROVAL in {item["path"] for item in graph.walk([ORDER], 3)}
    assert [chain.clamp_depth(value) for value in (None, 0, -3, 1, 4, 99)] == [2, 1, 1, 1, 4, 5]
    links = {f"ontology/operation/步骤{n}.md": _operation(f"调用[[步骤{n + 1}]]逻辑文档。") for n in range(1, 9)}
    links["ontology/operation/步骤9.md"] = _operation("合成")
    long = chain.Graph(make_repo(tmp_path / "long", links))
    deepest = max(item["depth"] for item in long.walk(["ontology/operation/步骤9.md"], 99))
    assert deepest == chain.MAX_DEPTH


def test_cycles_stop_and_each_entry_appears_once(tmp_path, chain):
    root = make_repo(tmp_path / "cycle", {
        "ontology/operation/甲.md": _operation("调用[[乙]]逻辑文档。"),
        "ontology/operation/乙.md": _operation("调用[[丙]]逻辑文档。"),
        "ontology/operation/丙.md": _operation("调用[[甲]]逻辑文档。"),
    })
    seed = "ontology/operation/甲.md"
    items = chain.Graph(root).walk([seed], 5)
    assert sorted(item["path"] for item in items) == ["ontology/operation/丙.md", "ontology/operation/乙.md"]
    assert seed not in {item["path"] for item in items}


def test_deleted_or_new_seed_still_has_referrers(repo, chain):
    (repo / ORDER).unlink()
    without = chain.Graph(repo).walk([ORDER])
    assert without == []  # 文件没了，不告诉 Graph 就连不上
    items = {item["path"] for item in chain.Graph(repo, stubs=[ORDER]).walk([ORDER])}
    assert {DELIVERY, PLACE, DAILY} <= items


REFERENCE_KEYS = ("operation_refs", "action_refs", "object_refs", "called_by", "invokes_action", "mock_refs")


def test_direct_referrers_cover_analyze_impact(repo, chain, monkeypatch):
    """关系的口径和 core 的影响分析一致；传播链多出规则、指标和同名测试数据。"""
    impact = importlib.import_module("impact_analysis")
    checker = importlib.import_module("ontology_checker")
    ontology = repo / "ontology"
    docs = checker.load_ontology(ontology)
    graph = chain.Graph(repo)
    for stem, doc in docs.items():
        result = impact.analyze_impact(stem, doc.doc_type, docs, ontology / "data-mock", ontology / "testcase")
        expected = {name.split(" (字段")[0] for key in REFERENCE_KEYS for name in result[key]}
        direct = {Path(path).stem for path in graph.referrers.get(docs[stem].path.relative_to(repo).as_posix(), {})}
        assert expected <= direct, stem


def test_chain_command_prints_a_capped_list(tmp_path):
    entries = {ORDER: _object("订单"), **{f"ontology/operation/操作{n:02}.md": _operation("读取[[订单]]。") for n in range(30)}}
    root = make_repo(tmp_path / "many", entries)
    done = run(root, "incremental_chain.py", ORDER)
    assert done.returncode == 0, done.stderr
    assert done.stdout.count("- 第 1 层") == 20 and "还有 10 条没列出，完整清单见 --json 的输出" in done.stdout
    assert len(json.loads(run(root, "incremental_chain.py", ORDER, "--json").stdout)) == 30


# ---- 范围文件 ----


def test_scope_commands_need_their_lock(repo):
    refused = scope_tool(repo, "draft", "--goal", "合成", "--seed", "modify", ORDER, "理由")
    assert refused.returncode == 2 and "/incremental-scope" in refused.stdout
    assert not (repo / "incremental").exists()
    assert scope_tool(repo, "applied").returncode == 2


def test_draft_writes_only_the_draft_until_confirmed(repo):
    head = git(repo, "rev-parse", "HEAD").strip()
    lock(repo, SCOPE)
    drafted = scope_tool(repo, "draft", "--goal", "配送单加时效", "--seed", "modify", ORDER, "加字段")
    assert drafted.returncode == 0, drafted.stdout
    # 草稿不算范围：等回复这一轮也可能合入主线，没确认的不能生效
    assert not (repo / SCOPE_FILE).exists()
    assert (repo / "incremental/scope.draft.md").is_file()
    assert "下单金额上限" in read_text(repo, "incremental/scope.draft.md")

    confirmed = scope_tool(repo, "confirm", "--exclude", DAILY)
    assert confirmed.returncode == 0, confirmed.stdout
    assert not (repo / "incremental/scope.draft.json").exists() and not (repo / "incremental/scope.draft.md").exists()
    scope = read_json(repo, SCOPE_FILE)
    assert scope["round"] == 1 and scope["goal"] == "配送单加时效" and scope["status"] == "active"
    assert scope["baseline"] == {"commit": head, "entries": 14} and scope["max_depth"] == 2
    assert scope["seeds"] == [{"action": "modify", "path": ORDER, "reason": "加字段"}]
    first = next(item for item in scope["chain"] if item["path"] == LIMIT)
    assert {"path", "depth", "via", "relation", "include"} <= set(first)
    assert next(item for item in scope["chain"] if item["path"] == DAILY)["include"] is False
    assert DAILY not in scope["editable"] and {ORDER, PLACE, LIMIT, SCRIPT} <= set(scope["editable"])
    # 脚本和同名描述符一起改
    assert "ontology/scripts/下单.md" in scope["editable"]
    assert "第 1 轮，进行中" in read_text(repo, "incremental/scope.md")


@pytest.mark.parametrize("seed, message", [
    (("rename", ORDER, "理由"), "动作只能是"),
    (("modify", "ontology/object/不存在.md", "理由"), "不存在"),
    (("delete", "ontology/object/不存在.md", "理由"), "不存在"),
    (("add", ORDER, "理由"), "已经有了"),
    (("modify", ORDER, " "), "要写理由"),
    (("modify", "reports/报告.md", "理由"), "不是本体条目"),
    (("modify", "ontology/knowledge/备注.md", "理由"), "不是本体条目"),
])
def test_bad_seeds_are_refused(repo, seed, message):
    lock(repo, SCOPE)
    done = scope_tool(repo, "draft", "--goal", "合成", "--seed", *seed)
    assert done.returncode == 2 and message in done.stdout
    assert not (repo / "incremental/scope.draft.json").exists()


def test_a_new_round_archives_the_previous_one(repo):
    first = start_round(repo, ("modify", STOCK))
    write(repo, "incremental/applied.json", "{}\n")
    write(repo, "incremental/check.md", "# 合成\n")
    write(repo, "incremental/check.json", "{}\n")
    lock(repo, SCOPE)
    assert scope_tool(repo, "close").returncode == 0
    closed = read_json(repo, SCOPE_FILE)
    assert closed["status"] == "closed" and closed["closed_at"]
    assert "已结束" in read_text(repo, "incremental/scope.md")
    assert "已经结束了" in scope_tool(repo, "close").stdout

    head = commit(repo)
    second = start_round(repo, ("modify", WAREHOUSE))
    assert second["round"] == 2 and second["status"] == "active"
    assert second["baseline"]["commit"] == head != first["baseline"]["commit"]
    assert STOCK not in {seed["path"] for seed in second["seeds"]}
    for name in ("scope.json", "scope.md", "applied.json", "check.md", "check.json"):
        assert (repo / "incremental/history/1" / name).is_file(), name
    assert read_json(repo, "incremental/history/1/scope.json")["status"] == "closed"
    # 上一轮的修改记录和核对报告不留到这一轮
    assert not (repo / "incremental/applied.json").exists() and not (repo / "incremental/check.md").exists()


def test_rescoping_an_active_round_enlarges_it(repo):
    first = start_round(repo, ("modify", STOCK))
    write(repo, "incremental/applied.json", "{}\n")
    commit(repo)
    second = start_round(repo, ("modify", DISPATCH))
    assert second["round"] == 2 and second["baseline"] == first["baseline"]
    assert {seed["path"] for seed in second["seeds"]} == {STOCK, DISPATCH}
    assert set(first["editable"]) | {DISPATCH} <= set(second["editable"])
    assert (repo / "incremental/history/1/applied.json").is_file() and not (repo / "incremental/applied.json").exists()


def test_applied_verifies_every_seed(repo):
    start_round(repo, ("modify", STOCK), ("add", "ontology/rule/超时配送.md"), ("delete", DAILY))
    lock(repo, APPLY)
    missing = scope_tool(repo, "applied")
    assert missing.returncode == 1 and "还有 3 个种子没处理" in missing.stdout
    assert not (repo / "incremental/applied.json").exists()

    write(repo, STOCK, ENTRIES[STOCK] + "\n补充说明\n")
    write(repo, "ontology/rule/超时配送.md", "# 超时配送\n\n合成规则\n")
    missing = scope_tool(repo, "applied")
    assert missing.returncode == 1 and "还有 1 个种子没处理" in missing.stdout and DAILY in missing.stdout

    refused = scope_tool(repo, "applied", "--skip", ORDER, "不是种子")
    assert refused.returncode == 2 and "不是本轮的种子" in refused.stdout
    assert scope_tool(repo, "applied", "--skip", DAILY, " ").returncode == 2

    done = scope_tool(repo, "applied", "--skip", DAILY, "合成理由")
    assert done.returncode == 0, done.stdout
    applied = read_json(repo, "incremental/applied.json")
    assert applied["round"] == 1
    assert {item["path"]: item["status"] for item in applied["handled"]} == {
        STOCK: "modified", "ontology/rule/超时配送.md": "added"}
    assert applied["skipped"] == [{"path": DAILY, "action": "delete", "reason": "合成理由"}]

    # 删除的种子：文件没了也算处理了
    (repo / DAILY).unlink()
    assert scope_tool(repo, "applied").returncode == 0


def test_applied_needs_an_active_round(repo):
    start_round(repo, ("modify", STOCK))
    assert scope_tool(repo, "close").returncode == 0
    lock(repo, APPLY)
    done = scope_tool(repo, "applied")
    assert done.returncode == 2 and "/incremental-scope" in done.stdout


def test_scope_output_is_capped(tmp_path):
    entries = {ORDER: _object("订单"), **{f"ontology/operation/操作{n:02}.md": _operation("读取[[订单]]。") for n in range(70)}}
    root = make_repo(tmp_path / "many", entries)
    lock(root, SCOPE)
    drafted = scope_tool(root, "draft", "--goal", "合成", "--seed", "modify", ORDER, "理由")
    assert drafted.stdout.count("- 第 1 层") == 20
    assert "还有 50 条没列出，完整清单见 incremental/scope.draft.json" in drafted.stdout
    md = read_text(root, "incremental/scope.draft.md")
    assert md.count("- 第 1 层") == 50 and "还有 20 条没列出" in md and "传播链（共 70 条）" in md
    assert len(read_json(root, "incremental/scope.draft.json")["chain"]) == 70


# ---- 传播核对 ----


def test_check_needs_a_scope_and_its_lock(repo):
    assert check_tool(repo, "run").returncode == 2
    lock(repo, CHECK)
    done = check_tool(repo, "run")
    assert done.returncode == 2 and "/incremental-scope" in done.stdout


def test_check_reports_changes_outside_the_scope_and_pending_items(repo):
    start_round(repo, ("modify", STOCK))
    lock(repo, CHECK)
    write(repo, STOCK, ENTRIES[STOCK] + "\n补充说明\n")
    # 网页上手工改的：范围外的条目也会变，核对要报出来
    write(repo, WAREHOUSE, ENTRIES[WAREHOUSE] + "\n手工补充\n")
    write(repo, "ontology/rule/新规则.md", "# 新规则\n\n合成\n")
    done = check_tool(repo, "run")
    assert done.returncode == 0, done.stdout
    report = read_json(repo, "incremental/check.json")
    assert report["changes"]["modified"] == sorted([STOCK, WAREHOUSE])
    assert report["changes"]["added"] == ["ontology/rule/新规则.md"]
    # 新增的不算范围外
    assert report["outside"] == [{"path": WAREHOUSE, "status": "modified"}]
    pending = {item["path"]: item for item in report["pending"]}
    assert PLACE in pending and pending[PLACE]["trail"].startswith("校验库存 → 下单（调用子操作）")
    assert pending[PLACE]["in_scope"] is True
    md = read_text(repo, "incremental/check.md")
    assert f"- [ ] {PLACE}" in md and "范围外改动（共 1 个）" in md and f"修改 {WAREHOUSE}" in md
    assert f"- [ ] {WAREHOUSE}" not in md  # 自己改了的不算待确认
    assert "范围外改动 1 个" in done.stdout
    # 范围外改动也是待处理项，不能标记掉：只有并进范围或撤回才算处理
    assert f"- [ ] 修改 {WAREHOUSE}" in md
    assert check_tool(repo, "mark", WAREHOUSE, "--reason", "合成").returncode == 2
    assert check_tool(repo, "mark", *[item["path"] for item in report["pending"]], "--reason", "合成").returncode == 0
    assert "[ ]" in read_text(repo, "incremental/check.md")
    write(repo, WAREHOUSE, ENTRIES[WAREHOUSE])
    assert check_tool(repo, "run").returncode == 0
    assert f"修改 {WAREHOUSE}" not in read_text(repo, "incremental/check.md")


def test_marking_clears_pending_items_until_no_checkbox_is_left(repo):
    start_round(repo, ("modify", STOCK))
    lock(repo, CHECK)
    write(repo, STOCK, ENTRIES[STOCK] + "\n补充说明\n")
    assert check_tool(repo, "run").returncode == 0
    pending = [item["path"] for item in read_json(repo, "incremental/check.json")["pending"]]
    assert pending and "[ ]" in read_text(repo, "incremental/check.md")

    assert check_tool(repo, "mark", DAILY, "--reason", "合成").returncode == 2  # 不在待确认项里
    assert check_tool(repo, "mark", pending[0], "--reason", " ").returncode == 2  # 要写理由

    # 理由里写了 `[ ]` 也不能让报告一直算没处理完
    done = check_tool(repo, "mark", *pending, "--reason", "只是 [ ] 合成理由")
    assert done.returncode == 0, done.stdout
    report = read_json(repo, "incremental/check.json")
    assert report["pending"] == [] and {item["path"] for item in report["cleared"]} == set(pending)
    assert "[ ]" not in read_text(repo, "incremental/check.md")
    assert "没有待确认项了" in done.stdout
    # 重跑不丢标记
    assert check_tool(repo, "run").returncode == 0
    assert read_json(repo, "incremental/check.json")["pending"] == []


def test_marks_do_not_carry_into_the_next_round(repo):
    start_round(repo, ("modify", STOCK))
    lock(repo, CHECK)
    write(repo, STOCK, ENTRIES[STOCK] + "\n补充说明\n")
    assert check_tool(repo, "run").returncode == 0
    pending = [item["path"] for item in read_json(repo, "incremental/check.json")["pending"]]
    assert check_tool(repo, "mark", *pending, "--reason", "合成").returncode == 0
    lock(repo, SCOPE)
    assert scope_tool(repo, "close").returncode == 0
    start_round(repo, ("modify", STOCK))
    assert check_tool(repo, "run").returncode == 0
    assert {item["path"] for item in read_json(repo, "incremental/check.json")["pending"]} == set(pending)


def test_deleted_entry_keeps_its_referrers_pending(repo):
    start_round(repo, ("delete", DAILY), ("modify", WAREHOUSE))
    lock(repo, CHECK)
    (repo / WAREHOUSE).unlink()
    assert check_tool(repo, "run").returncode == 0
    report = read_json(repo, "incremental/check.json")
    assert report["changes"]["deleted"] == [WAREHOUSE]
    assert STOCK in {item["path"] for item in report["pending"]}


def test_large_change_ratio_warns(repo):
    start_round(repo, ("modify", WAREHOUSE))
    lock(repo, CHECK)
    write(repo, WAREHOUSE, ENTRIES[WAREHOUSE] + "\n补充\n")
    done = check_tool(repo, "run")
    report = read_json(repo, "incremental/check.json")
    assert report["over_threshold"] is False and "超过" not in done.stdout and report["threshold"] == 0.3
    # 14 个条目改 5 个：36%
    for rel in (ORDER, DELIVERY, PLACE, STOCK):
        write(repo, rel, ENTRIES[rel] + "\n补充\n")
    done = check_tool(repo, "run")
    report = read_json(repo, "incremental/check.json")
    assert report["over_threshold"] is True and report["ratio"] == round(5 / 14, 4)
    assert "超过 30%" in done.stdout and "超过 30%" in read_text(repo, "incremental/check.md")
    done = check_tool(repo, "run", "--threshold", "0.5")
    assert "超过" not in done.stdout


def test_check_output_is_capped(tmp_path):
    entries = {ORDER: _object("订单"), **{f"ontology/operation/操作{n:02}.md": _operation("读取[[订单]]。") for n in range(70)}}
    root = make_repo(tmp_path / "many", entries)
    start_round(root, ("modify", ORDER))
    lock(root, CHECK)
    write(root, ORDER, entries[ORDER] + "\n补充\n")
    done = check_tool(root, "run")
    assert len(re.findall(r"^- \[ \] ", done.stdout, re.M)) == 20
    assert "还有 50 条没列出，完整清单见 incremental/check.json" in done.stdout
    md = read_text(root, "incremental/check.md")
    assert len(re.findall(r"^- \[ \] ", md, re.M)) == 50 and "还有 20 条没列出" in md
    assert len(read_json(root, "incremental/check.json")["pending"]) == 70


# ---- 按上传触发（ADR-0091）----


def test_closing_is_refused_until_the_changes_are_checked(repo):
    start_round(repo, ("modify", STOCK))
    lock(repo, CHECK)
    write(repo, STOCK, ENTRIES[STOCK] + "\n补充说明\n")
    refused = scope_tool(repo, "close")
    assert refused.returncode == 2 and "/incremental-check" in refused.stdout
    assert check_tool(repo, "run").returncode == 0
    assert scope_tool(repo, "close").returncode == 2  # 还有待确认项
    pending = [item["path"] for item in read_json(repo, "incremental/check.json")["pending"]]
    assert check_tool(repo, "mark", *pending, "--reason", "合成").returncode == 0
    # 核对之后又改了：报告对不上，还是不能结束
    write(repo, STOCK, ENTRIES[STOCK] + "\n再补充\n")
    write(repo, "ontology/rule/新规则.md", "# 新规则\n\n合成\n")
    assert scope_tool(repo, "close").returncode == 2
    assert check_tool(repo, "run").returncode == 0
    assert scope_tool(repo, "close").returncode == 0
    assert read_json(repo, SCOPE_FILE)["status"] == "closed"


def test_confirming_records_what_the_round_covers(repo):
    head = git(repo, "rev-parse", "HEAD").strip()
    assert start_round(repo, ("modify", STOCK))["covers"] == head


def test_a_release_ends_the_round_so_the_next_confirm_starts_a_new_one(repo):
    first = start_round(repo, ("modify", STOCK))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "合成提交")
    released = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "tag", "release/0.1.0")
    assert "已经结束了" in scope_tool(repo, "close").stdout
    second = start_round(repo, ("modify", WAREHOUSE))
    assert second["round"] == first["round"] + 1
    # 新的一轮：基线换成发布之后的主线，上一轮的种子不并进来
    assert second["baseline"]["commit"] == released and [seed["path"] for seed in second["seeds"]] == [WAREHOUSE]


def test_materials_lists_what_triggered_the_round(repo):
    lock(repo, SCOPE)
    assert "没有记录到" in scope_tool(repo, "materials").stdout
    state = repo / ".onto" / "run" / "activation.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"incremental": {"commit": "abc", "files": ["resource/others/新资料.md"]}}), encoding="utf-8")
    assert "resource/others/新资料.md" in scope_tool(repo, "materials").stdout
