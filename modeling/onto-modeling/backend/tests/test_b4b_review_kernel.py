"""B4b 独立审查修复（副本、确认生效与撤回、写入路径、守卫、发布）：编号与审查清单一致，每条一个修复前会失败的用例。

复现思路取自审查的脚本（R 编号），改成断言修复后的行为。测试数据全部为合成内容；假引擎不起真实 CLI。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from app import config
from app.api import deps
from app.kernel.changesets import service, store, worktree
from app.kernel.writepath import tx as writepath_tx
from app.kernel_guards import _guard_common as guards
from app.services.workspace import WorkspaceError, WorkspaceManager
from tests import proc
from tests.test_changesets import _pending, agent_write, git, main_write, obj, project  # noqa: F401


def files_under(root: Path, rel: str) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in (root / rel).rglob("*") if path.is_file())


def guard_says(guard: str, cwd: Path, payload: dict) -> str | None:
    os.environ["CLAUDE_PROJECT_DIR"] = str(cwd)
    try:
        return guards.check(guard, {"cwd": str(cwd), **payload})
    finally:
        os.environ.pop("CLAUDE_PROJECT_DIR", None)


# ---- 1 确认生效会删掉主线的全部 resource/ ----


def test_01_sparse_checkout_cannot_turn_into_deleting_mainline_resource(project):
    workspaces, project_id, root = project
    resources = files_under(root, "resource")
    assert "resource/others/资料.md" in resources
    copy = service.copy_for_turn(workspaces, project_id, root)
    # 守卫拦 sparse-checkout 与改 skip-worktree 的写法
    for command in ("git sparse-checkout disable", "git sparse-checkout set /*", "git -C . sparse-checkout reapply",
                    "git update-index --no-skip-worktree resource/others/资料.md"):
        reason = guard_says("toolset", copy, {"tool_name": "Bash", "tool_input": {"command": command}})
        assert reason, command
    # 守卫被绕过（或助手在终端里跑）：关掉稀疏、把「资料不见了」提交进副本分支
    proc.run(["git", "sparse-checkout", "disable"], cwd=copy, capture_output=True)
    agent_write(copy, "ontology/object/门店.md", obj("门店", "助手加的门店"))
    # 下一轮开始：重设稀疏规则、核对资料条目仍是不检出
    service.copy_for_turn(workspaces, project_id, root)
    listed = git(copy, "-c", "core.quotePath=false", "ls-files", "-t", "--", "resource")
    assert listed and all(line.startswith("S ") for line in listed.splitlines()), listed
    record = service.observe(workspaces, project_id, running=False)
    assert record["paths"] == ["ontology/object/门店.md"]
    # 就算副本分支上真有删资料的提交，合入也不碰受保护目录
    proc.run(["git", "-c", "core.hooksPath=/dev/null", "sparse-checkout", "disable"], cwd=copy,
             capture_output=True)
    git(copy, "add", "-A")
    git(copy, "commit", "-qm", "agent: 门店（顺手删了资料）")
    record = service.observe(workspaces, project_id, running=False)
    assert all(not rel.startswith("resource/") for rel in record["paths"])
    out = service.merge(workspaces, project_id, record["id"], actor="employee_0001")
    assert files_under(root, "resource") == resources
    changed = git(root, "-c", "core.quotePath=false", "show", "--name-only", "--format=", out["commit"]).split()
    assert "ontology/object/门店.md" in changed and not any(rel.startswith("resource/") for rel in changed)


# ---- 2 副本只是「不健康」就被整个删掉重建 ----


def test_02_unhealthy_copy_is_repaired_without_losing_work(project):
    workspaces, project_id, root = project
    copy = service.copy_for_turn(workspaces, project_id, root)
    agent_write(copy, "ontology/object/门店.md", obj("门店", "副本分支上的门店"), commit="agent: 门店")
    git(copy, "checkout", "-q", "-b", "scratch")
    agent_write(copy, "ontology/object/仓库.md", obj("仓库", "scratch 上提交的仓库"), commit="agent: 仓库")
    agent_write(copy, "ontology/object/客户.md", obj("客户", "还没提交的客户"))
    assert not worktree.healthy(root)
    service.copy_for_turn(workspaces, project_id, root)
    assert worktree.healthy(root)
    for name in ("门店", "仓库", "客户"):
        assert (copy / f"ontology/object/{name}.md").is_file(), name
    # 分离 HEAD 上的提交也接回副本分支
    git(copy, "checkout", "-q", "--detach")
    agent_write(copy, "ontology/object/商品.md", obj("商品", "分离 HEAD 上的提交"), commit="agent: 商品")
    service.copy_for_turn(workspaces, project_id, root)
    assert worktree.healthy(root) and (copy / "ontology/object/商品.md").is_file()
    # 目录真坏了（没有 .git）：挪到旁边留着，不直接删
    shutil.rmtree(copy / "ontology" / "object")
    (copy / ".git").unlink()
    (copy / "留着的草稿.md").write_text("坏副本里的东西", encoding="utf-8")
    service.copy_for_turn(workspaces, project_id, root)
    kept = [path for path in copy.parent.iterdir() if path.name.startswith("assistant.broken-")]
    assert kept and (kept[0] / "留着的草稿.md").is_file()
    assert worktree.healthy(root) and (copy / "ontology/object/商品.md").is_file()


# ---- 3 确认生效扫进主线里无关的未提交文件 ----


def test_03_empty_merge_does_not_sweep_unrelated_files(project):
    workspaces, project_id, root = project
    copy = service.copy_for_turn(workspaces, project_id, root)
    agent_write(copy, "ontology/object/订单.md", obj("订单", "助手改的描述"), commit="agent: 订单")
    record = service.observe(workspaces, project_id, running=False)
    main_write(root, "ontology/object/订单.md", obj("订单", "用户改的描述"))
    with pytest.raises(WorkspaceError):
        service.merge(workspaces, project_id, record["id"], actor="employee_0001")
    (root / "ontology/object/草稿.md").write_text(obj("草稿", "没确认的草稿"), encoding="utf-8")
    head = git(root, "rev-parse", "HEAD").strip()
    out = service.resolve(workspaces, project_id, record["id"], [{"target": "object:订单", "keep": "mainline"}],
                          actor="employee_0001")
    assert git(root, "rev-parse", "HEAD").strip() == head and out["commit"] is None
    assert "草稿.md" in git(root, "-c", "core.quotePath=false", "status", "--porcelain", "--untracked-files=all")
    # 写入路径的底层：空路径不是「全仓」
    assert writepath_tx.commit_paths(root, [], "web: 空", {"Onto-Source": "web"}) == ("", head, [])


# ---- 4 带冲突标记的文件被写进主线 ----


@pytest.mark.parametrize("change", ["blank_line", "title"])
def test_04_textual_conflicts_never_reach_mainline(project, change):
    workspaces, project_id, root = project
    base = obj("订单", "合成订单")
    if change == "blank_line":
        main_text = base.replace("## 对象描述\n\n合成订单", "## 对象描述\n合成订单")
        copy_text = base.replace("合成订单", "助手改的描述")
    else:
        main_text = base.replace("# 订单\n", "# 订单（主线）\n", 1)
        copy_text = base.replace("# 订单\n", "# 订单（助手）\n", 1)
    copy = service.copy_for_turn(workspaces, project_id, root)
    agent_write(copy, "ontology/object/订单.md", copy_text, commit="agent: 订单")
    record = service.observe(workspaces, project_id, running=False)
    main_write(root, "ontology/object/订单.md", main_text)
    with pytest.raises(WorkspaceError) as exc:
        service.merge(workspaces, project_id, record["id"], actor="employee_0001")
    assert exc.value.code == "merge_conflict"
    assert "<<<<<<<" not in (root / "ontology/object/订单.md").read_text(encoding="utf-8")


# ---- 5 确认生效和撤回并发 ----


def _slow_gate(monkeypatch, seconds=0.6):
    from app.kernel.changesets import gates

    original = gates.compare

    def slow(*args, **kwargs):
        time.sleep(seconds)
        return original(*args, **kwargs)

    monkeypatch.setattr(gates, "compare", slow)


def _race(*actions):
    import threading

    out, errors = [], []

    def run(action):
        try:
            out.append(action())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(action,)) for action in actions]
    for thread in threads:
        thread.start()
        time.sleep(0.15)
    for thread in threads:
        thread.join()
    return out, errors


def test_05_concurrent_merges_and_discards_are_serialized(project, monkeypatch):
    workspaces, project_id, root = project
    _copy, record = _pending(workspaces, project_id, root)
    (root / "tmp-note.md").write_text("主线工作区里没提交的东西\n")
    _slow_gate(monkeypatch)

    def merge():
        return service.merge(workspaces, project_id, record["id"], actor="employee_0001")

    out, errors = _race(merge, merge)
    assert len(out) == 1 and len(errors) == 1 and errors[0].code == "changeset_closed"
    assert "tmp-note.md" not in git(root, "-c", "core.quotePath=false", "log", "--name-only", "--format=", "-3")
    # 确认生效跑门禁时点撤回：撤回看到的是已合入，不能报「主线没有变化」
    _copy, second = _pending(workspaces, project_id, root, rel="ontology/object/仓库.md", text=obj("仓库", "合成仓库"))
    out, errors = _race(lambda: service.merge(workspaces, project_id, second["id"], actor="employee_0001"),
                        lambda: service.discard(workspaces, project_id, second["id"], actor="employee_0001"))
    assert [item["state"] for item in out] == ["merged"] and errors[0].code == "changeset_closed"
    assert store.get(root, second["id"])["state"] == "merged"


def test_05_turn_start_waits_for_a_running_merge(project, monkeypatch):
    """合入全程（含重置副本、记 merged）和一轮开始互斥：一轮开始不会夹在合入提交与重置副本之间。"""
    workspaces, project_id, root = project
    _copy, record = _pending(workspaces, project_id, root)
    real_collect = worktree.collect_state

    def slow_collect(*args, **kwargs):
        time.sleep(0.6)
        return real_collect(*args, **kwargs)

    monkeypatch.setattr(worktree, "collect_state", slow_collect)
    finished = {}

    def merge():
        service.merge(workspaces, project_id, record["id"], actor="employee_0001")
        finished["merge"] = time.monotonic()

    def turn():
        time.sleep(0.2)
        service.copy_for_turn(workspaces, project_id, root)
        finished["turn"] = time.monotonic()

    out, errors = _race(merge, turn)
    assert not errors and finished["turn"] >= finished["merge"]


# ---- 6 合入写到一半崩溃 ----


CRASH_CHILD = r'''
import os, sys
from pathlib import Path
sys.path.insert(0, os.getcwd())
from app import config
tmp = Path(sys.argv[1]); config.DATA_DIR = tmp / "data"
from app.api import deps
from app.kernel.changesets import service
from app.services.workspace import WorkspaceManager
from tests.test_changesets import agent_write, main_write, obj
ws = WorkspaceManager(tmp / "data", Path(config.ASSETS_DIR)); deps.workspaces = ws
pid = ws.create_project("变更集", owner="employee_0001")["id"]; root = ws.project_root(pid)
main_write(root, "ontology/object/订单.md", obj("订单", "合成订单"))
copy = service.copy_for_turn(ws, pid, root)
for name in ("门店", "仓库", "客户"):
    agent_write(copy, f"ontology/object/{name}.md", obj(name, f"助手加的{name}"))
rec = service.observe(ws, pid, running=False)
print(pid, rec["id"], flush=True)
mode = sys.argv[2]
if mode == "write":
    real = ws._atomic_write
    count = {"n": 0}
    def crashing(path, payload):
        count["n"] += 1
        if count["n"] == 2:
            os._exit(9)
        return real(path, payload)
    ws._atomic_write = crashing
else:
    from app.kernel.changesets import store
    real_update = store.update
    def crashing_update(root_, cs, **fields):
        if fields.get("state") == "merged":
            os._exit(9)
        return real_update(root_, cs, **fields)
    store.update = crashing_update
service.merge(ws, pid, rec["id"], actor="employee_0001")
'''


def _crash(tmp_path: Path, mode: str):
    crashed = proc.run([sys.executable, "-c", CRASH_CHILD, str(tmp_path), mode], capture_output=True, text=True,
                       cwd=Path(__file__).resolve().parents[1])
    assert crashed.returncode == 9, crashed.stderr[-800:]
    project_id, changeset_id = crashed.stdout.split()[-2:]
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    return workspaces, project_id, changeset_id


def test_06_crash_while_writing_a_merge_leaves_mainline_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    workspaces, project_id, changeset_id = _crash(tmp_path, "write")
    monkeypatch.setattr(deps, "workspaces", workspaces)
    root = workspaces.project_root(project_id)
    head = git(root, "rev-parse", "HEAD").strip()
    writepath_tx.recover_intents(workspaces)
    assert git(root, "rev-parse", "HEAD").strip() == head  # 没有补提交半个合入
    assert git(root, "status", "--porcelain", "--", "ontology") == ""  # 写了一半的文件恢复原样
    assert store.get(root, changeset_id)["state"] == "pending_confirm"
    # F3：启动时遗留的这一轮自动合入（不再等人确认生效）
    service.startup_repair(workspaces)
    assert store.get(root, changeset_id)["state"] == "merged"
    assert all((root / f"ontology/object/{name}.md").is_file() for name in ("仓库", "客户", "门店"))


def test_06_crash_after_the_merge_commit_is_reconciled_at_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    workspaces, project_id, changeset_id = _crash(tmp_path, "record")
    monkeypatch.setattr(deps, "workspaces", workspaces)
    root = workspaces.project_root(project_id)
    assert "Onto-Changeset: " + changeset_id in git(root, "log", "-1", "--format=%B")
    writepath_tx.recover_intents(workspaces)
    service.startup_repair(workspaces)
    record = store.get(root, changeset_id)
    assert record["state"] == "merged" and record["merged"]["commit"] == git(root, "rev-parse", "HEAD").strip()
    with pytest.raises(WorkspaceError) as exc:
        service.discard(workspaces, project_id, changeset_id, actor="employee_0001")
    assert exc.value.code == "changeset_closed"


# ---- 10 合入没产生提交时 merged.commit 记成别人的提交 ----


def test_10_noop_merge_records_no_commit_and_cannot_be_reverted(project):
    workspaces, project_id, root = project
    text = obj("订单", "两边改成一样")
    _copy, record = _pending(workspaces, project_id, root, rel="ontology/object/订单.md", text=text)
    head = main_write(root, "ontology/object/订单.md", text)
    out = service.merge(workspaces, project_id, record["id"], actor="employee_0001")
    assert out["commit"] is None and git(root, "rev-parse", "HEAD").strip() == head
    assert store.get(root, record["id"])["merged"]["commit"] is None
    with pytest.raises(WorkspaceError) as exc:
        service.revert_merged(workspaces, project_id, record["id"], actor="employee_0001")
    assert exc.value.code == "changeset_closed"
    assert "两边改成一样" in (root / "ontology/object/订单.md").read_text(encoding="utf-8")


# ---- 11 只产出验收回执的一轮 ----


def test_11_receipts_from_a_receipt_only_round_reach_mainline_and_survive(project):
    workspaces, project_id, root = project
    rel = ".ontology-state/script-verifications.json"
    (root / ".ontology-state").mkdir(exist_ok=True)
    (root / rel).write_text(json.dumps({"a.py": "old"}))
    copy = service.copy_for_turn(workspaces, project_id, root)
    time.sleep(0.02)
    (copy / rel).write_text(json.dumps({"a.py": "new", "b.py": "b"}))
    assert service.observe(workspaces, project_id, running=False) is None
    assert json.loads((root / rel).read_text()) == {"a.py": "new", "b.py": "b"}
    main_write(root, "ontology/object/客户.md", obj("客户", "无关"))
    service.copy_for_turn(workspaces, project_id, root)
    assert json.loads((copy / rel).read_text()) == {"a.py": "new", "b.py": "b"}


# ---- 13 发布时取项目元模型的根目录取错 ----


def test_13_publish_uses_the_project_root_for_the_metamodel(project, monkeypatch):
    workspaces, project_id, root = project
    from app.kernel import metamodel
    from app.services.publish import ontology_package
    from app.services.publish import service as publish_service

    seen = []
    real = metamodel.for_project
    monkeypatch.setattr(metamodel, "for_project", lambda path: seen.append(Path(path)) or real(path))
    monkeypatch.setattr(ontology_package.OntologyPackageAdapter, "_render", lambda self, src, out: None)
    publish_service.export_package_zip(workspaces, project_id, root.parent.parent / "out.zip")
    assert seen and all(path == root for path in seen)


# ---- 16 资料区守卫在副本里能被绕过 ----


def test_16_resource_guard_follows_the_link_into_mainline(project):
    workspaces, project_id, root = project
    copy = service.copy_for_turn(workspaces, project_id, root)
    link = os.readlink(copy / "resource")
    for payload in ({"tool_name": "Write", "tool_input": {"file_path": f"{link}/others/资料.md"}},
                    {"tool_name": "Write", "tool_input": {"file_path": str(root / "resource/others/资料.md")}},
                    {"tool_name": "Bash", "tool_input": {"command": f"echo x > {root}/resource/others/资料.md"}}):
        assert guard_says("resource", copy, payload), payload


# ---- 17 撤回已合入的变更集被派生的 index.md 误判冲突 ----


def test_17_revert_ignores_derived_index_and_catches_any_later_edit(project):
    workspaces, project_id, root = project
    _copy, record = _pending(workspaces, project_id, root)
    service.merge(workspaces, project_id, record["id"], actor="employee_0001")
    workspaces.write_file(project_id, "ontology/object/客户.md", obj("客户", "无关的客户"), author="employee_0001")
    out = service.revert_merged(workspaces, project_id, record["id"], actor="employee_0001")
    assert out["commit"] and not (root / "ontology/object/门店.md").exists()
    # 合入之后同一条目只改了一级标题：也算后来改过，不静默覆盖
    _copy, second = _pending(workspaces, project_id, root, rel="ontology/object/仓库.md", text=obj("仓库", "合成仓库"))
    service.merge(workspaces, project_id, second["id"], actor="employee_0001")
    text = (root / "ontology/object/仓库.md").read_text(encoding="utf-8")
    workspaces.write_file(project_id, "ontology/object/仓库.md", text.replace("# 仓库\n", "# 仓库（改名）\n", 1),
                          author="employee_0001")
    with pytest.raises(WorkspaceError) as exc:
        service.revert_merged(workspaces, project_id, second["id"], actor="employee_0001")
    assert exc.value.code == "merge_conflict"


# ---- 20 open_changeset 先占的段被第一次观察关掉 ----


def test_20_reserved_changeset_survives_an_empty_observe(project):
    workspaces, project_id, root = project
    copy = service.copy_for_turn(workspaces, project_id, root)
    slot = service.open_changeset(workspaces, project_id, source="autobuild", build_mode="L4", run_id="run-1",
                                  slice_id="s-1", purpose="第一段")
    service.observe(workspaces, project_id, running=True)
    assert store.get(root, slot["id"])["state"] == "open"
    agent_write(copy, "ontology/object/门店.md", obj("门店", "段里写的门店"))
    record = service.observe(workspaces, project_id, running=True)
    assert record["id"] == slot["id"] and record["run_id"] == "run-1" and record["slice_id"] == "s-1"
