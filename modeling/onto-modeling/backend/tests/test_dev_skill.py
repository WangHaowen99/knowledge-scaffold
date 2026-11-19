"""插件开发 skill（ADR-0081，契约 PL-27）：下载、离线检查和服务器结论一致、做出来的包第一次上传就过。

包名、用户名、内容均为合成数据。
"""

from __future__ import annotations

import copy
import io
import json
import re
import runpy
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

from app import config
from app.kernel.artifacts import registry as artifact_registry
from app.services.flow import need, view
from app.services.flow.state import KIND_DIRS
from app.services.onto_mcp.definitions import TOOLS
from app.services.packs import checks, dev_skill
from app.services.packs import manifest as mf
from app.services.packs import service as pack_service
from tests.pack_helpers import NODE_PACK_FLOW, make_node_pack, make_pack
from tests.proc import run as run_cmd
from tests.test_private_packs import (AS_OTHER, AS_OWNER, MINE, OWNER, install, post_zip, project,  # noqa: F401
                                       real_flow_env, zipped)

CATEGORIES = ("capability", "industry", "system", "provider")
URL = "/api/v1/packs/dev-skills/{}"


def skill_zip(client, category: str, headers=None) -> bytes:
    res = client.get(URL.format(category), headers=AS_OWNER if headers is None else headers)
    assert res.status_code == 200, res.text
    return res.content


def skill_dir(client, tmp_path: Path, category: str) -> Path:
    dest = tmp_path / f"skill-{category}"
    with zipfile.ZipFile(io.BytesIO(skill_zip(client, category))) as archive:
        archive.extractall(dest)
    return dest / f"onto-plugin-dev-{category}"


def pack_tool(skill: Path, *args, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    # -I：不带 PYTHONPATH 和当前目录，确认只靠 skill 自己的文件就能跑
    return run_cmd([sys.executable, "-I", str(skill / "scripts" / "pack_tool.py"), *map(str, args)],
                   capture_output=True, text=True, cwd=cwd, env=env, timeout=60)


def tool_check(skill: Path, directory: Path, *flags) -> tuple[int, dict]:
    done = pack_tool(skill, "check", directory, "--json", *flags, cwd=directory.parent)
    assert done.stdout, done.stderr
    return done.returncode, json.loads(done.stdout)


def server_results(manager, directory: Path) -> dict[str, str]:
    """和 UploadStore.run_checks 同样的调用：普通用户上传按租户包查。"""
    repository = manager.packs.repository
    stable = repository.latest("core", "stable")
    core_names = checks.declared_names(repository.manifest("core", stable), repository.version_dir("core", stable))
    _manifest, items = checks.run(directory, repository, tenant=True, core_names=core_names)
    return {item["id"]: item["result"] for item in items}


def tool_results(report: dict) -> dict[str, str]:
    """和服务器 checks.run 对得上的那些项：limits、private_name 是上传流程查的，不在 checks.run 里。"""
    return {item["id"]: item["result"] for item in report["checks"] if item["id"] not in ("limits", "private_name")}


def fails(results: dict[str, str]) -> set[str]:
    return {key for key, result in results.items() if result == "fail"}


def upload_time_ids(report: dict) -> set[str]:
    return {item["id"] for item in report["upload_time"]}


# ---------- 下载 ----------


def test_login_required_and_download_link_token(real_flow_env):
    client, _manager, _tmp = real_flow_env
    url = URL.format("capability")
    assert client.get(url).status_code == 401
    assert client.get(f"{url}?token=bad-token").status_code == 401
    # 浏览器的下载链接带不了头，前端走 ?token=
    assert client.get(f"{url}?token=owner-token").status_code == 200


@pytest.mark.parametrize("category", CATEGORIES)
def test_each_category_downloads_a_complete_skill(real_flow_env, category):
    client, _manager, _tmp = real_flow_env
    res = client.get(URL.format(category), headers=AS_OWNER)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "application/zip"
    assert f'filename="onto-plugin-dev-{category}.zip"' in res.headers["content-disposition"]
    assert len(res.content) < 512 * 1024  # 现在的公开包数量下；最坏大小见 dev_skill.py 开头
    top = f"onto-plugin-dev-{category}"
    with zipfile.ZipFile(io.BytesIO(res.content)) as archive:
        names = set(archive.namelist())
        assert all(name.startswith(f"{top}/") for name in names)
        for rel in ("SKILL.md", "platform.json", "references/protocol.md", f"references/{category}.md",
                    "scripts/pack_tool.py", "scripts/checker/services/packs/checks.py"):
            assert f"{top}/{rel}" in names
        assert any(name.startswith(f"{top}/references/examples/") and name.endswith("/manifest.yaml") for name in names)
        skill_md = archive.read(f"{top}/SKILL.md").decode("utf-8")
        snapshot = json.loads(archive.read(f"{top}/platform.json"))
    front = yaml.safe_load(skill_md.split("---")[1])
    assert front["name"] == top and "时使用" in front["description"]
    assert "{{" not in skill_md
    for phrase in ("pack_tool.py init", "pack_tool.py build", "安装插件", "启用到项目", f"references/{category}.md"):
        assert phrase in skill_md
    assert snapshot["kernel_api"] == mf.KERNEL_API and snapshot["category"] == category
    assert snapshot["limits"] == {"zip_bytes": 20 * 1024 * 1024, "extracted_bytes": 64 * 1024 * 1024, "files": 2000}
    assert "core" in snapshot["packs"] and snapshot["core"]["names"]["commands"]


def test_provider_skill_says_admin_must_trust_the_connector(real_flow_env):
    """多源连接器上传后要管理员信任才运行（ADR-0103），连接器包的 skill 要原样交代。"""
    client, _manager, tmp_path = real_flow_env
    assert "「信任」才运行" in (skill_dir(client, tmp_path, "provider") / "SKILL.md").read_text(encoding="utf-8")
    assert "「信任」才运行" not in (skill_dir(client, tmp_path, "capability") / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("category", ["exporter", "node", "nope", "core"])
def test_categories_without_a_skill_are_404(real_flow_env, category):
    client, _manager, _tmp = real_flow_env
    res = client.get(URL.format(category), headers=AS_OWNER)
    assert res.status_code == 404 and res.json()["code"] == "not_found"


def test_no_private_pack_names_in_the_zip(real_flow_env):
    client, _manager, tmp_path = real_flow_env
    secret = "theirs-only-pack"
    project(client)
    install(client, tmp_path, secret, headers=AS_OTHER, tenant=True)
    for category in CATEGORIES:
        data = skill_zip(client, category)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                assert secret not in name
                assert secret not in archive.read(name).decode("utf-8", errors="ignore"), name
    snapshot = json.loads((skill_dir(client, tmp_path, "capability") / "platform.json").read_text(encoding="utf-8"))
    assert secret not in snapshot["packs"]


def test_snapshot_is_bounded_and_always_keeps_core(real_flow_env, monkeypatch):
    _client, manager, _tmp = real_flow_env
    manager.packs.core_version()
    assert manager.packs.repository.names()[0] != "core", "要有排在 core 前面的包，这条才查得到"
    monkeypatch.setattr(dev_skill, "SNAPSHOT_MAX_PACKS", 1)
    monkeypatch.setattr(dev_skill, "SNAPSHOT_MAX_NAMES", 2)
    snapshot = dev_skill.platform_snapshot(manager, "capability")
    assert list(snapshot["packs"]) == ["core"]
    assert len(snapshot["packs"]["core"]["commands"]) == 2
    assert len(snapshot["core"]["names"]["commands"]) > 2, "和基础建模能力撞名要查全，这一份不截"


def test_checker_is_the_server_code_and_snapshots_match(real_flow_env):
    client, _manager, tmp_path = real_flow_env
    skill = skill_dir(client, tmp_path, "capability")
    app_dir = Path(dev_skill.__file__).resolve().parents[2]
    for rel in dev_skill.CHECKER_SOURCES:
        assert (skill / "scripts" / "checker" / rel).read_bytes() == (app_dir / rel).read_bytes(), rel
    assert runpy.run_path(str(skill / "scripts/checker/services/flow/state.py"))["KIND_DIRS"] == KIND_DIRS
    assert set(runpy.run_path(str(skill / "scripts/checker/services/onto_mcp/definitions.py"))["TOOLS"]) == set(TOOLS)


def test_protocol_comes_from_backend_constants(real_flow_env):
    client, _manager, tmp_path = real_flow_env
    text = (skill_dir(client, tmp_path, "capability") / "references" / "protocol.md").read_text(encoding="utf-8")
    for values in (mf.KINDS, mf.CATEGORIES, mf.LIST_FIELDS, mf.NAV_ICONS, mf.BUILTIN_PAGES, mf.PROTECTED_DIRS,
                   (need.REQUIRED, need.OPTIONAL), need.LEGACY_DROPPED, checks.RESERVED_PACK_NAMES,
                   artifact_registry.FORMATS, artifact_registry.FIELD_TYPES, ("object", "released")):
        for value in values:
            assert f"`{value}`" in text, value
    assert f"| 内核接口 | `{mf.KERNEL_API}` |" in text and f"| 平台页面接口 | `{mf.PAGE_API}` |" in text
    assert f"| 流程定义格式 | `{mf.FLOW_API}` |" in text and "≤ 2000 个文件" in text
    # 服务器报告里出现的检查项，协议文档都要讲到
    source = Path(checks.__file__).read_text(encoding="utf-8")
    for check_id in set(re.findall(r'_check\(\s*"([a-z_]+)"', source)):
        assert f"`{check_id}`" in text, check_id


def test_assets_outside_packs_are_not_imported_as_packs(real_flow_env):
    client, manager, _tmp = real_flow_env
    project(client)
    names = manager.packs.repository.names(private=True)
    assert not [name for name in names if name.startswith("sample-") or name == dev_skill.ASSET_DIR]


def test_static_assets_only_use_reserved_addresses():
    root = Path(config.ASSETS_DIR) / dev_skill.ASSET_DIR
    reserved_hosts = ("example.com", "example.org", "example.net")
    reserved_ips = ("192.0.2.", "198.51.100.", "203.0.113.", "127.0.0.1", "0.0.0.0")
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for url in re.findall(r"[a-z][a-z0-9+.-]*://[^\s\"'`<>)]+", text):
            host = url.split("://", 1)[1].split("/")[0].split(":")[0]
            assert host == "localhost" or host.endswith(reserved_hosts), f"{path}: {url}"
        for ip in re.findall(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])", text):
            assert ip.startswith(reserved_ips), f"{path}: {ip}"
        assert not checks._secrets(path.parent, [path.name]), path


def test_examples_are_tenant_packs_at_first_version_without_checksums():
    root = Path(config.ASSETS_DIR) / dev_skill.ASSET_DIR / "examples"
    manifests = list(root.glob("*/*/manifest.yaml"))
    assert len(manifests) == 6  # 含数据源连接器样例 sample-source（ADR-0103）
    for path in manifests:
        parsed = mf.parse(yaml.safe_load(path.read_text(encoding="utf-8")))
        assert parsed.publisher["kind"] == "tenant" and parsed.version == "0.1.0", path
        assert not (path.parent / mf.CHECKSUMS_FILE).exists(), path


# ---------- 离线和服务器结论一致 ----------


def _flow(**node_fields) -> dict:
    flow = copy.deepcopy(NODE_PACK_FLOW)
    flow["nodes"][0].update(node_fields)
    return flow


def _tamper(directory: Path) -> Path:
    (directory / "commands" / "my-pack-run.md").write_text("改过\n", encoding="utf-8")
    return directory


def _symlink(directory: Path) -> Path:
    (directory / "docs-link.md").symlink_to(directory / "manifest.yaml")
    return directory


def _plain(base, **kw):
    return make_pack(base, "my-pack", "0.1.0", tenant=True, **kw)


RUN = {"commands/my-pack-run.md": "跑 $ARGUMENTS\n"}
# 用例名 → （造包, 应该失败的检查项）。凭据拼出来写，仓库里不留长得像凭据的字面量
CASES = {
    "合格": (lambda b: _plain(b), set()),
    "带依赖的公开包": (lambda b: _plain(b, depends={"core": ">=0.1"}), set()),
    "没声明的脚本只给警告": (lambda b: _plain(b, files={**RUN, "scripts/tool.py": "print(1)\n"}, contributes={"scripts": []}), set()),
    "清单不合法": (lambda b: _plain(b, kernel_api="不是范围"), {"manifest"}),
    "缺校验和": (lambda b: _plain(b, checksums=False), {"checksums"}),
    "校验和对不上": (lambda b: _tamper(_plain(b, files=RUN)), {"checksums"}),
    "符号链接": (lambda b: _symlink(_plain(b)), {"checksums", "symlinks"}),
    "声明的文件不存在": (lambda b: _plain(b, contributes={"scripts": ["gone.py"]}), {"declared"}),
    "接口版本不满足": (lambda b: _plain(b, kernel_api=">=9.0"), {"compat"}),
    "公开依赖版本不满足": (lambda b: _plain(b, depends={"core": ">=99.0"}), {"compat"}),
    "命令没有包前缀": (lambda b: _plain(b, files={"commands/other-run.md": "跑\n"}), {"names"}),
    "和基础建模能力的命令重名": (lambda b: _plain(b, files={"commands/ontology.md": "跑\n"}), {"names"}),
    "保留包名": (lambda b: make_pack(b, "ontology", "0.1.0", tenant=True, files={"commands/ontology-x.md": "跑\n"}), {"names"}),
    "脚本占内核保留名": (lambda b: _plain(b, files={**RUN, "scripts/my_guard.sh": "exit 0\n"}), {"names"}),
    "Python 语法错误": (lambda b: _plain(b, files={**RUN, "scripts/bad.py": "def (:\n"}), {"syntax"}),
    "Shell 语法错误": (lambda b: _plain(b, files={**RUN, "scripts/bad.sh": "if then\n"}), {"syntax"}),
    "ui 之外的前端代码": (lambda b: _plain(b, files={**RUN, "web/app.js": "1\n"}), {"frontend"}),
    "页面不是平台内置": (lambda b: _plain(b, contributes={"pages": [{"id": "mine"}]}), {"frontend"}),
    "图标不在图标集": (lambda b: _plain(b, contributes={"pages": [{"id": "scenarios", "nav": {"icon": "nope"}}]}), {"frontend"}),
    "ui 里有源码": (lambda b: _plain(b, files={**RUN, "ui/mine/index.html": "<p>合成</p>\n", "ui/mine/app.py": "1\n"},
                                contributes={"editors": [{"id": "mine"}]}), {"ui"}),
    "带 settings.json": (lambda b: _plain(b, files={**RUN, "settings.json": "{}\n"}), {"hooks"}),
    "凭据": (lambda b: _plain(b, files={**RUN, "reference/note.md": "pass" + 'word = "' + "q1" * 6 + '"\n'}), {"secrets"}),
    "扩展项格式错": (lambda b: _plain(b, contributes={"metrics": [{"name": "Bad-Name"}]}), {"extensions"}),
    "提示词超预算": (lambda b: _plain(b, files={"commands/my-pack-run.md": "字" * (checks.PROMPT_HARD_BYTES // 3 + 1)}), {"prompt_budget"}),
    "写入范围碰受保护目录": (lambda b: make_node_pack(b, flow=_flow(produces=[{"path": ".onto/flow.lock"}]), tenant=True), {"write_scope"}),
    "写入范围碰资料目录": (lambda b: make_node_pack(b, flow=_flow(writes=["resource/x/*"]), tenant=True), {"write_scope"}),
    "节点主命令不存在": (lambda b: make_node_pack(b, flow=_flow(actions=[{"label": "运行", "command": "/naming-gone"}]), tenant=True), {"node_entry", "node_pack"}),
    "节点包合格": (lambda b: make_node_pack(b, tenant=True), set()),
    "节点新增产物类别": (lambda b: make_node_pack(b, flow=_flow(stale_on=["mine"]), tenant=True), {"node_pack"}),
    "节点探针没声明": (lambda b: make_node_pack(b, flow=_flow(done=["nope"]), tenant=True), {"node_pack"}),
    "节点用基础能力的探针": (lambda b: make_node_pack(b, flow=_flow(done=["object"]), tenant=True), set()),
    "节点挂法不对": (lambda b: make_node_pack(b, flow=_flow(placement="main"), tenant=True), {"node_need"}),
    "节点 need 写错": (lambda b: make_node_pack(b, flow=_flow(need="sometimes"), tenant=True), {"node_need"}),
    "节点要的基础能力版本没有": (lambda b: make_node_pack(b, flow={**_flow(), "requires_flow": "core>=99"}, tenant=True), {"node_pack"}),
}


@pytest.mark.parametrize("case", list(CASES))
def test_offline_check_reaches_the_same_conclusion_as_the_server(real_flow_env, case):
    client, manager, tmp_path = real_flow_env
    builder, expected = CASES[case]
    directory = builder(tmp_path / "src")
    skill = skill_dir(client, tmp_path, "capability")
    server = server_results(manager, directory)
    code, report = tool_check(skill, directory, "--verify-only")
    assert tool_results(report) == server
    assert report["ok"] is (not fails(server)) and code == (1 if fails(server) else 0)
    assert fails(server) == expected, server


def test_check_writes_checksums_then_the_server_agrees(real_flow_env):
    client, manager, tmp_path = real_flow_env
    directory = make_pack(tmp_path / "src", "my-pack", "0.1.0", tenant=True, checksums=False)
    skill = skill_dir(client, tmp_path, "capability")
    assert "checksums" in fails(server_results(manager, directory))
    code, report = tool_check(skill, directory)
    assert code == 0 and report["ok"]
    assert not fails(server_results(manager, directory))
    (directory / "commands" / "my-pack-run.md").write_text("又改了\n", encoding="utf-8")
    assert tool_check(skill, directory)[0] == 0 and not fails(server_results(manager, directory))


def test_public_pack_name_is_rejected_offline_like_the_server(real_flow_env):
    client, manager, tmp_path = real_flow_env
    skill = skill_dir(client, tmp_path, "capability")
    name = next(name for name in json.loads((skill / "platform.json").read_text(encoding="utf-8"))["packs"] if name != "core")
    directory = make_pack(tmp_path / "src", name, "9.9.9", tenant=True, files={f"commands/{name}-mine.md": "跑\n"})
    assert not manager.packs.repository.name_free(name, OWNER)
    code, report = tool_check(skill, directory)
    assert code == 1 and "private_name" in fails({item["id"]: item["result"] for item in report["checks"]})
    assert "private_name" not in upload_time_ids(report)


# ---------- 离线判不了的：不假装通过，单独列出来 ----------


def test_name_taken_by_someone_elses_private_pack_is_upload_time(real_flow_env):
    client, manager, tmp_path = real_flow_env
    project(client)
    install(client, tmp_path, "shared-name", headers=AS_OTHER, tenant=True)
    directory = make_pack(tmp_path / "mine", "shared-name", "0.2.0", tenant=True)
    code, report = tool_check(skill_dir(client, tmp_path, "capability"), directory)
    assert code == 0 and "private_name" in upload_time_ids(report)
    accepted = post_zip(client, MINE, zipped(directory), AS_OWNER)
    detail = client.get(f"{MINE}/{accepted.json()['id']}", headers=AS_OWNER).json()
    assert detail["state"] == "failed"
    assert {item["id"]: item["result"] for item in detail["checks"]}["private_name"] == "fail"


def test_same_version_already_installed_is_upload_time(real_flow_env):
    client, manager, tmp_path = real_flow_env
    project(client)
    install(client, tmp_path, "my-notes", tenant=True)
    directory = make_pack(tmp_path / "again", "my-notes", "0.1.0", tenant=True)
    code, report = tool_check(skill_dir(client, tmp_path, "capability"), directory)
    assert code == 0 and "same_version" in upload_time_ids(report)
    assert "names" in fails(server_results(manager, directory))


def test_dependency_outside_public_packs_is_upload_time(real_flow_env):
    client, manager, tmp_path = real_flow_env
    skill = skill_dir(client, tmp_path, "capability")
    mixed = make_pack(tmp_path / "mixed", "my-pack", "0.1.0", tenant=True, depends={"my-base": ">=0.1", "core": ">=99.0"})
    code, report = tool_check(skill, mixed)
    compat = next(item for item in report["checks"] if item["id"] == "compat")
    assert code == 1 and compat["result"] == "fail"
    assert "core" in compat["detail"] and "my-base" not in compat["detail"], "公开包照常判，不在快照里的挪走"
    assert "depends_private" in upload_time_ids(report)

    # 只依赖快照外的包：离线放行并列出来；服务器看开发者有没有装过这个包
    private_only = make_pack(tmp_path / "private", "my-pack", "0.1.0", tenant=True, depends={"my-base": ">=0.1"})
    code, report = tool_check(skill, private_only)
    assert code == 0 and "depends_private" in upload_time_ids(report)
    assert "compat" in fails(server_results(manager, private_only))
    install(client, tmp_path, "my-base", tenant=True)
    assert "compat" not in fails(server_results(manager, private_only))


def test_python_version_difference_is_upload_time(real_flow_env):
    client, _manager, tmp_path = real_flow_env
    skill = skill_dir(client, tmp_path, "capability")
    directory = make_pack(tmp_path / "src", "my-pack", "0.1.0", tenant=True, files={**RUN, "scripts/run.py": "print(1)\n"})
    assert "python_syntax" not in upload_time_ids(tool_check(skill, directory)[1])
    platform = json.loads((skill / "platform.json").read_text(encoding="utf-8"))
    platform["python"] = "2.7"
    (skill / "platform.json").write_text(json.dumps(platform), encoding="utf-8")
    assert "python_syntax" in upload_time_ids(tool_check(skill, directory)[1])


def test_no_bash_locally_is_upload_time_not_a_pass(real_flow_env):
    client, manager, tmp_path = real_flow_env
    skill = skill_dir(client, tmp_path, "capability")
    directory = make_pack(tmp_path / "src", "my-pack", "0.1.0", tenant=True, files={**RUN, "scripts/bad.sh": "if then\n"})
    assert "syntax" in fails(server_results(manager, directory))
    done = pack_tool(skill, "check", directory, "--json", cwd=tmp_path, env={"PATH": str(tmp_path / "empty")})
    report = json.loads(done.stdout)
    assert report["ok"] and "shell_syntax" in upload_time_ids(report)


# ---------- pack_tool 本身 ----------


def test_checker_runs_without_the_backend_on_the_path(real_flow_env):
    client, _manager, tmp_path = real_flow_env
    skill = skill_dir(client, tmp_path, "capability")
    directory = make_node_pack(tmp_path / "src", tenant=True, contributes={"metrics": [{"name": "good_one"}]})
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(skill / 'scripts')!r})\n"
        "import pack_tool\n"
        f"platform = json.load(open({str(skill / 'platform.json')!r}, encoding='utf-8'))\n"
        "from pathlib import Path\n"
        f"pack_tool.run_checks(Path({str(directory)!r}), platform, write_checksums=True)\n"
        "print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))\n"
    )
    done = run_cmd([sys.executable, "-I", "-c", code], capture_output=True, text=True, cwd=tmp_path, timeout=60)
    assert done.returncode == 0, done.stderr
    roots = set(json.loads(done.stdout))
    assert "checker" in roots and not roots & {"app", "fastapi", "starlette", "pydantic"}


def test_missing_pyyaml_gives_a_clear_message(real_flow_env):
    client, _manager, tmp_path = real_flow_env
    skill = skill_dir(client, tmp_path, "capability")
    code = (
        "import runpy, sys\n"
        "sys.modules['yaml'] = None\n"
        f"sys.argv = ['pack_tool.py', 'init', 'my-pack']\n"
        f"runpy.run_path({str(skill / 'scripts' / 'pack_tool.py')!r}, run_name='__main__')\n"
    )
    done = run_cmd([sys.executable, "-I", "-c", code], capture_output=True, text=True, cwd=tmp_path, timeout=60)
    assert done.returncode == 1 and "PyYAML" in done.stderr and "Traceback" not in done.stderr
    assert not (tmp_path / "my-pack").exists()


def test_init_refuses_bad_names_and_kinds(real_flow_env):
    client, _manager, tmp_path = real_flow_env
    skill = skill_dir(client, tmp_path, "capability")
    for args in (["Bad_Name"], ["ontology"], ["core"], ["my-pack", "--kind", "provider"]):
        done = pack_tool(skill, "init", *args, cwd=tmp_path)
        assert done.returncode == 1 and done.stderr, args
    assert pack_tool(skill, "init", "my-pack", cwd=tmp_path).returncode == 0
    done = pack_tool(skill, "init", "my-pack", cwd=tmp_path)
    assert done.returncode == 1 and "已存在" in done.stderr


def test_init_leaves_no_sample_name_behind(real_flow_env):
    client, _manager, tmp_path = real_flow_env
    skill = skill_dir(client, tmp_path, "capability")
    for kind, name in (("capability", "my-notes"), ("node", "my-review")):
        assert pack_tool(skill, "init", name, "--kind", kind, cwd=tmp_path).returncode == 0
        directory = tmp_path / name
        assert yaml.safe_load((directory / "manifest.yaml").read_text(encoding="utf-8"))["kind"] == kind
        for path in directory.rglob("*"):
            assert "sample" not in path.name, path
            if path.is_file():
                assert not re.search(r"sample[-_](notes|review)", path.read_text(encoding="utf-8")), path


def test_build_zip_root_is_the_pack_without_junk(real_flow_env):
    client, _manager, tmp_path = real_flow_env
    skill = skill_dir(client, tmp_path, "capability")
    directory = make_pack(tmp_path / "src", "my-pack", "0.1.0", tenant=True)
    (directory / "__pycache__").mkdir()
    (directory / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"\0")
    (directory / ".DS_Store").write_bytes(b"\0")
    done = pack_tool(skill, "build", directory, "--out", tmp_path / "dist", "--json", cwd=tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    report = json.loads(done.stdout)
    with zipfile.ZipFile(report["zip"]) as archive:
        names = set(archive.namelist())
    assert Path(report["zip"]).name == "my-pack-0.1.0.zip"
    assert {"manifest.yaml", mf.CHECKSUMS_FILE, "commands/my-pack-run.md"} <= names
    assert not {name for name in names if "__pycache__" in name or name.endswith(".DS_Store")}


def test_build_stops_on_failures_and_limits(real_flow_env):
    client, _manager, tmp_path = real_flow_env
    skill = skill_dir(client, tmp_path, "capability")
    out = tmp_path / "dist"
    bad = make_pack(tmp_path / "bad", "my-pack", "0.1.0", tenant=True, files={**RUN, "scripts/bad.py": "def (:\n"})
    assert pack_tool(skill, "build", bad, "--out", out, cwd=tmp_path).returncode == 1
    assert not out.exists()

    good = make_pack(tmp_path / "good", "my-pack", "0.1.0", tenant=True)
    done = pack_tool(skill, "build", good, "--out", good / "dist", cwd=tmp_path)
    assert done.returncode == 1 and "--out" in done.stderr

    path = skill / "platform.json"
    original = json.loads(path.read_text(encoding="utf-8"))
    for key, value, check_failed in (("files", 1, True), ("extracted_bytes", 1, True), ("zip_bytes", 1, False)):
        path.write_text(json.dumps({**original, "limits": {**original["limits"], key: value}}), encoding="utf-8")
        done = pack_tool(skill, "build", good, "--out", out, "--json", cwd=tmp_path)
        assert done.returncode == 1, key
        limits = next(item for item in json.loads(done.stdout)["checks"] if item["id"] == "limits")
        assert (limits["result"] == "fail") is check_failed, key
        assert not list(out.glob("*.zip")), key


# ---------- 端到端：skill 做的包第一次上传就过 ----------


def _upload_and_install(client, built_zip: Path) -> dict:
    """真实上传（PL-22）→ 检查通过 → 安装（PL-26）；检查结果和离线一致。"""
    accepted = post_zip(client, MINE, built_zip.read_bytes(), AS_OWNER)
    assert accepted.status_code == 202, accepted.text
    detail = client.get(f"{MINE}/{accepted.json()['id']}", headers=AS_OWNER).json()
    assert detail["state"] == "passed", [item for item in detail["checks"] if item["result"] == "fail"]
    done = client.post(f"{MINE}/{detail['id']}/install", json={}, headers=AS_OWNER)
    assert done.status_code == 200, done.text
    assert done.json()["state"] == "installed"
    return detail


def _make(client, tmp_path: Path, category: str, name: str, *init_args, edit=None) -> tuple[Path, dict]:
    skill = skill_dir(client, tmp_path, category)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    done = pack_tool(skill, "init", name, *init_args, cwd=work)
    assert done.returncode == 0, done.stderr
    directory = work / name
    if edit:
        edit(directory)
    built = pack_tool(skill, "build", directory, "--json", cwd=work)
    assert built.returncode == 0, built.stdout + built.stderr
    report = json.loads(built.stdout)
    assert report["ok"] and {"private_name", "same_version"} <= upload_time_ids(report)
    detail = _upload_and_install(client, Path(report["zip"]))
    server = {item["id"]: item["result"] for item in detail["checks"] if item["id"] != "private_name"}
    assert tool_results(report) == server, "离线和上传的结果逐项一致"
    return directory, detail


def _enable(client, manager, pid: str, name: str) -> Path:
    enabled = client.post(f"/api/v1/projects/{pid}/packs", json={"name": name}, headers=AS_OWNER)
    assert enabled.status_code == 201, enabled.text
    pack_service.prepare_for_message(manager, pid)
    return manager.project_root(pid)


def _card(client, name: str) -> dict:
    return next(item for item in client.get("/api/v1/packs", headers=AS_OWNER).json()["items"] if item["name"] == name)


def test_capability_pack_end_to_end(real_flow_env):
    client, manager, tmp_path = real_flow_env
    pid = project(client)  # 建项目时 core 入库

    def edit(directory: Path) -> None:
        # 开发者加一条命令：照清单协议声明
        (directory / "commands" / "my-notes-extra.md").write_text("再整理一遍 $ARGUMENTS\n", encoding="utf-8")
        data = yaml.safe_load((directory / "manifest.yaml").read_text(encoding="utf-8"))
        data["contributes"]["commands"].append("my-notes-extra")
        (directory / "manifest.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    _make(client, tmp_path, "capability", "my-notes", edit=edit)
    assert _card(client, "my-notes")["category"] == "capability"
    claude = _enable(client, manager, pid, "my-notes") / ".claude"
    assert (claude / "commands" / "my-notes-run.md").is_file()
    assert (claude / "commands" / "my-notes-extra.md").is_file()
    assert (claude / "agents" / "my-notes-agent.md").is_file()
    assert (claude / "skills" / "my-notes-terms" / "SKILL.md").is_file()
    assert (claude / "scripts" / "my_notes_terms.py").is_file()
    assert (claude / "templates" / "my-notes-glossary.md").is_file()


def test_node_pack_end_to_end(real_flow_env):
    client, manager, tmp_path = real_flow_env
    pid = project(client)
    _make(client, tmp_path, "capability", "my-review", "--kind", "node")
    assert _card(client, "my-review")["category"] == "capability"
    root = _enable(client, manager, pid, "my-review")
    nodes = {node["id"] for node in view.build(root)["nodes"]}
    assert {"my-review.check", "my-review.stats"} <= nodes
    assert (root / ".claude" / "scripts" / "my_review_stats.py").is_file()
    assert (root / ".claude" / "commands" / "my-review-run.md").is_file()


@pytest.mark.parametrize("category, name, entry, scope, content", [
    ("industry", "my-domain", "domain-rule-overdue-inspection", "object:巡检记录", "1.5 倍"),
    ("system", "my-orders", "domain-rule-shipped-order-close", "object:销售订单", "状态码 10"),
])
def test_knowledge_pack_end_to_end(real_flow_env, category, name, entry, scope, content):
    client, manager, tmp_path = real_flow_env
    pid = project(client)
    _make(client, tmp_path, category, name)
    assert _card(client, name)["category"] == category
    root = _enable(client, manager, pid, name)
    data = root / ".claude" / "industry" / name
    for rel in ("pack.json", "concepts.json", "checks.json", "standards.json", f"knowledge/{entry}.md"):
        assert (data / rel).is_file(), rel
    assert json.loads((data / "pack.json").read_text(encoding="utf-8"))["category"] == category
    items = client.get(f"/api/v1/projects/{pid}/knowledge?layer=industry", headers=AS_OWNER).json()["items"]
    assert next(item for item in items if item["id"] == f"{name}:{entry}")["readonly"] is True
    # 建模步骤靠这条命令取知识，不只是文件落地
    loaded = run_cmd([sys.executable, ".claude/scripts/knowledge_helper.py", "load", "--scope", scope],
                     capture_output=True, text=True, cwd=root, timeout=60)
    assert loaded.returncode == 0 and content in loaded.stdout, loaded.stdout + loaded.stderr


def test_provider_pack_imports_and_shows_in_the_catalog(real_flow_env):
    client, manager, tmp_path = real_flow_env
    project(client)
    _make(client, tmp_path, "provider", "my-convert")
    card = _card(client, "my-convert")
    assert card["category"] == "provider" and card["project_status"] == "platform_installed"
    assert card["converter"]["types"] == [".kvtxt"]
    assert manager.packs.repository.owner("my-convert") == OWNER
    # 转换器打进网关镜像才运行（ADR-0073）：导入只到目录这一步，提示开发者的话在 SKILL.md 里
    assert "my-convert" not in {item.get("id") for item in client.get("/api/v1/converters", headers=AS_OWNER).json()["items"]}
