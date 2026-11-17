"""内核的范围限定 confine（ADR-0085）：范围生效时，基线里不在 editable 的条目对助手只读；组装和上传时校验声明。

直接跑内核守卫的 shell 包装，流程用真实的组装结果（真实的 core 加 tests/pack_helpers 里合成的限定范围包）。
条目、范围、包都是合成的。
"""

from __future__ import annotations

import copy
import json
import os
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
from app.services.packs import builtin, checks
from app.services.packs import service as pack_service
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import ROUND_PACK, ROUND_PACK_FLOW, ROUND_SCOPE_FILE, install_round_pack, make_pack
from tests.proc import git
from tests.test_dev_skill import pack_tool, skill_dir
from tests.test_guard_bypass import bash, denied, run_guard, write, ws  # noqa: F401 - 共用 fixture 与工具
from tests.test_private_packs import project as make_project, real_flow_env  # noqa: F401 - 共用 fixture

ASSETS = Path(config.ASSETS_DIR)
SCOPE_FILE = ROUND_SCOPE_FILE
OUTSIDE = "ontology/object/仓库.md"
INSIDE = "ontology/object/订单.md"
INSIDE_OP = "ontology/operation/下单.md"
ENTRIES = (OUTSIDE, INSIDE, INSIDE_OP, "ontology/operation/派送.md")


def _core() -> tuple[str, str, dict]:
    return ("core", "0.5.0", yaml.safe_load((ASSETS / "workspace-toolkit" / "flow.yaml").read_text(encoding="utf-8")))


def _round() -> tuple[str, str, dict]:
    return (ROUND_PACK, "0.1.0", copy.deepcopy(ROUND_PACK_FLOW))


def _flow(*, confined: bool = True) -> str:
    return assemble.render(assemble.assemble([_core(), _round()] if confined else [_core()]))


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
    set_flow(ws, _flow())
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


def test_denial_names_the_command_that_sets_the_scope(scoped):
    """拒绝说明只讲范围由哪条命令确定，不承诺能扩大（领域包扩展的底座就放不宽）。"""
    message = json.loads(guard(scoped, write(OUTSIDE)))["hookSpecificOutput"]["permissionDecisionReason"]
    assert OUTSIDE in message and "不在本轮可改范围内" in message and "范围由 /intake-scope 确定" in message
    assert "新增条目不受限" in message and "扩大" not in message


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
    acquire(scoped, "intake-apply")
    assert denied(guard(scoped, write(OUTSIDE)))
    assert denied(guard(scoped, bash(f"rm {OUTSIDE}")))
    assert not denied(guard(scoped, write(INSIDE)))
    assert not denied(guard(scoped, write("ontology/object/新对象.md")))
    # 建模命令也一样：节点声明的写入范围放行，范围限定照样拦
    (scoped / ".ontology-state" / "lock-intake-apply.json").unlink()
    acquire(scoped, "extract-object")
    assert denied(guard(scoped, write(OUTSIDE)))
    assert not denied(guard(scoped, write(INSIDE)))


def test_scope_file_is_only_writable_while_the_scope_command_runs(scoped):
    assert denied(guard(scoped, write(SCOPE_FILE)))
    assert denied(guard(scoped, bash(f"echo '{{}}' > {SCOPE_FILE}")))
    assert denied(guard(scoped, bash("rm -rf intake")))
    assert not denied(guard(scoped, bash(f"cat {SCOPE_FILE}")))
    # 同一个节点的文件才归这条命令；别的命令拿不到
    acquire(scoped, "intake-apply")
    assert denied(guard(scoped, write(SCOPE_FILE)))
    (scoped / ".ontology-state" / "lock-intake-apply.json").unlink()
    acquire(scoped, "intake-scope", pid=1)  # 持有者已经不在
    assert denied(guard(scoped, write(SCOPE_FILE)))
    acquire(scoped, "intake-scope")
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
    set_flow(scoped, _flow(confined=False))
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
    set_flow(ws, _flow())
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
    install_round_pack(manager, tmp_path)
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

    assert client.post(f"/api/v1/projects/{project_id}/packs", json={"name": ROUND_PACK}).status_code == 201
    pack_service.prepare_for_message(manager, project_id)
    set_scope(root, scope_doc(baseline))
    copy = changesets.copy_for_turn(manager, project_id, root)
    assert denied(guard(copy, write(OUTSIDE), cwd=copy))
    assert not denied(guard(copy, write(INSIDE), cwd=copy))
    assert denied(guard(copy, write(SCOPE_FILE), cwd=copy))

    assert client.post(f"/api/v1/projects/{project_id}/packs/{ROUND_PACK}/disable").status_code == 200
    pack_service.prepare_for_message(manager, project_id)
    copy = changesets.copy_for_turn(manager, project_id, root)
    assert not denied(guard(copy, write(OUTSIDE), cwd=copy))


# ---- 声明的校验：组装、上传检查、开发 skill ----


def test_assembly_records_the_owner_of_the_scope_file():
    assert assemble.assemble([_core(), _round()])["confine"] == {
        "scope_file": SCOPE_FILE, "pack": ROUND_PACK, "node": "intake.scope", "entry": "/intake-scope",
        "lock_on_activation": True}
    assert "confine" not in assemble.assemble([_core()])


def _with_confine(confine) -> tuple[str, str, dict]:
    pack, version, data = _round()
    return pack, version, {**data, "confine": confine}


@pytest.mark.parametrize("confine, message", [
    ("intake/scope.json", "只能写 scope_file"),
    ({"scope_file": SCOPE_FILE, "other": 1}, "只能写 scope_file"),
    ({"scope_file": ""}, "具体文件"),
    ({"scope_file": "/etc/scope.json"}, "具体文件"),
    ({"scope_file": "intake/../scope.json"}, "具体文件"),
    ({"scope_file": "intake\\scope.json"}, "具体文件"),
    ({"scope_file": "intake/*.json"}, "具体文件"),
    ({"scope_file": "ontology/object/scope.json"}, "具体文件"),
    ({"scope_file": ".onto/scope.json"}, "具体文件"),
    ({"scope_file": ".claude/scope.json"}, "具体文件"),
    ({"scope_file": "resource/scope.json"}, "具体文件"),
    ({"scope_file": "intake/other.json"}, "produces"),
])
def test_assembly_refuses_a_bad_confine(confine, message):
    with pytest.raises(assemble.FlowAssemblyError, match=message):
        assemble.assemble([_core(), _with_confine(confine)])


def test_only_one_pack_may_declare_confine():
    second = {"confine": {"scope_file": "second/scope.json"}, "nodes": [{
        "id": "second.scope", "title": "合成范围", "kind": "agent", "placement": "step", "section": "required",
        "after": "core.operation", "done": [], "produces": ["second/scope.json"], "entry": {"command": "/ontology"},
    }]}
    with pytest.raises(assemble.FlowAssemblyError, match="只能有一个包声明 confine"):
        assemble.assemble([_core(), _round(), ("second", "0.1.0", second)])
    # 一个包声明就没事
    assert assemble.assemble([_core(), ("second", "0.1.0", second)])["confine"]["pack"] == "second"


def _synthetic_pack(base: Path, confine) -> Path:
    flow = {"nodes": [{"id": "my-scope.scope", "title": "合成范围", "kind": "agent", "placement": "step",
                       "section": "required", "after": "core.operation", "done": [],
                       "produces": ["my-scope/scope.json"], "entry": {"command": "/my-scope-run"}}]}
    if confine is not None:
        flow["confine"] = confine
    return make_pack(base, "my-scope", "0.1.0", files={
        "commands/my-scope-run.md": "运行 $ARGUMENTS\n", "flow.yaml": yaml.safe_dump(flow, allow_unicode=True),
    }, contributes={"flow": "flow.yaml", "nodes": ["my-scope.scope"]})


@pytest.mark.parametrize("confine, result", [
    ({"scope_file": "my-scope/scope.json"}, "pass"),
    ({"scope_file": "my-scope/other.json"}, "fail"),
    ({"scope_file": "ontology/scope.json"}, "fail"),
    ({"scope_file": ".onto/scope.json"}, "fail"),
    ({"scope_file": "../scope.json"}, "fail"),
    ({"scope_file": "my-scope/*.json"}, "fail"),
    ({"scope_file": "my-scope/scope.json", "extra": 1}, "fail"),
    ("my-scope/scope.json", "fail"),
])
def test_static_check_reads_confine(tmp_path, confine, result):
    _, items = checks.run(_synthetic_pack(tmp_path, confine), core_names={})
    assert next(item for item in items if item["id"] == "confine")["result"] == result


def test_static_check_has_no_confine_item_without_the_declaration(tmp_path):
    _, items = checks.run(_synthetic_pack(tmp_path, None), core_names={})
    assert "confine" not in {item["id"] for item in items}


def test_local_check_reports_a_bad_confine(real_flow_env, tmp_path):  # noqa: F811
    client, _manager, _tmp = real_flow_env
    make_project(client)
    skill = skill_dir(client, tmp_path, "capability")
    source = tmp_path / "bad" / "my-scope-0.1.0"
    _synthetic_pack(tmp_path / "bad", {"scope_file": "ontology/scope.json"})
    done = pack_tool(skill, "check", source, "--json", cwd=tmp_path)
    assert done.returncode != 0
    assert next(item for item in json.loads(done.stdout)["checks"] if item["id"] == "confine")["result"] == "fail"
