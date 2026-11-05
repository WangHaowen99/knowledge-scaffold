"""内核的范围限定 confine（ADR-0085）：范围生效时，基线里不在 editable 的条目对助手只读。

直接跑内核守卫的 shell 包装，流程用真实的组装结果（core 加增量构建包）。条目、范围都是合成的。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.kernel.changesets import service as changesets
from app.main import app
from app.services import auth
from app.services.flow import assemble
from app.services.packs import builtin
from app.services.packs import service as pack_service
from app.services.workspace import WorkspaceManager
from tests.test_guard_bypass import bash, denied, run_guard, write, ws  # noqa: F401 - 共用 fixture 与工具

ASSETS = Path(config.ASSETS_DIR)
SCOPE_FILE = "incremental/scope.json"
OUTSIDE = "ontology/object/仓库.md"
INSIDE = "ontology/object/订单.md"
INSIDE_OP = "ontology/operation/下单.md"
ENTRIES = (OUTSIDE, INSIDE, INSIDE_OP, "ontology/operation/派送.md")


def _flow(*packs: str) -> str:
    def load(rel: str) -> dict:
        return yaml.safe_load((ASSETS / rel).read_text(encoding="utf-8"))

    contributions = [("core", "0.4.0", load("workspace-toolkit/flow.yaml"))]
    contributions += [(name, "0.1.0", load(f"packs/{name}/flow.yaml")) for name in packs]
    return assemble.render(assemble.assemble(contributions))


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def commit(root: Path, message: str = "合成提交") -> str:
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD").strip()


def scope_doc(baseline: str, *, editable=(INSIDE, INSIDE_OP), status: str = "active") -> str:
    return json.dumps({"round": 1, "goal": "合成目标", "baseline": {"commit": baseline, "entries": len(ENTRIES)},
                       "seeds": [{"action": "modify", "path": INSIDE, "reason": "合成"}], "chain": [],
                       "editable": list(editable), "status": status, "max_depth": 2}, ensure_ascii=False)


def set_flow(root: Path, text: str) -> None:
    (root / ".claude" / "flow.yaml").write_text(text, encoding="utf-8")


def set_scope(root: Path, text: str) -> str:
    path = root / SCOPE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return commit(root, "合成范围")


def acquire(root: Path, command: str, pid: int | None = None) -> None:
    path = root / ".ontology-state" / f"lock-{command}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"phase": "start", "owner_pid": os.getpid() if pid is None else pid}), encoding="utf-8")


@pytest.fixture()
def scoped(ws):  # noqa: F811
    """范围已经合入主线的工作区：基线里有四个条目，订单和下单可改。"""
    git(ws, "config", "user.email", "tester@example.com")
    git(ws, "config", "user.name", "tester")
    (ws / ".gitignore").write_text(".claude\nCLAUDE.md\n.onto/run/\n.ontology-state/\ntmp/\n", encoding="utf-8")
    for rel in ENTRIES:
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {Path(rel).stem}\n", encoding="utf-8")
    baseline = commit(ws, "合成基线")
    set_flow(ws, _flow("incremental"))
    set_scope(ws, scope_doc(baseline))
    return ws


def guard(root: Path, payload: dict, *, cwd: Path | None = None) -> str:
    return run_guard(root, "scope", payload, cwd=cwd)


BLOCKED = [
    ("Write", write(OUTSIDE)),
    ("绝对路径之外的相对写法", write("./" + OUTSIDE)),
    ("Edit", {"tool_name": "Edit", "tool_input": {"file_path": OUTSIDE, "old_string": "a", "new_string": "b"}}),
    ("重定向", bash(f"echo x > {OUTSIDE}")),
    ("追加", bash(f"echo x >> {OUTSIDE}")),
    ("rm", bash(f"rm {OUTSIDE}")),
    ("rm 上级目录", bash("rm -rf ontology/object")),
    ("mv 走", bash(f"mv {OUTSIDE} tmp/x.md")),
    ("cp 覆盖", bash(f"cp tmp/.keep {OUTSIDE}")),
    ("sed -i", bash(f"sed -i 's/a/b/' {OUTSIDE}")),
    ("python -c", bash(f"python3 -c \"open('{OUTSIDE}','w').write('x')\"")),
    ("通配删除", bash("rm ontology/object/*")),
    ("find -delete", bash("find ontology -name '*.md' -delete")),
    ("git checkout", bash(f"git checkout -- {OUTSIDE}")),
    ("git rm", bash(f"git rm {OUTSIDE}")),
    ("带变量的路径", bash("D=ontology; touch $D/object/仓库.md")),
    ("cd 之后", bash("cd ontology/object && rm 仓库.md")),
]


@pytest.mark.parametrize("name, payload", BLOCKED, ids=[case[0] for case in BLOCKED])
def test_out_of_scope_writes_are_blocked_without_a_command(scoped, name, payload):
    assert denied(guard(scoped, payload)), name


def test_denial_tells_how_to_widen_the_scope(scoped):
    message = json.loads(guard(scoped, write(OUTSIDE)))["hookSpecificOutput"]["permissionDecisionReason"]
    assert "只读" in message and "/incremental-scope" in message and "新增条目不受限" in message and "结束本轮" in message


ALLOWED = [
    ("Write 范围内", write(INSIDE)),
    ("Edit 范围内", {"tool_name": "Edit", "tool_input": {"file_path": INSIDE_OP, "old_string": "a", "new_string": "b"}}),
    ("rm 范围内", bash(f"rm {INSIDE}")),
    ("重定向范围内", bash(f"echo x > {INSIDE}")),
    ("新增条目", write("ontology/object/新对象.md")),
    ("新增操作", bash("echo x > ontology/operation/新操作.md")),
    ("新增规则", write("ontology/rule/新规则.md")),
    ("读", bash(f"cat {OUTSIDE}")),
    ("搜索", bash("grep -r 仓库 ontology/object")),
    ("sed 只读", bash(f"sed -n 1,5p {OUTSIDE}")),
    ("读当工作目录", bash("cd ontology/object && cat 仓库.md")),
    ("范围不管的目录", write("ontology/knowledge/备注.md")),
    ("目录索引", write("ontology/object/index.md")),
    ("工作区其他地方", write("tmp/草稿.md")),
]


@pytest.mark.parametrize("name, payload", ALLOWED, ids=[case[0] for case in ALLOWED])
def test_other_tool_calls_pass(scoped, name, payload):
    assert not denied(guard(scoped, payload)), name


def test_same_rule_applies_while_a_command_runs(scoped):
    acquire(scoped, "incremental-apply")
    assert denied(guard(scoped, write(OUTSIDE)))
    assert denied(guard(scoped, bash(f"rm {OUTSIDE}")))
    assert not denied(guard(scoped, write(INSIDE)))
    assert not denied(guard(scoped, write("ontology/object/新对象.md")))
    # 建模命令也一样：节点声明的写入范围放行，范围限定照样拦
    (scoped / ".ontology-state" / "lock-incremental-apply.json").unlink()
    acquire(scoped, "extract-object")
    assert denied(guard(scoped, write(OUTSIDE)))
    assert not denied(guard(scoped, write(INSIDE)))


def test_scope_file_is_only_writable_while_the_scope_command_runs(scoped):
    assert denied(guard(scoped, write(SCOPE_FILE)))
    assert denied(guard(scoped, bash(f"echo '{{}}' > {SCOPE_FILE}")))
    assert denied(guard(scoped, bash("rm -rf incremental")))
    assert not denied(guard(scoped, bash(f"cat {SCOPE_FILE}")))
    # 同一个节点的文件才归这条命令；别的命令拿不到
    acquire(scoped, "incremental-apply")
    assert denied(guard(scoped, write(SCOPE_FILE)))
    (scoped / ".ontology-state" / "lock-incremental-apply.json").unlink()
    acquire(scoped, "incremental-scope", pid=1)  # 持有者已经不在
    assert denied(guard(scoped, write(SCOPE_FILE)))
    acquire(scoped, "incremental-scope")
    assert not denied(guard(scoped, write(SCOPE_FILE)))


def test_editing_the_scope_file_in_the_copy_does_not_widen_the_scope(scoped, tmp_path):
    """范围只认主线：副本里改 scope.json（甚至提交了）在合入前都不算。"""
    copy = tmp_path / "copy"
    git(scoped, "worktree", "add", "-q", "-b", "agent", str(copy))
    (copy / ".claude").symlink_to((scoped / ".claude").resolve())
    assert denied(guard(copy, write(OUTSIDE), cwd=copy))
    baseline = json.loads((scoped / SCOPE_FILE).read_text(encoding="utf-8"))["baseline"]["commit"]
    wider = scope_doc(baseline, editable=(*ENTRIES,))
    (copy / SCOPE_FILE).write_text(wider, encoding="utf-8")
    assert denied(guard(copy, write(OUTSIDE), cwd=copy))
    commit(copy, "副本里改范围")
    assert denied(guard(copy, write(OUTSIDE), cwd=copy))
    # 主线上的范围变了（走圈定范围合入）：head 变了，缓存跟着失效
    set_scope(scoped, wider)
    assert not denied(guard(copy, write(OUTSIDE), cwd=copy))


def test_result_is_cached_by_mainline_head(scoped):
    assert denied(guard(scoped, write(OUTSIDE)))
    cache = json.loads((scoped / ".onto" / "run" / "confine-cache.json").read_text(encoding="utf-8"))
    assert cache["key"][:2] == [git(scoped, "rev-parse", "HEAD").strip(), SCOPE_FILE]
    assert OUTSIDE in cache["protected"] and INSIDE not in cache["protected"]
    # 缓存真的被用了：手工改掉，同一个 head 下照缓存算
    cache["protected"] = []
    (scoped / ".onto" / "run" / "confine-cache.json").write_text(json.dumps(cache), encoding="utf-8")
    assert not denied(guard(scoped, write(OUTSIDE)))
    (scoped / "ontology" / "operation" / "派送.md").write_text("# 派送\n合成\n", encoding="utf-8")
    commit(scoped)
    assert denied(guard(scoped, write(OUTSIDE)))


def test_lifted_when_the_pack_is_off(scoped):
    assert denied(guard(scoped, write(OUTSIDE)))
    set_flow(scoped, _flow())
    assert not denied(guard(scoped, write(OUTSIDE)))
    assert not denied(guard(scoped, write(SCOPE_FILE)))
    assert not denied(guard(scoped, bash(f"rm {OUTSIDE}")))


@pytest.mark.parametrize("build", [
    lambda baseline: "不是 json",
    lambda baseline: "[]",
    lambda baseline: scope_doc(baseline, status="closed"),
    lambda baseline: scope_doc("不是提交号"),
    lambda baseline: scope_doc("0" * 40),
    lambda baseline: json.dumps({"status": "active", "baseline": {"commit": baseline}}),
    lambda baseline: json.dumps({"status": "active", "baseline": "x", "editable": []}),
], ids=["坏 json", "不是对象", "已结束", "基线不是提交号", "基线不存在", "没有 editable", "基线类型不对"])
def test_unusable_scope_narrows_nothing(scoped, build):
    baseline = json.loads((scoped / SCOPE_FILE).read_text(encoding="utf-8"))["baseline"]["commit"]
    set_scope(scoped, build(baseline))
    assert not denied(guard(scoped, write(OUTSIDE)))


def test_no_scope_on_mainline_narrows_nothing(scoped):
    (scoped / SCOPE_FILE).unlink()
    commit(scoped, "去掉范围")
    assert not denied(guard(scoped, write(OUTSIDE)))


def test_scope_confirmed_in_the_copy_tightens_at_once(ws):  # noqa: F811
    """范围文件刚确认、还没进主线：收紧马上算，确认的这一轮接着改也受限。"""
    git(ws, "config", "user.email", "tester@example.com")
    git(ws, "config", "user.name", "tester")
    (ws / ".gitignore").write_text(".claude\nCLAUDE.md\n.onto/run/\n", encoding="utf-8")
    path = ws / OUTSIDE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# 仓库\n", encoding="utf-8")
    baseline = commit(ws)
    set_flow(ws, _flow("incremental"))
    (ws / SCOPE_FILE).parent.mkdir(parents=True, exist_ok=True)
    (ws / SCOPE_FILE).write_text(scope_doc(baseline, editable=()), encoding="utf-8")
    assert denied(guard(ws, write(OUTSIDE)))
    # 副本里结束本轮不算放宽，没有主线上的范围时才放开
    (ws / SCOPE_FILE).write_text(scope_doc(baseline, editable=(), status="closed"), encoding="utf-8")
    assert not denied(guard(ws, write(OUTSIDE)))


def test_entries_added_after_the_baseline_stay_writable(scoped):
    (scoped / "ontology" / "object" / "新对象.md").write_text("# 新对象\n", encoding="utf-8")
    commit(scoped)
    assert not denied(guard(scoped, write("ontology/object/新对象.md")))
    assert denied(guard(scoped, write(OUTSIDE)))


# ---- 真实项目：启用包、助手副本、停用包 ----


@pytest.fixture()
def project(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    monkeypatch.setattr(auth, "ENABLED", False)
    manager = WorkspaceManager(tmp_path / "data", ASSETS)
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(changesets, "_activity_provider", None)
    project_id = manager.create_project("范围限定")["id"]
    yield TestClient(app), manager, project_id, manager.project_root(project_id)
    builtin.clear_cache()


def test_real_project_copy_is_confined_until_the_pack_is_disabled(project):
    client, manager, project_id, root = project
    for rel in ENTRIES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {Path(rel).stem}\n", encoding="utf-8")
    baseline = commit(root, "合成基线")
    copy = changesets.copy_for_turn(manager, project_id, root)
    assert not denied(guard(copy, write(OUTSIDE), cwd=copy))  # 没启用包的项目和以前一样

    assert client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "incremental"}).status_code == 201
    pack_service.prepare_for_message(manager, project_id)
    set_scope(root, scope_doc(baseline))
    copy = changesets.copy_for_turn(manager, project_id, root)
    assert denied(guard(copy, write(OUTSIDE), cwd=copy))
    assert not denied(guard(copy, write(INSIDE), cwd=copy))
    assert denied(guard(copy, write(SCOPE_FILE), cwd=copy))

    assert client.post(f"/api/v1/projects/{project_id}/packs/incremental/disable").status_code == 200
    pack_service.prepare_for_message(manager, project_id)
    copy = changesets.copy_for_turn(manager, project_id, root)
    assert not denied(guard(copy, write(OUTSIDE), cwd=copy))
