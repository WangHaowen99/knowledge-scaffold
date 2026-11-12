"""内核守卫：出错即拒与常见绕过写法（方案 D9、D10；详设 4.8 实测表）。

直接跑内核守卫的 shell 包装（与 Claude CLI、Jiuwen rail 执行的是同一份），stdin 喂 hook 协议
JSON。工作区与真实项目同构：`.claude` 是指向 `<数据目录>/assembled/<哈希>` 的符号链接。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from app import kernel_guards
from tests.proc import run as run_cmd

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None,
                                reason="需要 bash 与 git")


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    assembled = tmp_path / "data" / "assembled" / "0123456789abcdef"
    (assembled / "scripts").mkdir(parents=True)
    for script in kernel_guards.scripts():
        shutil.copy2(script, assembled / "scripts" / script.name)
    (tmp_path / "data" / "packs").mkdir()
    root = tmp_path / "data" / "workspaces" / "demo"
    root.mkdir(parents=True)
    run_cmd(["git", "init", "-q"], cwd=root, check=True)
    (root / ".claude").symlink_to(Path("../../assembled/0123456789abcdef"))
    (root / "CLAUDE.md").symlink_to(Path(".claude/CLAUDE.md"))
    (assembled / "CLAUDE.md").write_text("# 规则\n", encoding="utf-8")
    for rel in ("resource/a.md", "resource/systems/s1/x.json", "ontology/object/订单.md", "tmp/.keep",
                ".onto/packs.lock", "ontology/solver/s/binding.yaml"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    return root


def run_guard(root: Path, guard: str, payload, *, raw: str | None = None, cwd: Path | None = None) -> str:
    script = {"resource": "resource_write_guard.sh", "toolset": "toolset_write_guard.sh",
              "lock": "lock_file_guard.sh", "scope": "ontology_scope_guard.sh"}[guard]
    data = raw if raw is not None else json.dumps(payload, ensure_ascii=False)
    proc = run_cmd(
        ["bash", str(root / ".claude" / "scripts" / script)],
        input=data, cwd=cwd or root, capture_output=True, text=True, timeout=30,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(root)},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def denied(output: str) -> bool:
    if not output:
        return False
    decision = json.loads(output.splitlines()[-1])
    return decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def write(path: str) -> dict:
    return {"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}}


# 详设 4.8「resource_write_guard 实测结果」整张表，加上方案 D10 列出的补洞写法
RESOURCE_CASES = [
    # 原来就拦的
    ("Write 资料", write("resource/a.md"), True),
    ("重定向", bash("echo x > resource/a.md"), True),
    ("rm -rf", bash("rm -rf resource/systems"), True),
    ("sed -i", bash("sed -i 's/x/y/' resource/a.md"), True),
    # 原来放行、现在要拦的
    ("python -c 写文件", bash("python3 -c \"open('resource/a.md','w').write('x')\""), True),
    ("cd 之后的相对路径", bash("cd resource && touch a.md"), True),
    ("find -delete", bash("find resource -name '*.md' -delete"), True),
    ("git checkout --", bash("git checkout -- resource/"), True),
    ("带变量的路径", bash("D=resource; touch $D/a.md"), True),
    # 其他常见写法
    ("变量代不出", bash("for f in resource/*; do echo > $f; done"), True),
    ("bash -c", bash("bash -c 'touch resource/b.md'"), True),
    ("git -C 子目录", bash("git -C resource checkout ."), True),
    ("git restore", bash("git restore resource/a.md"), True),
    ("git rm", bash("git rm -r resource/systems"), True),
    ("git clean 不带路径", bash("git clean -fdx"), True),
    ("rm -rf .", bash("rm -rf ."), True),
    ("rm -rf 通配", bash("rm -rf *"), True),
    ("find | xargs rm", bash("find resource -name '*.json' | xargs rm -f"), True),
    ("find -exec rm", bash("find resource -type f -exec rm {} +"), True),
    ("mv 移走", bash("mv resource/a.md tmp/a.md"), True),
    ("cp 进资料区", bash("cp tmp/.keep resource/"), True),
    ("tee", bash("echo x | tee resource/a.md"), True),
    ("perl -pi", bash("perl -pi -e 's/x/y/' resource/a.md"), True),
    ("python heredoc", bash("python3 - <<'EOF'\nopen('resource/a.md', 'w').write('x')\nEOF"), True),
    ("node -e", bash("node -e \"require('fs').writeFileSync('resource/a.md','x')\""), True),
    ("绝对路径", None, True),  # 运行时填入
    # 读资料、写派生产物：放行
    ("cat 读资料", bash("cat resource/a.md"), False),
    ("python -c 读资料", bash("python3 -c \"print(open('resource/a.md').read())\""), False),
    ("python -c 读表格", bash("python3 -c \"import json; rows=json.load(open('resource/systems/s1/x.json')); print(len(rows) > 3)\""), False),
    ("find 只列文件", bash("find resource -name '*.md'"), False),
    ("find -exec grep", bash("find . -name '*.md' -exec grep -l 订单 {} \\;"), False),
    ("写本体", bash("echo x > ontology/object/订单.md"), False),
    ("cd 之后写本体", bash("cd ontology && touch object/新.md"), False),
    ("git checkout 本体", bash("git checkout -- ontology/object/订单.md"), False),
    ("Write 本体", write("ontology/object/订单.md"), False),
    ("变量不涉及资料区", bash("touch $HOME/notes.txt"), False),
]


@pytest.mark.parametrize("label, payload, expected", RESOURCE_CASES, ids=[case[0] for case in RESOURCE_CASES])
def test_guard_bypass_resource(ws: Path, label, payload, expected):
    if payload is None:
        payload = write(str(ws / "resource" / "a.md"))
    assert denied(run_guard(ws, "resource", payload)) is expected, label


def test_resource_guard_uses_hook_cwd(ws: Path):
    # Bash 工具的工作目录会保留上一次 cd 的结果：hook 输入里的 cwd 是准的
    payload = {**bash("touch new.md"), "cwd": str(ws / "resource")}
    assert denied(run_guard(ws, "resource", payload))


TOOLSET_CASES = [
    ("Write .claude 脚本", write(".claude/scripts/state_helper.py"), True),
    ("Edit CLAUDE.md", {"tool_name": "Edit", "tool_input": {"file_path": "CLAUDE.md", "old_string": "a", "new_string": "b"}}, True),
    ("改锁文件", bash("echo '{}' > .onto/packs.lock"), True),
    ("cd .claude 之后写", bash("cd .claude && echo x > scripts/a.sh"), True),
    ("python -c 写 .claude", bash("python3 -c \"open('.claude/x.md','w').write('x')\""), True),
    ("删 .git", bash("rm -rf .git"), True),
    ("经真实路径写装配目录", None, True),
    ("写包仓库", None, True),
    ("写 tmp", write("tmp/helper.py"), False),
    ("执行工具箱脚本", bash("python3 .claude/scripts/state_helper.py detect-stage"), False),
    ("读 CLAUDE.md", bash("cat CLAUDE.md"), False),
    ("git commit", bash("git add ontology && git commit -m 'extract: 对象'"), False),
]


@pytest.mark.parametrize("label, payload, expected", TOOLSET_CASES, ids=[case[0] for case in TOOLSET_CASES])
def test_guard_bypass_toolset(ws: Path, label, payload, expected):
    if label == "经真实路径写装配目录":
        payload = write(str((ws / ".claude").resolve() / "scripts" / "x.sh"))
    if label == "写包仓库":
        payload = bash(f"touch {(ws / '.claude').resolve().parent.parent / 'packs' / 'x'}")
    assert denied(run_guard(ws, "toolset", payload)) is expected, label


LOCK_CASES = [
    ("Edit 锁文件", write(".ontology-state/lock-research.json"), True),
    ("重定向写锁文件", bash("echo '{}' > .ontology-state/lock-validate.json"), True),
    ("写验证记录", write(".ontology-state/script-verifications.json"), True),
    ("删整个状态目录", bash("rm -rf .ontology-state"), True),
    ("写进度快照", write(".ontology-state/progress.json"), False),
]


@pytest.mark.parametrize("label, payload, expected", LOCK_CASES, ids=[case[0] for case in LOCK_CASES])
def test_guard_bypass_lock(ws: Path, label, payload, expected):
    assert denied(run_guard(ws, "lock", payload)) is expected, label


SCOPE_CASES = [
    ("自创目录", write("ontology/cases/案例.md"), True),
    ("写求解受管目录", write("ontology/solver/s/binding.yaml"), True),
    ("python -c 写求解目录", bash("python3 -c \"open('ontology/solver/s/x.yaml','w').write('x')\""), True),
    ("cd 之后自创目录", bash("cd ontology && mkdir -p cases/x && touch cases/x/a.md"), True),
    ("删求解目录", bash("rm -rf ontology"), True),
    ("写知识条目", write("ontology/knowledge/条目.md"), False),
    ("写对象", write("ontology/object/订单.md"), False),
]


@pytest.mark.parametrize("label, payload, expected", SCOPE_CASES, ids=[case[0] for case in SCOPE_CASES])
def test_guard_bypass_scope(ws: Path, label, payload, expected):
    assert denied(run_guard(ws, "scope", payload)) is expected, label


EVOLUTION_ALLOW = ["ontology/object/*", "ontology/operation/*", "ontology/logic/*", "ontology/rule/*",
                   "ontology/metric/*", "ontology/scripts/*", "ontology/testcase/*", "ontology/data-mock/*"]


def mark_active(ws: Path, commands: dict) -> None:
    """state_helper acquire 写的 `.onto/run/active.json`：正在执行的命令与节点声明的写入范围（方案 D11），
    连同各命令的锁（守卫只认锁还在的命令，审查第 9 条；这里的锁不带持有者信息，按还在跑）。"""
    path = ws / ".onto" / "run" / "active.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"commands": commands}, ensure_ascii=False), encoding="utf-8")
    state = ws / ".ontology-state"
    state.mkdir(exist_ok=True)
    for lock in state.glob("lock-*.json"):
        lock.unlink()
    for command in commands:
        (state / f"lock-{command}.json").write_text(json.dumps({"command": command}), encoding="utf-8")


def test_scope_guard_evolution_only_allows_packaged_dirs(ws: Path):
    """原来守卫里反馈演进的特判，改成反馈演进节点声明的写入范围（方案 D11）。"""
    mark_active(ws, {"evolution": {"node": "evo.evolution", "allow": EVOLUTION_ALLOW}})
    reason = run_guard(ws, "scope", write("ontology/knowledge/条目.md"))
    assert denied(reason) and "evolution" in reason
    assert not denied(run_guard(ws, "scope", write("ontology/rule/新规则.md")))


def test_scope_guard_only_allows_the_running_nodes_scope(ws: Path):
    mark_active(ws, {"extract-object": {"node": "core.object", "allow": ["ontology/object/*"]}})
    assert not denied(run_guard(ws, "scope", write("ontology/object/订单.md")))
    assert denied(run_guard(ws, "scope", write("ontology/operation/下单.md")))
    assert denied(run_guard(ws, "scope", {"tool_name": "Bash", "tool_input": {"command": "rm ontology/rule/r.md"}}))
    # 不在本体目录里的写入不收窄（报告、临时文件）
    assert not denied(run_guard(ws, "scope", write("reports/r.md")))
    # 两条命令同时在跑：放行两者范围的并集
    mark_active(ws, {"extract-object": {"node": "core.object", "allow": ["ontology/object/*"]},
                     "validate": {"node": "core.validate", "allow": ["ontology/*"]}})
    assert not denied(run_guard(ws, "scope", write("ontology/operation/下单.md")))
    # 有不属于任何节点的命令（如 /ontology 总控）：不收窄
    mark_active(ws, {"extract-object": {"node": "core.object", "allow": ["ontology/object/*"]},
                     "ontology": {"node": None, "allow": None}})
    assert not denied(run_guard(ws, "scope", write("ontology/operation/下单.md")))
    # 文件坏了按没有处理
    (ws / ".onto" / "run" / "active.json").write_text("not json", encoding="utf-8")
    assert not denied(run_guard(ws, "scope", write("ontology/operation/下单.md")))
    # 命令的锁没了（打断、重启没跑到 release）：它的范围不再算数
    mark_active(ws, {"extract-object": {"node": "core.object", "allow": ["ontology/object/*"]}})
    (ws / ".ontology-state" / "lock-extract-object.json").unlink()
    assert not denied(run_guard(ws, "scope", write("ontology/operation/下单.md")))


# ---- 出错即拒（D9）----


@pytest.mark.parametrize("guard", ["resource", "toolset", "lock", "scope"])
@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", '{"tool_name": "Bash", "tool_input": ["x"]}'])
def test_guard_errors_deny(ws: Path, guard, raw):
    assert denied(run_guard(ws, guard, None, raw=raw))


def test_missing_guard_script_blocks_via_exit_code(ws: Path):
    # settings.json 里每条守卫都带 `|| exit 2`：脚本缺失（127）也变成阻断
    proc = run_cmd(
        ["bash", "-c", 'bash "$CLAUDE_PROJECT_DIR/.claude/scripts/no_such_guard.sh" || exit 2'],
        input="{}", capture_output=True, text=True, env={**os.environ, "CLAUDE_PROJECT_DIR": str(ws)},
    )
    assert proc.returncode == 2


def test_settings_base_wraps_every_guard_with_exit_2():
    settings = kernel_guards.settings_base()
    commands = [hook["command"] for entry in settings["hooks"]["PreToolUse"] for hook in entry["hooks"]]
    assert commands and all(command.endswith("|| exit 2") for command in commands)
    assert all("$CLAUDE_PROJECT_DIR" in command for command in commands)


# ---- 提交检查（pre_commit_check）----


def run_pre_commit(root: Path, command: str) -> str:
    proc = run_cmd(
        ["bash", str(root / ".claude" / "scripts" / "pre_commit_check.sh")],
        input=json.dumps(bash(command)), cwd=root, capture_output=True, text=True, timeout=60,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(root)},
    )
    return proc.stdout


@pytest.mark.parametrize("command", [
    "git commit -m x",
    "git -C . commit -m x",
    "git -c user.name=x commit -m x",
    "/usr/bin/git commit -m x",
    "cd ontology && git commit -m x",
])
def test_pre_commit_check_recognizes_commit_variants(ws: Path, command):
    run_cmd(["git", "add", "-f", "resource/a.md"], cwd=ws, check=True)
    assert '"block"' in run_pre_commit(ws, command)


def test_pre_commit_check_blocks_staged_lock_file(ws: Path):
    run_cmd(["git", "add", "-f", ".onto/packs.lock"], cwd=ws, check=True)
    assert "能力包与锁文件" in run_pre_commit(ws, "git commit -m x")


def test_pre_commit_check_blocks_unreadable_input(ws: Path):
    proc = run_cmd(
        ["bash", str(ws / ".claude" / "scripts" / "pre_commit_check.sh")],
        input="not json", cwd=ws, capture_output=True, text=True, timeout=30,
    )
    assert '"block"' in proc.stdout


def test_pre_commit_check_ignores_non_commit_commands(ws: Path):
    run_cmd(["git", "add", "-f", "resource/a.md"], cwd=ws, check=True)
    assert run_pre_commit(ws, "git status") == ""
