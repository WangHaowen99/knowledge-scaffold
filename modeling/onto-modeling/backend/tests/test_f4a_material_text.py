"""F4a 资料的文本版（ADR-0073）：上传后台转换、状态、查看文本、重新转换、网关不可用时等待并自动补转、升级迁移、
换默认转换器、资料变化让资料分析回执失效、「多源连接器」进插件中心、健康检查。

网关是真的（app.connector_gateway，在测试进程里起 HTTP 服务、用内核的公钥验票据、每个任务一个子进程），转换器用
只依赖标准库的假转换器（ID 与官方转换器相同），不依赖转换库。
"""

import json
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.connector_gateway import registry, runner, server
from app.main import app
from app.services import auth, material_conversion, streams
from app.services.material_conversion import gateway, store
from app.services.workspace import WorkspaceManager

FAKE_PDF = """
    from app.evidence.convert import Conversion, page_marker, parse_locations, unsupported
    def convert(data, filename, limits):
        if data.startswith(b"SCAN"):
            return unsupported("扫描件或图片型 PDF，暂不支持")
        if data.startswith(b"SLOW"):
            import time; time.sleep(30)
        pages = data.decode().split("\\f")
        md = "\\n\\n".join(page_marker(n) + "\\n" + text for n, text in enumerate(pages, 1)) + "\\n"
        return Conversion(state="converted", markdown=md, locations=parse_locations(md))
"""
FAKE_PLAIN = """
    from app.evidence.convert import Conversion, ConvertError
    def convert(data, filename, limits):
        if data.startswith(b"BAD"):
            raise ConvertError("corrupt")
        return Conversion(state="converted", markdown="# " + filename + "\\n\\n" + data.decode(errors="replace"))
"""
OTHER_PDF = """
    from app.evidence.convert import Conversion
    def convert(data, filename, limits):
        return Conversion(state="converted", markdown="另一个转换器：" + data.decode())
"""


def _fake_converters(base: Path, extra: dict | None = None) -> dict:
    specs = {"convert-pdf": ([".pdf"], FAKE_PDF), "convert-docx": ([".docx"], FAKE_PLAIN),
             "convert-xlsx": ([".xlsx", ".xlsm"], FAKE_PLAIN), **(extra or {})}
    for name, (types, code) in specs.items():
        folder = base / name
        folder.mkdir(parents=True)
        (folder / "manifest.yaml").write_text(
            f"name: {name}\nversion: 1.0.0\nkind: provider\ncategory: provider\nlabel: {name}\n"
            f"kernel_api: '>=1.0 <2.0'\nengines: [claude, jiuwen]\ndepends: {{}}\n"
            f"converter: {{entry: converter.py, types: {json.dumps(types)}, license: MIT}}\n", encoding="utf-8")
        (folder / "converter.py").write_text(textwrap.dedent(code), encoding="utf-8")
    return registry.load(base)


class GatewayServer:
    """测试里的连接器网关：可以停、可以再起（同一个端口）。"""

    def __init__(self, converters: dict):
        self.converters = converters
        self.httpd = None
        self.port = 0

    def start(self):
        instance = server.Gateway(self.converters, server.KeySource(url="test", fetch=lambda: gateway.issuer().public_jwk()),
                                  job_limits=runner.JobLimits(timeout_s=3))
        self.httpd = server.serve(self.port, instance, host="127.0.0.1")
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "SECRETS_DIR", "")
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    # 后台线程不起：测试里用 drain() 在当前线程转，结果确定
    monkeypatch.setattr(material_conversion, "start", lambda: None)
    material_conversion.reset()
    gw = GatewayServer(_fake_converters(tmp_path / "converters")).start()
    monkeypatch.setenv(gateway.URL_ENV, gw.url)
    project_id = workspaces.create_project("转换", owner="employee_0001")["id"]
    streams.reset_all()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, workspaces, project_id, workspaces.project_root(project_id), gw
    gw.stop()
    material_conversion.reset()
    streams.reset_all()


def _upload(client, pid, name, data):
    response = client.post(f"/api/v1/projects/{pid}/resources", files={"file": (name, data)})
    assert response.status_code == 201, response.text
    return response.json()["material"]


def _materials(client, pid):
    groups = client.get(f"/api/v1/projects/{pid}/resources").json()["groups"]
    return {item["path"]: item for group in groups for item in group["items"]}


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def _events(pid, kind):
    return [envelope["data"] for _n, _ts, envelope in list(streams.project_channel(pid)._buffer) if envelope["type"] == kind]


def test_upload_returns_at_once_then_converts_in_background(env):
    client, _ws, pid, root, _gw = env
    material = _upload(client, pid, "退款规范.pdf", "第一页正文\f第二页正文".encode())
    assert material["text"]["state"] == "converting"
    assert not (root / "resource/text/others/退款规范.pdf.md").exists()
    assert material_conversion.drain() == 1
    text = (root / "resource/text/others/退款规范.pdf.md").read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith('<!-- onto:text source="退款规范.pdf" sha256="')
    assert 'converter="convert-pdf@1.0.0"' in text.splitlines()[0]
    assert "<!-- 第 2 页 -->\n第二页正文" in text
    # 走写入路径：单独一个提交，来源是插件；资料列表里不出现文本版
    log = _git(root, "log", "-1", "--format=%s%n%b")
    assert "转换资料 resource/others/退款规范.pdf" in log and "Onto-Source: plugin" in log
    listed = _materials(client, pid)
    assert list(listed) == ["resource/others/退款规范.pdf"]
    assert listed["resource/others/退款规范.pdf"]["text"]["state"] == "converted"
    assert listed["resource/others/退款规范.pdf"]["text"]["converter"]["id"] == "convert-pdf"
    states = [item["text"]["state"] for item in _events(pid, material_conversion.EVENT)]
    assert states[0] == "converting" and states[-1] == "converted"


def test_view_text(env):
    client, _ws, pid, _root, _gw = env
    _upload(client, pid, "a.pdf", "甲\f乙".encode())
    material_conversion.drain()
    body = client.get(f"/api/v1/projects/{pid}/resources/text", params={"path": "resource/others/a.pdf"}).json()
    assert body["text"]["state"] == "converted" and body["truncated"] is False
    assert body["markdown"] == "<!-- 第 1 页 -->\n甲\n\n<!-- 第 2 页 -->\n乙\n"
    # 文本版不当成资料来管：不能单独删、改用途、查看文本
    text_path = "resource/text/others/a.pdf.md"
    assert client.delete(f"/api/v1/projects/{pid}/resources", params={"path": text_path}).status_code == 403
    assert client.patch(f"/api/v1/projects/{pid}/resources/purpose",
                        json={"path": text_path, "purpose": "goal"}).status_code == 403
    assert client.get(f"/api/v1/projects/{pid}/resources/text", params={"path": text_path}).status_code == 403


def test_states_for_native_unsupported_scanned_and_failed(env):
    client, _ws, pid, root, _gw = env
    assert _upload(client, pid, "说明.md", "# 说明".encode())["text"]["state"] == "native"
    assert _upload(client, pid, "流程.drawio", b"<mxfile/>")["text"] is None
    image = _upload(client, pid, "照片.png", b"\x89PNG....")
    assert image["text"] == {**image["text"], "state": "unsupported", "reason": "图片暂不支持"}
    _upload(client, pid, "旧.doc", b"\xd0\xcf\x11\xe0")
    _upload(client, pid, "扫描.pdf", b"SCAN...")
    _upload(client, pid, "坏.docx", b"BAD...")
    material_conversion.drain()
    listed = _materials(client, pid)
    assert listed["resource/others/旧.doc"]["text"]["reason"] == "老版 Office 格式暂不支持"
    assert listed["resource/others/扫描.pdf"]["text"] == {**listed["resource/others/扫描.pdf"]["text"],
                                                          "state": "unsupported", "reason": "扫描件或图片型 PDF，暂不支持"}
    assert listed["resource/others/坏.docx"]["text"]["state"] == "failed"
    assert listed["resource/others/坏.docx"]["text"]["reason"] == "文件损坏，打不开"
    assert not (root / "resource/text").exists()
    # 失败的可以重试（R-14）；不是文档的不行
    assert client.post(f"/api/v1/projects/{pid}/resources/convert", json={"path": "resource/others/坏.docx"}).status_code == 202
    assert client.post(f"/api/v1/projects/{pid}/resources/convert", json={"path": "resource/others/说明.md"}).status_code == 409


def test_timeout_is_failed_not_stuck(env):
    client, _ws, pid, _root, _gw = env
    _upload(client, pid, "慢.pdf", b"SLOW")
    material_conversion.drain()
    assert _materials(client, pid)["resource/others/慢.pdf"]["text"]["reason"] == "转换超时"


def test_gateway_down_waits_then_catches_up(env):
    client, _ws, pid, root, gw = env
    gw.stop()
    gateway.reset()
    _upload(client, pid, "a.pdf", "正文".encode())
    material_conversion.drain()
    assert _materials(client, pid)["resource/others/a.pdf"]["text"]["state"] == "waiting"
    assert material_conversion.retry_waiting() == 0  # 网关还没恢复
    gw.start()
    gateway.reset()
    assert material_conversion.retry_waiting() == 1
    material_conversion.drain()
    assert _materials(client, pid)["resource/others/a.pdf"]["text"]["state"] == "converted"
    assert (root / "resource/text/others/a.pdf.md").is_file()


def test_not_configured_waits(env, monkeypatch):
    client, _ws, pid, _root, _gw = env
    monkeypatch.delenv(gateway.URL_ENV)
    material = _upload(client, pid, "a.pdf", b"x")
    assert material["text"]["state"] == "waiting"
    material_conversion.drain()
    assert _materials(client, pid)["resource/others/a.pdf"]["text"]["state"] == "waiting"


def test_replace_drops_stale_text_in_same_commit_and_delete_removes_it(env):
    client, _ws, pid, root, _gw = env
    _upload(client, pid, "a.pdf", "旧内容".encode())
    material_conversion.drain()
    _upload(client, pid, "a.pdf", "新内容".encode())
    changed = _git(root, "show", "--name-status", "--format=", "HEAD").split("\n")
    assert "D\tresource/text/others/a.pdf.md" in changed and "M\tresource/others/a.pdf" in changed
    assert _materials(client, pid)["resource/others/a.pdf"]["text"]["state"] == "converting"
    material_conversion.drain()
    assert "新内容" in (root / "resource/text/others/a.pdf.md").read_text(encoding="utf-8")
    assert client.delete(f"/api/v1/projects/{pid}/resources", params={"path": "resource/others/a.pdf"}).status_code == 200
    assert not (root / "resource/text/others/a.pdf.md").exists()


def test_reconvert_with_new_default_invalidates_research_receipt(env, tmp_path):
    """换默认转换器后「重新转换」：文本变了，资料分析回执里记的文件修订对不上，退回待核验；旧资料不自动重转。"""
    client, workspaces, pid, root, gw = env
    _upload(client, pid, "a.pdf", "正文".encode())
    material_conversion.drain()
    # 平台多了一个能转 PDF 的转换器（包仓库里一个连接器包 + 网关里一份）
    extra = _fake_converters(tmp_path / "more", {"pdf-alt": ([".pdf"], OTHER_PDF)})
    gw.converters["pdf-alt"] = extra["pdf-alt"]
    from app.services.packs.repository import write_checksums

    write_checksums(extra["pdf-alt"].directory)
    workspaces.packs.repository.install(extra["pdf-alt"].directory, origin="upload")
    workspaces.packs.repository.set_channel("pdf-alt", "stable", "1.0.0", by="test")
    gateway.reset()
    material_conversion.reset()
    view = client.get("/api/v1/converters").json()
    assert {row["type"]: row["converters"] for row in view["types"]}[".pdf"] == ["convert-pdf", "pdf-alt"]
    # 回执记下现在的文本版
    from app.services.research_view import _script

    review = _script(root, "research_review")
    text_rel = "resource/text/others/a.pdf.md"
    (root / "research").mkdir(exist_ok=True)
    (root / "research/资料分析.md").write_text("# 资料分析\n\n见 resource/text/others/a.pdf.md#L3\n", encoding="utf-8")
    review.write_review(root, review.PASSED, review.file_revisions(root, ["research/资料分析.md", text_rel]), [], [], {})
    assert review.review_status(root)["status"] == review.PASSED
    # 普通用户不能设默认（本地模式就是管理员，这里直接调设置后核对口径）
    response = client.put("/api/v1/admin/converters/defaults", json={"type": ".pdf", "converter": "pdf-alt"})
    assert response.status_code == 200
    assert {row["type"]: row["default"] for row in response.json()["types"]}[".pdf"] == "pdf-alt"
    assert client.put("/api/v1/admin/converters/defaults", json={"type": ".pdf", "converter": "nope"}).status_code == 400
    assert material_conversion.drain() == 0  # 换默认不自动重转
    assert client.post(f"/api/v1/projects/{pid}/resources/convert", json={"path": "resource/others/a.pdf"}).status_code == 202
    material_conversion.drain()
    text = (root / text_rel).read_text(encoding="utf-8")
    assert "另一个转换器：正文" in text and 'converter="pdf-alt@1.0.0"' in text
    assert review.review_status(root)["reason"] == review.REASON_INPUTS_CHANGED
    material = _materials(client, pid)["resource/others/a.pdf"]
    assert material["text"]["converter"]["id"] == "pdf-alt"


def test_upgrade_migration_converts_once(env):
    client, workspaces, pid, root, _gw = env
    # 升级前就有的资料：直接落盘提交，没经过转换
    workspaces.save_resource(pid, "others", "旧资料.pdf", "旧\f资料".encode())
    material_conversion.reset()
    store.drop(pid, "resource/others/旧资料.pdf")
    report = material_conversion.migrate(workspaces)
    assert report["result"] == "queued" and report["detail"] == "1 份资料排队转成文本"
    material_conversion.drain()
    assert (root / "resource/text/others/旧资料.pdf.md").is_file()
    assert material_conversion.migrate(workspaces)["result"] == "ok"
    # 判过失败的不再自动重试
    _upload(client, pid, "坏.docx", b"BAD")
    material_conversion.drain()
    assert material_conversion.migrate(workspaces)["result"] == "ok"


def test_restored_version_without_text_heals(env):
    client, _ws, pid, root, _gw = env
    _upload(client, pid, "a.pdf", "正文".encode())
    material_conversion.drain()
    (root / "resource/text/others/a.pdf.md").unlink()
    material = _materials(client, pid)["resource/others/a.pdf"]
    assert material["text"]["state"] == "converting"
    assert material_conversion.drain() == 1
    assert (root / "resource/text/others/a.pdf.md").is_file()


def test_catalog_lists_data_access_converters(env):
    client, _ws, pid, _root, _gw = env
    body = client.get("/api/v1/packs", params={"project_id": pid}).json()
    providers = {item["name"]: item for item in body["items"] if item["category"] == "provider"}
    assert set(providers) == {"convert-pdf", "convert-docx", "convert-xlsx"}
    assert body["counts"]["provider"] == 3
    assert providers["convert-pdf"]["project_status"] == "platform_installed"
    assert providers["convert-pdf"]["converter"]["default_for"] == [".pdf"]
    assert client.get("/api/v1/packs").json()["counts"]["provider"] == 3
    # 运维的版本清单也列转换器，标上 converter（前端在证据采集不可用时照样显示它们）
    from app.services.packs import governance

    rows = [row for row in governance.versions(_ws) if row["category"] == "provider"]
    assert {row["component"] for row in rows} == set(providers) and all(row["converter"] for row in rows)


def test_health_and_issuer_key(env, monkeypatch):
    client, _ws, _pid, _root, gw = env
    from app.governance import health

    assert health._probe_connector_gateway() == ("ok", "已加载 3 个转换器")
    key = client.get("/api/v1/connector/issuer-key").json()
    assert key["kty"] == "OKP" and key["crv"] == "Ed25519" and "d" not in key
    gw.stop()
    gateway.reset()
    assert health._probe_connector_gateway()[0] == "fail"
    monkeypatch.delenv(gateway.URL_ENV)
    gateway.reset()
    assert health._probe_connector_gateway() == ("warn", "未部署")


def test_kernel_never_imports_parsers():
    """解析库只在网关镜像里：后端进程导入全部路由后，sys.modules 里没有它们。"""
    code = ("import sys; import app.main; "
            "print([m for m in ('pdfplumber', 'pdfminer', 'docx', 'pypdfium2') if m in sys.modules])")
    out = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                         capture_output=True, text=True, timeout=120)
    assert out.stdout.strip().splitlines()[-1] == "[]", out.stderr[-500:]


def test_migration_reconverts_text_versions_without_anchors(env):
    """ADR-0093：Word、Excel 的文本版没带原件锚点的，启动迁移重转一次；带上了就不再转，PDF 不需要。"""
    from app.evidence.convert import anchor_index

    client, _ws, pid, root, _gw = env
    _upload(client, pid, "规则.docx", "合成规则".encode())
    _upload(client, pid, "手册.pdf", "第一页".encode())
    material_conversion.drain()
    text = root / "resource/text/others/规则.docx.md"
    assert material_conversion.lacks_anchors(root, "resource/others/规则.docx")
    assert not material_conversion.lacks_anchors(root, "resource/others/手册.pdf")
    report = material_conversion.migrate(_ws)
    assert report["result"] == "queued" and report["detail"] == "1 份资料排队转成文本"
    material_conversion.drain()
    text.write_text(text.read_text(encoding="utf-8") + "\n" + anchor_index([(1, "p:1")]) + "\n", encoding="utf-8")
    assert not material_conversion.lacks_anchors(root, "resource/others/规则.docx")
    assert material_conversion.migrate(_ws)["result"] == "ok"
