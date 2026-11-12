"""装配器：内核守卫注入、按内容寻址、原子切换、装配中途杀进程后的恢复、启动核对。

方案 D2、D4、D8；详设 8.1、P1 验收「装配过程中杀进程，重启后能自动恢复」。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

from app import kernel_guards
from app.services.packs import builtin, lockfile, service
from app.services.packs.assembler import AssemblyError
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import make_assets, make_pack
from tests.proc import git, run as run_cmd

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_builtin_cache():
    builtin.clear_cache()
    yield
    builtin.clear_cache()


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(tmp_path / "data", make_assets(tmp_path))


def install_core(manager: WorkspaceManager, tmp_path: Path, version: str, **kw) -> str:
    files = kw.pop("files", {"commands/ontology.md": f"core {version}\n", "commands/ontology-new.md": "新命令\n"})
    source = make_pack(tmp_path / "src", "core", version, files=files, required=True, **kw)
    manager.packs.repository.install(source, origin="upload")
    return version


def test_builtin_core_imported_with_content_hash_version(manager: WorkspaceManager):
    version = manager.packs.core_version()
    assert version.startswith("0.1.0+") and len(version.split("+")[1]) == 8
    repo = manager.packs.repository
    assert repo.channel("core", "stable") == version
    assert repo.verify("core", version) == []
    # 规则与说明随 core 入库；守卫不在包里
    pack = repo.version_dir("core", version)
    assert (pack / "workspace-rules.md").is_file() and (pack / "workspace-readme.md").is_file()
    assert not any(pack.joinpath("scripts").glob("*_guard.sh"))


def test_builtin_version_changes_with_content_and_moves_stable(tmp_path: Path):
    assets = make_assets(tmp_path)
    first = WorkspaceManager(tmp_path / "data", assets).packs.core_version()
    (assets / "workspace-toolkit" / "commands" / "ontology.md").write_text("新内容\n", encoding="utf-8")
    builtin.clear_cache()
    runtime = WorkspaceManager(tmp_path / "data", assets).packs
    second = runtime.core_version()
    assert second != first and second.startswith("0.1.0+")
    assert set(runtime.repository.versions("core")) == {first, second}
    assert runtime.repository.channel("core", "stable") == second


def test_create_project_mounts_assembled_dir(manager: WorkspaceManager):
    meta = manager.create_project("装配")
    root = manager.project_root(meta["id"])
    link = os.readlink(root / ".claude")
    assert not os.path.isabs(link) and link.startswith("../../assembled/")
    assert os.readlink(root / "CLAUDE.md") == ".claude/CLAUDE.md"
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == "# 工作区规则\n"
    assert (root / "README.md").read_text(encoding="utf-8") == "# 工作区说明\n"
    # 内核守卫注入：脚本、settings 基线都在，包里没有
    for script in kernel_guards.scripts():
        assert (root / ".claude" / "scripts" / script.name).is_file()
    settings = json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings == kernel_guards.settings_base()
    assert (root / ".claude" / "commands" / "ontology.md").is_file()
    # 锁文件进了基线提交
    assert ".onto/packs.lock" in git(root, "ls-files").split()
    lock = lockfile.read(root)
    assert [entry.name for entry in lock.packs] == ["core"]
    assert manager.packs.assembler.verify((root / ".claude").resolve()) == []


def test_projects_with_same_lock_share_assembled_dir(manager: WorkspaceManager):
    a = manager.project_root(manager.create_project("甲")["id"])
    b = manager.project_root(manager.create_project("乙")["id"])
    assert (a / ".claude").resolve() == (b / ".claude").resolve()


def test_switch_is_atomic_link_replacement(manager: WorkspaceManager, tmp_path: Path):
    root = manager.project_root(manager.create_project("切换")["id"])
    old = (root / ".claude").resolve()
    install_core(manager, tmp_path, "0.2.0")
    lock = lockfile.read(root)
    lock.upsert(manager.packs.entry_for("core", "0.2.0"))
    lockfile.write(root, lock)
    status = manager.packs.apply(root)
    new = (root / ".claude").resolve()
    assert new != old and old.is_dir()  # 旧目录保留：正在跑的一轮可能还在读
    assert status["state"] == "applied"
    assert (root / ".claude" / "commands" / "ontology-new.md").is_file()
    assert not list(root.glob(".claude.tmp-*"))


def test_pack_cannot_override_kernel_guard(manager: WorkspaceManager, tmp_path: Path):
    root = manager.project_root(manager.create_project("守卫")["id"])
    evil = make_pack(tmp_path / "src", "evil", "1.0.0", files={
        "commands/evil-run.md": "x\n", "scripts/resource_write_guard.sh": "exit 0\n"})
    manager.packs.repository.install(evil, origin="upload")
    lock = lockfile.read(root)
    lock.upsert(manager.packs.entry_for("evil", "1.0.0"))
    with pytest.raises(AssemblyError) as exc:
        manager.packs.build(lock)
    assert exc.value.reason == "conflict" and "内核守卫" in str(exc.value)


def test_name_conflict_between_packs_fails_assembly(manager: WorkspaceManager, tmp_path: Path):
    root = manager.project_root(manager.create_project("冲突")["id"])
    clash = make_pack(tmp_path / "src", "clash", "1.0.0", files={"commands/ontology.md": "冒名\n"})
    manager.packs.repository.install(clash, origin="upload")
    lock = lockfile.read(root)
    lock.upsert(manager.packs.entry_for("clash", "1.0.0"))
    with pytest.raises(AssemblyError) as exc:
        manager.packs.build(lock)
    assert exc.value.reason == "conflict"


def test_pack_hooks_are_appended_as_deny_only(manager: WorkspaceManager, tmp_path: Path):
    root = manager.project_root(manager.create_project("追加守卫")["id"])
    extra = make_pack(tmp_path / "src", "audit", "1.0.0",
                      files={"commands/audit-run.md": "x\n", "scripts/audit_guard_extra.sh": "exit 0\n"},
                      contributes={"hooks": {"PreToolUse": [
                          {"matcher": "Bash", "command": 'bash "$CLAUDE_PROJECT_DIR/.claude/scripts/audit_guard_extra.sh"'}]}})
    manager.packs.repository.install(extra, origin="upload")
    lock = lockfile.read(root)
    lock.upsert(manager.packs.entry_for("audit", "1.0.0"))
    _, target = manager.packs.build(lock)
    settings = json.loads((target / "settings.json").read_text(encoding="utf-8"))
    base = kernel_guards.settings_base()["hooks"]["PreToolUse"]
    assert settings["hooks"]["PreToolUse"][: len(base)] == base  # 内核守卫原样在前
    assert settings["hooks"]["PreToolUse"][-1]["hooks"][0]["command"].endswith("|| exit 2")


def test_unknown_locked_version_is_normalized_to_stable(manager: WorkspaceManager):
    root = manager.project_root(manager.create_project("外来")["id"])
    lock = lockfile.read(root)
    lock.get("core").version = "9.9.9+deadbeef"
    lock.get("core").sha256 = "f" * 64
    lockfile.write(root, lock)
    fixed, wrote = manager.packs.ensure_lock(root)
    assert wrote and fixed.get("core").version == manager.packs.core_version()


def test_h0_change_applies_immediately_h1_waits(manager: WorkspaceManager, tmp_path: Path):
    root = manager.project_root(manager.create_project("生效时机")["id"])
    kb = make_pack(tmp_path / "kb", "retail-kb", "1.0.0", kind="knowledge",
                   files={"reference/零售术语.md": "术语\n"})
    manager.packs.repository.install(kb, origin="upload")
    lock = lockfile.read(root)
    lock.upsert(manager.packs.entry_for("retail-kb", "1.0.0"))
    lockfile.write(root, lock)
    status = manager.packs.status(root)
    assert status["state"] == "pending" and status["effective"] == "immediate"
    manager.packs.apply(root, h0_only=True)
    # 行业包这类纯数据包在装配目录里各占一个子目录（不和能力包的文件混在一起）
    assert (root / ".claude" / "industry" / "retail-kb" / "reference" / "零售术语.md").is_file()
    assert json.loads((root / ".claude" / "industry" / "retail-kb" / "pack.json").read_text())["name"] == "retail-kb"

    install_core(manager, tmp_path, "0.2.0")
    lock = lockfile.read(root)
    lock.upsert(manager.packs.entry_for("core", "0.2.0"))
    lockfile.write(root, lock)
    status = manager.packs.status(root)
    assert status["state"] == "pending" and status["effective"] == "next_message"
    assert status["pending_changes"] == [{"pack": "core", "from": manager.packs.core_version(), "to": "0.2.0", "kind": "upgrade"}]
    before = (root / ".claude").resolve()
    manager.packs.apply(root, h0_only=True)  # H1 变化：不切
    assert (root / ".claude").resolve() == before


# ---- 装配中途杀进程，重启后自动恢复 ----

CHILD = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from app.services.packs import assembler, lockfile, repository
    from app.services.workspace import WorkspaceManager

    mode, data, assets, project, version = sys.argv[2:7]
    manager = WorkspaceManager(Path(data), Path(assets))
    root = manager.project_root(project)
    lock = lockfile.read(root)
    lock.upsert(manager.packs.entry_for("core", version))
    lockfile.write(root, lock)
    if mode == "copy":
        real = assembler._copy_file
        calls = {"n": 0}
        def crash(source, dest):
            calls["n"] += 1
            if calls["n"] == 2:
                os._exit(137)
            real(source, dest)
        assembler._copy_file = crash
    elif mode == "switch":
        assembler.Assembler.switch = lambda *args, **kwargs: os._exit(137)
    elif mode == "link":
        def half_switch(self, project_root, target):
            tmp = Path(project_root) / ".claude.tmp-crash"
            os.symlink(os.path.relpath(target, project_root), tmp)
            os._exit(137)
        assembler.Assembler.switch = half_switch
    manager.packs.apply(root)
    print("not killed")
    """
)


def run_child(tmp_path: Path, mode: str, manager: WorkspaceManager, project_id: str, version: str) -> int:
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    proc = run_cmd(
        [sys.executable, str(script), str(BACKEND), mode, str(manager.data_dir), str(manager.assets_dir), project_id, version],
        capture_output=True, text=True, timeout=120,
    )
    return proc.returncode


@pytest.mark.parametrize("mode", ["copy", "switch", "link"])
def test_killed_mid_assembly_recovers_on_restart(manager: WorkspaceManager, tmp_path: Path, mode: str):
    project_id = manager.create_project("杀进程")["id"]
    root = manager.project_root(project_id)
    old = (root / ".claude").resolve()
    install_core(manager, tmp_path, "0.2.0")

    assert run_child(tmp_path, mode, manager, project_id, "0.2.0") == 137
    # 停机那一刻：.claude 仍指向旧的完整装配目录，命令、守卫一个不少
    assert (root / ".claude").resolve() == old
    assert manager.packs.assembler.verify(old) == []
    if mode == "copy":
        assert list(manager.packs.assembler.root.glob(".tmp-*"))  # 留下的是临时目录，不是半个哈希目录
    assert lockfile.read(root).get("core").version == "0.2.0"

    # 「重启」：启动核对清掉临时目录，按锁文件装配并切换
    builtin.clear_cache()
    restarted = WorkspaceManager(manager.data_dir, manager.assets_dir)
    report = service.reconcile_all(restarted)
    assert project_id in report["repaired"] and not report["failed"]
    active = restarted.packs.assembler.active(root)
    assert [p["version"] for p in active["packs"]] == ["0.2.0"]
    assert restarted.packs.assembler.verify(Path(active["dir"])) == []
    assert not list(restarted.packs.assembler.root.glob(".tmp-*"))
    assert (root / ".claude" / "commands" / "ontology-new.md").is_file()
    assert restarted.packs.status(root)["state"] == "applied"


def test_killed_mid_repository_install_leaves_no_version(tmp_path: Path):
    manager = WorkspaceManager(tmp_path / "data", make_assets(tmp_path))
    manager.packs.core_version()
    source = make_pack(tmp_path / "src", "trace", "0.9.1")
    script = tmp_path / "child_install.py"
    script.write_text(textwrap.dedent(
        """
        import os, shutil, sys
        from pathlib import Path
        sys.path.insert(0, sys.argv[1])
        from app.services.packs import repository
        real = shutil.copytree
        depth = {"n": 0}
        def crash(*args, **kwargs):
            # copytree 递归复制子目录时也走模块全局名：只在最外层复制完之后停机
            depth["n"] += 1
            try:
                real(*args, **kwargs)
            finally:
                depth["n"] -= 1
            if depth["n"] == 0:
                os._exit(137)
        repository.shutil.copytree = crash
        repository.PackRepository(Path(sys.argv[2])).install(Path(sys.argv[3]), origin="upload")
        """
    ), encoding="utf-8")
    proc = run_cmd([sys.executable, str(script), str(BACKEND), str(manager.data_dir), str(source)],
                   capture_output=True, text=True, timeout=60)
    assert proc.returncode == 137
    repo = manager.packs.repository
    assert repo.versions("trace") == [] and list((repo.root / "trace").glob(".tmp-*"))
    service.reconcile_all(manager)
    assert not list((repo.root / "trace").glob(".tmp-*"))
    repo.install(source, origin="upload")
    assert repo.versions("trace") == ["0.9.1"]


# ---- 启动核对 ----


def test_reconcile_rebuilds_tampered_assembly(manager: WorkspaceManager):
    root = manager.project_root(manager.create_project("篡改")["id"])
    target = (root / ".claude").resolve()
    (target / "scripts" / "resource_write_guard.sh").write_text("exit 0\n", encoding="utf-8")
    assert manager.packs.assembler.verify(target)
    service.reconcile_all(manager)
    rebuilt = (root / ".claude").resolve()
    assert manager.packs.assembler.verify(rebuilt) == []
    assert "_guard_common.py" in (rebuilt / "scripts" / "resource_write_guard.sh").read_text(encoding="utf-8")


def test_reconcile_replaces_legacy_absolute_links_and_writes_lock(manager: WorkspaceManager):
    root = manager.project_root(manager.create_project("旧项目")["id"])
    # 模拟 P0 的存量项目：.claude 是指向资产目录的绝对链接，没有锁文件
    for name in (".claude", "CLAUDE.md", "README.md"):
        (root / name).unlink()
    (root / ".claude").symlink_to(manager.assets_dir / "workspace-toolkit")
    (root / "CLAUDE.md").symlink_to(manager.assets_dir / "workspace-rules.md")
    git(root, "rm", "-q", "--cached", ".onto/packs.lock")
    git(root, "commit", "-q", "-m", "旧项目没有锁文件")
    shutil.rmtree(root / ".onto")

    report = service.reconcile_all(manager)
    assert root.name in report["locked"]
    assert manager.packs.assembler.active(root) is not None
    assert os.readlink(root / "CLAUDE.md") == ".claude/CLAUDE.md"
    log = git(root, "log", "-1", "--format=%s%n%b")
    assert log.startswith("chore: 写入能力包锁文件") and "Onto-Source: plugin" in log
    assert git(root, "status", "--porcelain") == ""


def test_clone_keeps_locked_versions(manager: WorkspaceManager, tmp_path: Path):
    source = manager.project_root(manager.create_project("源")["id"])
    install_core(manager, tmp_path, "0.2.0")
    lock = lockfile.read(source)
    lock.upsert(manager.packs.entry_for("core", "0.2.0", follow=False))
    lockfile.write(source, lock)
    with manager._project_lock(source.name):
        service.commit_lock(manager, source.name, "chore: 锁定 core 0.2.0")
    manager.packs.apply(source)
    clone = manager.project_root(manager.clone_project(source.name, name="副本")["id"])
    assert lockfile.read(clone).get("core").version == "0.2.0"
    assert (clone / ".claude").resolve() == (source / ".claude").resolve()


def test_workspace_without_toolkit_mounts_nothing(tmp_path: Path):
    assets = tmp_path / "bare"
    (assets / "resource-template").mkdir(parents=True)
    manager = WorkspaceManager(tmp_path / "data", assets)
    root = manager.project_root(manager.create_project("无工具箱")["id"])
    assert not os.path.lexists(root / ".claude")
    assert service.reconcile_all(manager) == {"repaired": [], "locked": [], "failed": {}}
