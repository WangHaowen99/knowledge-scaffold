"""包仓库、清单、静态检查与锁文件（方案 D1；详设 5.2、8.1 第 1 步、5.9.6）。"""

from __future__ import annotations

import pytest

from app.services.packs import checks, lockfile
from app.services.packs import manifest as mf
from app.services.packs.repository import PackRepository, RepositoryError, verify_checksums
from tests.pack_helpers import make_pack


# ---- 版本号与兼容范围 ----


@pytest.mark.parametrize(
    "version, spec, ok",
    [
        ("1.0.0", ">=1.0 <2.0", True),
        ("2.0.0", ">=1.0 <2.0", False),
        ("1.4.2", ">=1.4", True),
        ("1.3.9", ">=1.4", False),
        ("1.2.5", "^1.2", True),
        ("2.0.0", "^1.2", False),
        ("0.3.1", "^0.3", True),
        ("0.4.0", "^0.3", False),
        ("1.2.9", "~1.2", True),
        ("1.3.0", "~1.2", False),
        ("1.0.0-beta.1", ">=1.0", False),
        ("0.1.0+ab12cd34", "0.1.0", True),
        ("1.0.0", "*", True),
        ("1.0.0", ">=x", False),
    ],
)
def test_version_ranges(version, spec, ok):
    assert mf.satisfies(version, spec) is ok


def test_version_precedence_ignores_build_metadata():
    assert mf.version_key("0.1.0+aaaa") == mf.version_key("0.1.0+bbbb")
    assert mf.version_key("1.0.0-rc.1") < mf.version_key("1.0.0") < mf.version_key("1.0.1")


# ---- 清单 ----


def test_manifest_rejects_bad_fields():
    with pytest.raises(mf.ManifestError) as exc:
        mf.parse({"name": "X", "version": "1", "kind": "plugin"})
    problems = "；".join(exc.value.problems)
    assert "name" in problems and "version" in problems and "kind" in problems and "kernel_api" in problems


def test_manifest_only_allows_pre_tool_use_hooks():
    data = {"name": "trace", "version": "0.9.1", "kind": "capability", "kernel_api": ">=1.0 <2.0",
            "contributes": {"hooks": {"UserPromptSubmit": [{"command": "x.sh"}]}}}
    with pytest.raises(mf.ManifestError) as exc:
        mf.parse(data)
    assert "PreToolUse" in exc.value.problems[0]


def test_manifest_hot_levels():
    base = {"version": "1.0.0", "kernel_api": ">=1.0"}
    assert mf.parse({**base, "name": "power-kb", "kind": "knowledge"}).effective == "immediate"
    assert mf.parse({**base, "name": "trace", "kind": "capability"}).effective == "next_message"
    assert mf.parse({**base, "name": "mysql", "kind": "provider"}).hot_level == "H2"


# ---- 包仓库 ----


def test_install_verifies_hash_and_is_idempotent(tmp_path):
    repo = PackRepository(tmp_path / "data")
    source = make_pack(tmp_path / "src", "trace", "0.9.1")
    meta = repo.install(source, origin="upload", by="admin")
    assert repo.has("trace", "0.9.1")
    assert meta["sha256"] == repo.digest("trace", "0.9.1")
    assert repo.verify("trace", "0.9.1") == []
    # 同版本同内容再入库：幂等
    assert repo.install(source, origin="upload")["sha256"] == meta["sha256"]


def test_install_rejects_tampered_content(tmp_path):
    repo = PackRepository(tmp_path / "data")
    source = make_pack(tmp_path / "src", "trace", "0.9.1")
    (source / "commands" / "trace-run.md").write_text("被改过\n", encoding="utf-8")
    with pytest.raises(RepositoryError) as exc:
        repo.install(source, origin="upload")
    assert exc.value.code == "pack_upload_invalid"
    assert not repo.has("trace", "0.9.1")


def test_version_directory_is_immutable(tmp_path):
    repo = PackRepository(tmp_path / "data")
    repo.install(make_pack(tmp_path / "a", "trace", "0.9.1"), origin="upload")
    other = make_pack(tmp_path / "b", "trace", "0.9.1", files={"commands/trace-run.md": "另一份内容\n"})
    with pytest.raises(RepositoryError) as exc:
        repo.install(other, origin="upload")
    assert exc.value.code == "pack_version_exists"


def test_verify_detects_modified_version_directory(tmp_path):
    repo = PackRepository(tmp_path / "data")
    repo.install(make_pack(tmp_path / "src", "trace", "0.9.1"), origin="upload")
    (repo.version_dir("trace", "0.9.1") / "commands" / "trace-run.md").write_text("篡改\n", encoding="utf-8")
    assert any("不一致" in problem for problem in repo.verify("trace", "0.9.1"))


def test_versions_order_and_channels(tmp_path):
    repo = PackRepository(tmp_path / "data")
    for version in ("0.9.0", "0.10.0", "0.9.1"):
        repo.install(make_pack(tmp_path / version, "trace", version), origin="upload")
    assert repo.versions("trace") == ["0.10.0", "0.9.1", "0.9.0"]
    # 通道没设时取最新；设了以后按指针，回滚只改指针
    assert repo.latest("trace") == "0.10.0"
    repo.set_channel("trace", "stable", "0.9.1", by="ops")
    assert repo.latest("trace") == "0.9.1"
    repo.disable_version("trace", "0.10.0", "有问题")
    with pytest.raises(RepositoryError):
        repo.set_channel("trace", "stable", "0.10.0")


def test_cleanup_staging_removes_half_installed_versions(tmp_path):
    repo = PackRepository(tmp_path / "data")
    leftover = repo.root / "trace" / ".tmp-deadbeef"
    leftover.mkdir(parents=True)
    (leftover / "manifest.yaml").write_text("name: trace\n", encoding="utf-8")
    assert repo.cleanup_staging() == [leftover]
    assert repo.versions("trace") == []


# ---- 静态检查 ----


def _result(items: list[dict], check_id: str) -> str:
    return next(item["result"] for item in items if item["id"] == check_id)


def test_clean_pack_passes(tmp_path):
    source = make_pack(tmp_path, "trace", "0.9.1", files={
        "commands/trace-run.md": "运行\n", "agents/trace-agent.md": "子代理\n", "scripts/trace_inputs.py": "print(1)\n",
    })
    manifest, items = checks.run(source, PackRepository(tmp_path / "data"), tenant=True)
    assert manifest is not None
    assert checks.summary(items)["fail"] == 0, items


@pytest.mark.parametrize("rel", ["web/index.js", "scripts/page.tsx", "ui/App.vue", "package.json", "assets/style.css"])
def test_frontend_code_fails(tmp_path, rel):
    source = make_pack(tmp_path, "trace", "0.9.1", files={"commands/trace-run.md": "运行\n", rel: "x\n"})
    _, items = checks.run(source, tenant=False)
    # ui/ 下的由 ui 检查拦（上传包只能放构建好的静态资源，ADR-0055），其余位置的前端代码由 frontend 检查拦
    assert _result(items, "ui" if rel.startswith("ui/") else "frontend") == "fail"


def test_tenant_html_with_script_fails_but_official_only_warns(tmp_path):
    files = {"commands/trace-run.md": "运行\n", "templates/report.html": "<html><script>alert(1)</script></html>\n"}
    _, tenant_items = checks.run(make_pack(tmp_path / "t", "trace", "0.9.1", files=files, tenant=True), tenant=True)
    _, official_items = checks.run(make_pack(tmp_path / "o", "trace", "0.9.1", files=files), tenant=False)
    assert _result(tenant_items, "frontend") == "fail"
    assert _result(official_items, "frontend") == "warn"


def test_compat_range_and_missing_dependency_fail(tmp_path):
    source = make_pack(tmp_path, "trace", "0.9.1", kernel_api=">=2.0", depends={"core": ">=9.0"})
    _, items = checks.run(source, PackRepository(tmp_path / "data"))
    assert _result(items, "compat") == "fail"
    detail = next(item["detail"] for item in items if item["id"] == "compat")
    assert "kernel_api" in detail and "core" in detail


def test_name_conflicts_fail(tmp_path):
    source = make_pack(tmp_path, "trace", "0.9.1", tenant=True,
                       files={"commands/ontology-validate.md": "冒名\n", "scripts/_guard_common.py": "x = 1\n"})
    core = {"commands": {"ontology-validate"}, "agents": set(), "skills": set(), "scripts": set()}
    _, items = checks.run(source, tenant=True, core_names=core)
    detail = next(item["detail"] for item in items if item["id"] == "names")
    assert _result(items, "names") == "fail"
    assert "包前缀" in detail and "重名" in detail and "内核保留名" in detail


def test_existing_version_is_a_name_conflict(tmp_path):
    repo = PackRepository(tmp_path / "data")
    repo.install(make_pack(tmp_path / "a", "trace", "0.9.1"), origin="upload")
    _, items = checks.run(make_pack(tmp_path / "b", "trace", "0.9.1"), repo)
    assert _result(items, "names") == "fail"


def test_checksum_mismatch_and_bundled_settings_fail(tmp_path):
    source = make_pack(tmp_path, "trace", "0.9.1")
    (source / "settings.json").write_text("{}", encoding="utf-8")
    _, items = checks.run(source)
    assert _result(items, "checksums") == "fail"
    assert _result(items, "hooks") == "fail"


def test_write_scope_and_entity_type_checks(tmp_path):
    files = {
        "commands/trace-run.md": "运行\n",
        "nodes/trace.yaml": "id: trace.trace\nproduces:\n  - path: resource/x.md\nentry:\n  command: /trace-run\n",
        "artifacts/obj.yaml": "type: object\npath: ontology/object/*.md\n",
    }
    source = make_pack(tmp_path, "trace", "0.9.1", files=files,
                       contributes={"nodes": ["nodes/trace.yaml"], "artifacts": ["artifacts/obj.yaml"]})
    _, items = checks.run(source)
    assert _result(items, "write_scope") == "fail"
    assert _result(items, "entity_types") == "fail"
    assert _result(items, "node_entry") == "pass"


def test_secrets_fail_without_echoing_value(tmp_path):
    fake = "sk-" + "A" * 30
    source = make_pack(tmp_path, "trace", "0.9.1", files={"commands/trace-run.md": f"key {fake}\n"})
    _, items = checks.run(source)
    item = next(item for item in items if item["id"] == "secrets")
    assert item["result"] == "fail" and fake not in item["detail"]


def test_page_must_be_builtin(tmp_path):
    source = make_pack(tmp_path, "trace", "0.9.1", contributes={"pages": [{"id": "my-dashboard"}]})
    _, items = checks.run(source)
    assert _result(items, "frontend") == "fail"


def test_unparseable_manifest_stops_checks(tmp_path):
    source = make_pack(tmp_path, "trace", "0.9.1", checksums=False)
    (source / "manifest.yaml").write_text("name: [\n", encoding="utf-8")
    manifest, items = checks.run(source)
    assert manifest is None and items[0]["result"] == "fail"


def test_checksums_cover_new_files(tmp_path):
    source = make_pack(tmp_path, "trace", "0.9.1")
    (source / "commands" / "trace-extra.md").write_text("后加\n", encoding="utf-8")
    assert any("不在校验和清单" in problem for problem in verify_checksums(source))


# ---- 锁文件 ----


def test_lock_roundtrip_and_hash(tmp_path):
    lock = lockfile.PackLock([
        lockfile.LockEntry("trace", "0.9.1", "b" * 64, enabled_at=2.0),
        lockfile.LockEntry("core", "0.1.0+aa", "a" * 64, enabled_at=1.0),
    ])
    assert lockfile.write(tmp_path, lock) is True
    assert lockfile.write(tmp_path, lock) is False  # 同内容不重写
    text = (tmp_path / ".onto" / "packs.lock").read_text(encoding="utf-8")
    assert text.index('"core"') < text.index('"trace"')  # core 永远在前
    again = lockfile.read(tmp_path)
    assert again.render() == lock.render()
    # 跟随标记、启用时间不影响锁哈希；停用的包不参与
    other = again.copy()
    other.get("trace").follow = False
    other.get("trace").enabled_at = 99.0
    assert other.hash() == lock.hash()
    other.get("trace").enabled = False
    assert other.hash() != lock.hash()


def test_lock_rejects_bad_format(tmp_path):
    (tmp_path / ".onto").mkdir()
    (tmp_path / ".onto" / "packs.lock").write_text('{"packs": [{"name": "core"}]}', encoding="utf-8")
    with pytest.raises(ValueError):
        lockfile.read(tmp_path)
    assert lockfile.read(tmp_path / "missing") is None
