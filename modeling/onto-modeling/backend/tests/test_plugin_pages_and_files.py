"""插件自己的页面、上传位与下载（ADR-0103）。

用 sticky-notes 夹具（合成的上传包）加一个页面和一个上传位：页面进左栏、id 是「包名:页面 id」，打开走编辑器同一套
iframe 票据；上传位收知识包（校验同 PC-01），落在 ext/<包>/uploads/ 并提交到主线，助手的副本改不动；
下载只认插件目录里已提交的内容。数据全部合成。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
import yaml

from app.kernel.changesets import worktree
from app.services.packs import checks, files as pack_files
from tests.test_pack_ui import _result, env, sample_pack, zipped  # noqa: F401 - env 是夹具

PACK = "sticky-notes"
PAGE_KEY = f"{PACK}:board"


def _with_page(base: Path, *, page: dict | None = None, uploads: list | None = None) -> Path:
    """夹具包加一个自己的页面和一个上传位（清单就地改写，再补校验和）。"""
    from app.services.packs.repository import write_checksums

    target = sample_pack(base)
    manifest_path = target / "manifest.yaml"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data["page_api"] = ">=1.1 <2.0"
    data["contributes"]["pages"] = [page or {"id": "board", "nav": {"title": "便签板", "icon": "book", "order": 2},
                                             "ui": "note"}]
    data["contributes"]["uploads"] = uploads if uploads is not None else [
        {"id": "inputs", "title": "输入知识包", "kind": "knowledge-package", "max": 2}]
    manifest_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    write_checksums(target)
    return target


def _publish_and_enable(client, tmp_path: Path, project_id: str) -> None:
    accepted = client.post("/api/v1/admin/packs/uploads",
                           files={"file": ("pack.zip", zipped(_with_page(tmp_path / "src")), "application/zip")})
    assert accepted.status_code == 202, accepted.text
    upload_id = accepted.json()["id"]
    detail = client.get(f"/api/v1/admin/packs/uploads/{upload_id}").json()
    assert detail["state"] == "passed", detail["checks"]
    assert client.post(f"/api/v1/admin/packs/uploads/{upload_id}/publish", json={"channel": "stable"}).status_code == 200
    enabled = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": PACK})
    assert enabled.status_code == 201, enabled.text


def knowledge_package(package_id: str, *, version: str = "1.0.0", objects: dict[str, str] | None = None,
                      extra: dict[str, str] | None = None) -> bytes:
    """合成的知识包 ZIP（manifest 照 PC-01 的严格校验）。"""
    manifest = {"kind": "ontology-package", "schema_version": "0.1", "id": package_id, "version": version,
                "name": f"合成包 {package_id}", "description": "合成数据"}
    members = {"manifest.yaml": yaml.safe_dump(manifest, allow_unicode=True), "index.md": "# 索引\n",
               "objects/index.md": "# 对象\n", "rules/r1.md": "# 规则一\n", "mappings/object-data-mappings.json": "{}"}
    for key, body in (objects or {"会员": "# 会员\n"}).items():
        members[f"objects/{key}.md"] = body
    members.update(extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in members.items():
            zf.writestr(f"pkg/{name}", text)
    return buffer.getvalue()


def _put(client, project_id: str, data: bytes, name: str = "package.zip"):
    return client.post(f"/api/v1/projects/{project_id}/packs/{PACK}/uploads/inputs",
                       files={"file": (name, data, "application/zip")})


# ---- 静态检查 ----


def test_uploaded_pack_may_declare_its_own_page(tmp_path):
    manifest, items = checks.run(_with_page(tmp_path / "ok"), tenant=True)
    assert manifest is not None and checks.summary(items)["fail"] == 0, items
    assert _result(items, "frontend")[0] == "pass"
    assert manifest.summary()["contributes"]["uploads"] == ["inputs"]


@pytest.mark.parametrize("page, expected", [
    ({"id": "board"}, "要用 ui 指向本包的编辑器"),
    ({"id": "board", "ui": "nope"}, "没有在 contributes.editors 里声明"),
    ({"id": "Board!", "ui": "note"}, "页面 id 不合法"),
])
def test_custom_page_must_point_at_a_declared_editor(tmp_path, page, expected):
    _manifest, items = checks.run(_with_page(tmp_path / "bad", page=page), tenant=True)
    result, detail = _result(items, "frontend")
    assert result == "fail" and expected in detail


def test_upload_slot_declaration_is_validated(tmp_path):
    from app.services.packs import manifest as mf

    with pytest.raises(mf.ManifestError) as caught:
        mf.parse({"name": "x-pack", "version": "0.1.0", "kind": "capability", "kernel_api": ">=1.0",
                  "contributes": {"uploads": [{"id": "inputs", "kind": "anything"}, {"id": "b", "kind": "knowledge-package",
                                                                                     "max": 99}]}})
    text = "；".join(caught.value.problems)
    assert "kind 只能是 knowledge-package" in text and "max 要是 1～20 的整数" in text


# ---- 左栏与页面 ----


def test_custom_page_joins_the_nav_and_opens_like_an_editor(env):
    client, _, project_id, tmp_path = env
    _publish_and_enable(client, tmp_path, project_id)
    ext = client.get(f"/api/v1/projects/{project_id}/extensions").json()
    nav = next(item for item in ext["nav"] if item["id"] == PAGE_KEY)
    assert nav["kind"] == "plugin" and nav["title"] == "便签板" and nav["pack"] == PACK
    page = next(item for item in ext["pages"] if item["id"] == PAGE_KEY)
    assert page["page"] == "board" and page["ui"] == "note"
    opened = client.post(f"/api/v1/projects/{project_id}/packs/{PACK}/ui-sessions", json={"editor": "note"})
    assert opened.status_code == 201, opened.text
    body = opened.json()
    assert body["ui_api"] == "1.1"
    assert body["uploads"] == [{"id": "inputs", "title": "输入知识包", "kind": "knowledge-package", "max": 2}]
    assert "sticky-notes.note" in body["types"]
    # 停用后页面离开左栏，旧链接按停用的页面显示
    assert client.post(f"/api/v1/projects/{project_id}/packs/{PACK}/disable").status_code == 200
    ext = client.get(f"/api/v1/projects/{project_id}/extensions").json()
    assert all(item["id"] != PAGE_KEY for item in ext["nav"])
    assert any(item["id"] == PAGE_KEY for item in ext["disabled_pages"])


# ---- 上传位 ----


def _git_files(root: Path, rel: str) -> list[str]:
    from tests import proc

    out = proc.run(["git", "-c", "core.quotePath=false", "ls-files", "--", rel], cwd=root, capture_output=True,
                   text=True).stdout
    return sorted(line for line in out.splitlines() if line)


def test_knowledge_package_upload_keeps_ontology_text_and_commits(env):
    client, manager, project_id, tmp_path = env
    _publish_and_enable(client, tmp_path, project_id)
    created = _put(client, project_id, knowledge_package("lib.lending"))
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["package"] == {"id": "lib.lending", "name": "合成包 lib.lending", "version": "1.0.0"}
    assert item["counts"]["objects"] == 1 and item["counts"]["rules"] == 1 and item["files"] == 3
    root = manager.project_root(project_id)
    base = f"ext/{PACK}/uploads/inputs/{item['id']}"
    # 只留本体文本：不要 index.md、mappings/；提交到了主线
    assert _git_files(root, base) == [f"{base}/manifest.yaml", f"{base}/objects/会员.md", f"{base}/rules/r1.md",
                                      f"{base}/upload.json"]
    listed = client.get(f"/api/v1/projects/{project_id}/packs/{PACK}/uploads/inputs").json()["items"]
    assert [entry["id"] for entry in listed] == [item["id"]]

    # 同一个包重传就替换：旧文件删掉
    again = _put(client, project_id, knowledge_package("lib.lending", version="1.1.0", objects={"读者": "# 读者\n"}))
    assert again.status_code == 201 and again.json()["id"] == item["id"]
    assert f"{base}/objects/会员.md" not in _git_files(root, base)
    assert f"{base}/objects/读者.md" in _git_files(root, base)

    # 上传位最多 2 个
    assert _put(client, project_id, knowledge_package("tool.lending")).status_code == 201
    full = _put(client, project_id, knowledge_package("room.booking"))
    assert full.status_code == 409 and full.json()["code"] == "upload_slot_full"

    # 删除
    assert client.delete(f"/api/v1/projects/{project_id}/packs/{PACK}/uploads/inputs/{item['id']}").status_code == 200
    assert _git_files(root, base) == []
    gone = client.delete(f"/api/v1/projects/{project_id}/packs/{PACK}/uploads/inputs/{item['id']}")
    assert gone.status_code == 404


@pytest.mark.parametrize("data, code", [
    (b"not a zip", 400),
    (b"", 400),
])
def test_bad_uploads_are_refused(env, data, code):
    client, _, project_id, tmp_path = env
    _publish_and_enable(client, tmp_path, project_id)
    assert _put(client, project_id, data).status_code == code


def test_upload_rejects_unsafe_and_non_package_archives(env):
    client, _, project_id, tmp_path = env
    _publish_and_enable(client, tmp_path, project_id)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("../evil.md", "x")
    assert _put(client, project_id, buffer.getvalue()).status_code == 400
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("manifest.yaml", "kind: something-else\n")
        zf.writestr("objects/a.md", "# a\n")
    assert _put(client, project_id, buffer.getvalue()).status_code == 400


def test_unknown_slot_and_disabled_pack(env):
    client, _, project_id, tmp_path = env
    _publish_and_enable(client, tmp_path, project_id)
    missing = client.post(f"/api/v1/projects/{project_id}/packs/{PACK}/uploads/nope",
                          files={"file": ("p.zip", knowledge_package("a.b"), "application/zip")})
    assert missing.status_code == 404
    other = client.get(f"/api/v1/projects/{project_id}/packs/nope/uploads/inputs")
    assert other.status_code == 409 and other.json()["code"] == "pack_not_enabled"


def test_assistant_copy_cannot_change_upload_dirs():
    assert worktree.is_protected("ext/sticky-notes/uploads/inputs/a/objects/x.md")
    assert worktree.is_protected("resource/a.md")
    assert not worktree.is_protected("ext/sticky-notes/notes/a.md")
    assert not worktree.is_protected("ext/uploads/a.md")
    assert pack_files.is_upload_path("ext/p/uploads/s/x")


# ---- 下载 ----


def test_download_only_reads_committed_plugin_files(env):
    client, manager, project_id, tmp_path = env
    _publish_and_enable(client, tmp_path, project_id)
    item = _put(client, project_id, knowledge_package("lib.lending")).json()
    url = f"/api/v1/projects/{project_id}/packs/{PACK}/files"
    one = client.get(url, params={"path": f"uploads/inputs/{item['id']}/upload.json"})
    assert one.status_code == 200 and "attachment" in one.headers["content-disposition"]
    assert b"lib.lending" in one.content
    whole = client.get(url, params={"path": "uploads"})
    assert whole.status_code == 200 and whole.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(whole.content)) as zf:
        assert f"uploads/inputs/{item['id']}/objects/会员.md" in zf.namelist()
    # 没提交的文件不给；路径越界、隐藏段都拒绝
    root = manager.project_root(project_id)
    (root / "ext" / PACK / "draft.md").write_text("草稿", encoding="utf-8")
    assert client.get(url, params={"path": "draft.md"}).status_code == 404
    for bad in ("../../.git/config", "a/../b", ".hidden", "/etc/passwd"):
        assert client.get(url, params={"path": bad}).status_code in (400, 404), bad
