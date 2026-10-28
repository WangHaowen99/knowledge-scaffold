"""脚本运行验证：经脚本验证网关在 jiuwenbox 沙箱中执行，网关不可用时在本机执行，全部通过才写验证记录。

沙箱路径用真实的网关（app.sandbox_gateway）加假的 jiuwenbox：假 jiuwenbox 收到 exec 时在本机真正执行命令。
"""

import importlib.util
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config
from fake_jiuwenbox import FakeJiuwenbox, RunningGateway, unused_port

SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"
BASH = shutil.which("bash")
GIT = shutil.which("git")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"toolkit_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verify = _load("script_verify")
client = verify.sandbox  # script_verify 实际使用的模块对象，替换地址要改它

QUOTE = '''"""
关联operation：无

功能描述：计算报价

输入参数：
    quantity    int    数量，必填

输出：
    JSON 对象：{"total": 总价}
"""
import argparse
import json

from _pricing import unit_price


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quantity", type=int, required=True)
    args = parser.parse_args()
    if args.quantity < 0:
        raise SystemExit("数量不能为负")
    print(json.dumps({"total": args.quantity * unit_price()}))


if __name__ == "__main__":
    main()
'''

CASES = [
    {"name": "正常", "argv": ["--quantity", "2"], "expect": "success"},
    {"name": "缺少必填参数", "argv": [], "expect": "error"},
    {"name": "业务错误", "argv": ["--quantity", "-1"], "expect": "error"},
]


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    scripts = tmp_path / "ontology" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "quote.py").write_text(QUOTE, encoding="utf-8")
    (scripts / "_pricing.py").write_text("def unit_price():\n    return 3\n", encoding="utf-8")
    write_cases(tmp_path, "quote", CASES)
    # 默认没配置脚本验证网关（单独部署 onto-modeling 的情形）：验证回退本机
    monkeypatch.setattr(client, "gateway_url", lambda: "")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write_cases(root: Path, name: str, cases: list) -> str:
    path = root / "tmp" / f"{name}.cases.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    return f"tmp/{name}.cases.json"


def run_cli(root: Path, capsys, *args: str):
    code = verify.main(["--root", str(root), *args])
    return code, capsys.readouterr()


def verify_quote(root: Path, capsys, cases_file: str = "tmp/quote.cases.json"):
    code, out = run_cli(root, capsys, "run", "ontology/scripts/quote.py", "--cases", cases_file)
    return code, json.loads(out.out) if out.out.strip() else None, out


def test_run_falls_back_to_local_and_records_verification(workspace, capsys):
    code, report, _out = verify_quote(workspace, capsys)

    assert code == 0
    assert report["runtime"] == "local"
    assert "未配置脚本验证网关" in report["fallback_reason"]
    assert [case["ok"] for case in report["cases"]] == [True, True, True]
    # 同目录依赖模块随脚本一起送进执行环境
    assert json.loads(report["cases"][0]["stdout"]) == {"total": 6}
    assert "required" in report["cases"][1]["stderr"]
    records = json.loads((workspace / ".ontology-state" / "script-verifications.json").read_text(encoding="utf-8"))
    record = records["scripts"]["ontology/scripts/quote.py"]
    assert record["runtime"] == "local" and record["source_hash"].startswith("sha256:")
    assert [case["name"] for case in record["cases"]] == ["正常", "缺少必填参数", "业务错误"]


def test_check_requires_passing_record_for_current_source(workspace, capsys):
    code, out = run_cli(workspace, capsys, "check", "ontology/scripts/quote.py")
    assert code == 1 and "还没有通过运行验证" in out.out

    verify_quote(workspace, capsys)
    assert run_cli(workspace, capsys, "check", "ontology/scripts/quote.py")[0] == 0
    # 纯库模块不单独验证，import 它的脚本记录有效即可
    assert run_cli(workspace, capsys, "check", "ontology/scripts/_pricing.py")[0] == 0

    # 改了被 import 的模块，import 它的脚本的验证记录随之失效
    (workspace / "ontology" / "scripts" / "_pricing.py").write_text(
        "def unit_price():\n    return 4\n", encoding="utf-8"
    )
    code, out = run_cli(workspace, capsys, "check", "ontology/scripts/_pricing.py")
    assert code == 1 and "ontology/scripts/quote.py" in out.out and "又改过" in out.out


def test_failed_cases_are_reported_and_not_recorded(workspace, capsys):
    cases = write_cases(workspace, "bad", [
        {"name": "预期成功却缺参数", "argv": [], "expect": "success"},
        {"name": "预期报错却成功", "argv": ["--quantity", "1"], "expect": "error"},
    ])
    code, report, _out = verify_quote(workspace, capsys, cases)

    assert code == 1 and report["recorded"] is False and report["failed"] == 2
    assert "退出码 2" in report["cases"][0]["reason"]
    assert "预期报错" in report["cases"][1]["reason"]
    assert not (workspace / ".ontology-state" / "script-verifications.json").exists()


def test_success_requires_single_json_and_output_is_capped(workspace, capsys):
    scripts = workspace / "ontology" / "scripts"
    (scripts / "plain.py").write_text(
        'if __name__ == "__main__":\n    print("done")\n    print("{}")\n', encoding="utf-8"
    )
    (scripts / "long.py").write_text(
        'import json\nif __name__ == "__main__":\n    print(json.dumps("x" * 10000))\n',
        encoding="utf-8",
    )
    cases = write_cases(workspace, "ok", [{"name": "正常", "argv": [], "expect": "success"}])

    code, out = run_cli(workspace, capsys, "run", "ontology/scripts/plain.py", "--cases", cases)
    assert code == 1 and "单个 JSON" in json.loads(out.out)["cases"][0]["reason"]

    code, out = run_cli(workspace, capsys, "run", "ontology/scripts/long.py", "--cases", cases)
    case = json.loads(out.out)["cases"][0]
    # 判定看完整输出，回传给 agent 的只截取前 1500 字
    assert code == 0 and case["ok"] and len(case["stdout"]) == client.OUTPUT_LIMIT


def test_case_timeout_is_reported(workspace, capsys, monkeypatch):
    monkeypatch.setattr(client, "CASE_TIMEOUT_SECONDS", 1)
    (workspace / "ontology" / "scripts" / "slow.py").write_text(
        'import time\nif __name__ == "__main__":\n    time.sleep(10)\n', encoding="utf-8"
    )
    cases = write_cases(workspace, "slow", [{"name": "正常", "argv": [], "expect": "success"}])

    code, out = run_cli(workspace, capsys, "run", "ontology/scripts/slow.py", "--cases", cases)

    assert code == 1 and "超过 1 秒" in json.loads(out.out)["cases"][0]["reason"]


@pytest.mark.parametrize(
    ("cases", "message"),
    [
        ([{"name": "只测报错", "argv": [], "expect": "error"}], "expect=success"),
        ([{"name": "数字参数", "argv": ["--quantity", 2], "expect": "success"}], "字符串数组"),
        ([], "1~12"),
    ],
)
def test_invalid_cases_are_rejected(workspace, capsys, cases, message):
    code, _report, out = verify_quote(workspace, capsys, write_cases(workspace, "invalid", cases))
    assert code == 2 and message in out.err


def test_ok_false_counts_as_failed_call_like_consumer(workspace, capsys):
    """consumer 把退出码 0、输出 ok=false 的结果当作调用失败（不终止对话）：成功用例不能这样，参数错误用例可以。"""
    (workspace / "ontology" / "scripts" / "reject.py").write_text(
        "import json\nif __name__ == \"__main__\":\n"
        "    print(json.dumps({'ok': False, 'error': {'code': 'INVALID_INPUT', 'message': 'qty 不是整数'}}))\n",
        encoding="utf-8",
    )
    cases = write_cases(workspace, "reject", [
        {"name": "预期成功", "argv": [], "expect": "success"},
        {"name": "参数错误", "argv": [], "expect": "error"},
    ])
    code, out = run_cli(workspace, capsys, "run", "ontology/scripts/reject.py", "--cases", cases)
    reported = json.loads(out.out)["cases"]
    assert code == 1
    assert "失败结果" in reported[0]["reason"] and reported[1]["ok"]


ORDER_SCRIPT = '''"""
关联operation：订单审核

功能描述：给出订单审核结论

输入参数：
    orders    array    [[订单]] 查询结果，必填；例：[{"order_id":"A-001","qty":2}]
    order_id    string    订单编号

输出：
    conclusion    string    审核结论
"""
import argparse
import json


def _cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--orders", required=True, type=json.loads)
    parser.add_argument("--order-id")
    args = parser.parse_args()
    print(json.dumps({"conclusion": "通过" if args.orders else "资料不足"}, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
'''


def test_scripts_with_identity_need_a_success_case_without_any(workspace, capsys):
    """只去掉顶层编号参数、记录里仍带编号的用例不算：要模拟用户只描述情况、没给编号的提问。"""
    (workspace / "ontology" / "scripts" / "order_review.py").write_text(ORDER_SCRIPT, encoding="utf-8")
    with_ids = [
        {"name": "按编号", "argv": ["--order-id", "A-001", "--orders", '[{"qty":2}]'], "expect": "success"},
        {"name": "顶层不传编号", "argv": ['--orders=[{"order_id":"A-001","qty":2}]'], "expect": "success"},
        {"name": "嵌套编号", "argv": ["--orders", '[{"qty":2,"lines":[{"sku_code":"S-1"}]}]'], "expect": "success"},
    ]
    code, out = run_cli(
        workspace, capsys, "run", "ontology/scripts/order_review.py",
        "--cases", write_cases(workspace, "order_review", with_ids),
    )
    assert code == 2 and "不带编号的成功用例" in out.err and "order_id" in out.err

    without_ids = [*with_ids, {"name": "不带编号", "argv": ["--orders", '[{"order_id":"","qty":2}]'], "expect": "success"}]
    code, out = run_cli(
        workspace, capsys, "run", "ontology/scripts/order_review.py",
        "--cases", write_cases(workspace, "order_review", without_ids),
    )
    # 只看退出码时，脚本把资料完整的场景判成资料不足也会通过：不带编号的用例要写明预期结论
    assert code == 2 and "stdout_contains" in out.err

    without_ids[-1]["stdout_contains"] = ["通过"]
    code, out = run_cli(
        workspace, capsys, "run", "ontology/scripts/order_review.py",
        "--cases", write_cases(workspace, "order_review", without_ids),
    )
    assert code == 0, out.err


def test_stdout_contains_checks_the_conclusion(workspace, capsys):
    (workspace / "ontology" / "scripts" / "escaped.py").write_text(
        "import json\nif __name__ == \"__main__\":\n    print(json.dumps({'conclusion': '资料不足', 'total': 0}))\n",
        encoding="utf-8",
    )
    cases = write_cases(workspace, "escaped", [
        {"name": "中文被转义也认得出", "argv": [], "expect": "success", "stdout_contains": ["资料不足"]},
        {"name": "结论不对", "argv": [], "expect": "success", "stdout_contains": ["有效线索", "资料不足"]},
    ])
    code, out = run_cli(workspace, capsys, "run", "ontology/scripts/escaped.py", "--cases", cases)
    reported = json.loads(out.out)["cases"]
    assert code == 1 and reported[0]["ok"]
    assert "没有预期的结论：有效线索（" in reported[1]["reason"]  # 只列出缺的那个


@pytest.mark.parametrize("expected", ["有效线索", [""], [1], ["x"] * 11])
def test_invalid_stdout_contains_is_rejected(workspace, capsys, expected):
    cases = [{"name": "正常", "argv": ["--quantity", "2"], "expect": "success", "stdout_contains": expected}]
    code, _report, out = verify_quote(workspace, capsys, write_cases(workspace, "invalid", cases))
    assert code == 2 and "stdout_contains" in out.err


def test_run_uses_jiuwenbox_when_available(workspace, capsys, monkeypatch):
    with FakeJiuwenbox() as box, RunningGateway(box.url) as gateway:
        monkeypatch.setattr(client, "gateway_url", lambda: gateway.url)
        code, report, _out = verify_quote(workspace, capsys)

    assert code == 0 and report["runtime"] == "jiuwenbox" and "fallback_reason" not in report
    assert [method for method, _path in box.calls] == ["GET", "POST", "POST", "DELETE"]
    create, execute = box.bodies
    assert create["policy_mode"] == "override"
    policy = create["policy"]
    assert policy["network"]["egress"]["default"] == "deny"
    # 只挂系统目录，工作区不进沙箱：脚本随 exec 参数送入
    assert {mount["host_path"] for mount in policy["filesystem_policy"]["bind_mounts"]} <= {
        "/bin", "/sbin", "/usr", "/usr/local", "/lib", "/lib64",
    }
    assert execute["command"][:3] == ["/usr/bin/python3", "-I", "-c"]
    assert box.calls[-1] == ("DELETE", f"/api/v1/sandboxes/{create['sandbox_id']}")


@pytest.mark.parametrize(
    ("statuses", "deleted"),
    [({"create_status": 500}, False), ({"exec_status": 500}, True)],
)
def test_sandbox_errors_fall_back_to_local(workspace, capsys, monkeypatch, statuses, deleted):
    with FakeJiuwenbox(**statuses) as box, RunningGateway(box.url) as gateway:
        monkeypatch.setattr(client, "gateway_url", lambda: gateway.url)
        code, report, _out = verify_quote(workspace, capsys)

    assert code == 0 and report["runtime"] == "local" and "500" in report["fallback_reason"]
    # 建成了的沙箱无论 exec 成败都要删除
    assert any(method == "DELETE" for method, _path in box.calls) is deleted


def test_unreachable_gateway_falls_back_to_local(workspace, capsys, monkeypatch):
    monkeypatch.setattr(client, "gateway_url", lambda: f"http://127.0.0.1:{unused_port()}")
    code, report, _out = verify_quote(workspace, capsys)

    assert code == 0 and report["runtime"] == "local" and "连接 脚本验证网关 失败" in report["fallback_reason"]


FLAGS_SCRIPT = '''import json, os, sys
if __name__ == "__main__":
    print(json.dumps({
        "no_user_site": sys.flags.no_user_site,
        "ignore_environment": sys.flags.ignore_environment,
        "isolated": sys.flags.isolated,
        "utf8_mode": sys.flags.utf8_mode,
        "env": sorted(key for key in os.environ if key.startswith(("PYTHON", "PIP"))),
    }))
'''


@pytest.mark.parametrize("sandboxed", [False, True])
def test_scripts_run_with_consumer_interpreter_flags(workspace, monkeypatch, sandboxed):
    """与 consumer 一样 `/usr/bin/python3 -u <脚本>`：不能加 -s/-E/-I。

    jiuwenbox 是 openEuler，它的 Python 把 /usr/local 下的 site-packages 和用户目录包绑在一个
    开关上，加了 -s 就看不到 pip 装进 /usr/local 的包（ortools、consumer 的运行时依赖）。
    """
    script = workspace / "ontology" / "scripts" / "flags.py"
    script.write_text(FLAGS_SCRIPT, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", "/leaked/from/onto-modeling")

    if sandboxed:
        with FakeJiuwenbox() as box, RunningGateway(box.url) as gateway:
            outcome = client.run_cases(script, [[]], gateway=gateway.url)
        assert outcome["runtime"] == "jiuwenbox" and box.bodies[1]["env"] == client.SCRIPT_ENV
    else:
        outcome = client.run_cases(script, [[]], gateway="")
        assert outcome["runtime"] == "local"

    observed = json.loads(outcome["results"][0]["stdout"])
    assert (observed["no_user_site"], observed["ignore_environment"], observed["isolated"]) == (0, 0, 0)
    assert observed["utf8_mode"] == 1
    # 只有 SCRIPT_ENV 里的变量，onto-modeling 进程自己的 PYTHONPATH 不会带进脚本
    assert observed["env"] == ["PYTHONIOENCODING", "PYTHONUTF8"]


def test_probe_lists_pip_packages_of_verification_python():
    with FakeJiuwenbox() as box:
        result = client.probe(box.url)

    # 列包的解释器参数与跑脚本一致，不加隔离参数
    assert box.bodies[1]["command"][:2] == ["/usr/bin/python3", "-c"]
    assert box.bodies[1]["env"] == client.SCRIPT_ENV
    # 假守护进程把沙箱里的 /usr/bin/python3 换成本机解释器执行，列的就是它的包
    assert result["connected"] is True and result["python"] == platform.python_version()
    assert result["path"] == sys.executable and result["pip"]
    # 与 pip list --format=freeze 一致：本机装的 pytest 及其版本都在，按包名排序
    versions = {item["name"].lower(): item["version"] for item in result["packages"]}
    assert versions["pytest"] == pytest.__version__
    names = [item["name"].lower() for item in result["packages"]]
    assert names == sorted(names)

    missing = client.probe("unix:///nonexistent/jiuwenbox.sock")
    assert missing["connected"] is False and missing["packages"] == []
    assert missing["socket"] == "/nonexistent/jiuwenbox.sock" and missing["path"] == "/usr/bin/python3"


def test_gateway_status_reports_sandbox_environment_for_settings_page():
    """配置中心经网关看沙箱：网关在 jiuwenbox 那一侧探测，主容器只拿到结果。"""
    with FakeJiuwenbox() as box, RunningGateway(box.url) as gateway:
        status = client.gateway_status(gateway.url)
    assert status["connected"] is True and status["gateway"] == gateway.url
    assert status["python"] == platform.python_version() and status["packages"]

    unconfigured = client.gateway_status("")
    assert unconfigured["connected"] is False and "未配置脚本验证网关" in unconfigured["detail"]
    unreachable = client.gateway_status(f"http://127.0.0.1:{unused_port()}")
    assert unreachable["connected"] is False and "脚本验证网关" in unreachable["detail"]
    with RunningGateway("unix:///nonexistent/jiuwenbox.sock") as gateway:
        no_socket = client.gateway_status(gateway.url)
    assert no_socket["connected"] is False and "未找到 jiuwenbox socket" in no_socket["detail"]


def test_status_command_reports_gateway(workspace, capsys):
    code, out = run_cli(workspace, capsys, "status")
    state = json.loads(out.out)
    assert code == 0 and state["sandbox"] == "unavailable" and "onto-modeling 容器内执行" in state["detail"]


def test_sandbox_status_route_is_admin_only(monkeypatch):
    pytest.importorskip("fastapi")
    from app.routers import settings as settings_router
    from app.services import auth, sandbox_status
    from app.services.auth import AuthError

    monkeypatch.setattr(sandbox_status.client, "gateway_url", lambda: "")
    monkeypatch.setattr(auth, "ENABLED", True)

    admin = SimpleNamespace(state=SimpleNamespace(user={"sub": "admin", "role": "admin"}))
    status = settings_router.get_sandbox_status(admin)
    assert status["connected"] is False and status["packages"] == [] and status["checked_at"]

    user = SimpleNamespace(state=SimpleNamespace(user={"sub": "alice", "role": "user"}))
    with pytest.raises(AuthError) as exc:
        settings_router.get_sandbox_status(user)
    assert exc.value.status == 403


def _bash_python3_usable() -> bool:
    if BASH is None:
        return False
    try:
        # 安全审查：argv 是 shutil.which 解析出的 bash 路径加写死的探测命令，以序列传入；
        # 探测的正是 pre-commit 钩子经 bash 调用 python3 的运行条件。
        done = subprocess.run(  # NOSONAR(S4721)
            [BASH, "-c", "python3 -c 'import zoneinfo; zoneinfo.ZoneInfo(\"Asia/Shanghai\")'"],
            capture_output=True, timeout=30, check=False, shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def _git(root: Path, *args: str) -> None:
    # 安全审查：argv 是 shutil.which 解析出的 git 路径加本测试写死的子命令与参数，以序列传入、
    # 不经 shell 解析，只作用于 tmp_path 下的临时仓库。
    subprocess.run(  # NOSONAR(S4721)
        [GIT, *args], cwd=root, check=True, capture_output=True, shell=False, timeout=60,
    )


@pytest.mark.skipif(
    GIT is None or not _bash_python3_usable(), reason="pre-commit 钩子经 bash 调用 python3 与 git",
)
def test_pre_commit_blocks_unverified_scripts(workspace, capsys):
    from app.services.engines.jiuwen_assets import JiuwenAssetLoader, run_hooks_sync

    _git(workspace, "init", "-q")
    (workspace / ".claude").symlink_to(SCRIPTS.parent)
    _git(workspace, "add", "ontology")
    entries = JiuwenAssetLoader(Path(config.ASSETS_DIR)).pre_tool_use_hooks()
    commit = {"command": "git commit -m codegen"}

    reason = run_hooks_sync(entries, "bash", commit, workspace)
    assert reason and "运行验证" in reason and "ontology/scripts/quote.py" in reason

    assert verify_quote(workspace, capsys)[0] == 0
    assert run_hooks_sync(entries, "bash", commit, workspace) is None

    # 验证记录只能由验证工具写入：被门禁拦下后手写记录绕过同样被拒
    reason = run_hooks_sync(
        entries, "write_file",
        {"file_path": ".ontology-state/script-verifications.json", "content": "{}"}, workspace,
    )
    assert reason and "script_verify.py run" in reason
