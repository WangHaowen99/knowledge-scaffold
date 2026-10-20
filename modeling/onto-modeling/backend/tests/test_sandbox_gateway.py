"""脚本验证网关：只接受打包好的脚本和用例，按写死的策略经 jiuwenbox 运行，自己从不执行脚本。

网关是唯一挂载 jiuwenbox 管理 socket 的容器；onto-modeling 主容器里的建模 agent 能直接访问网关，
所以这里断言的都是 agent 绕不过去的边界：改不了策略、解释器和执行器，挤不爆沙箱，拿不到 socket。
"""

import base64
import http.client
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app import sandbox_gateway
from fake_jiuwenbox import FakeJiuwenbox, RunningGateway

client = sandbox_gateway.client

SCRIPT = 'import json\nif __name__ == "__main__":\n    print(json.dumps({"total": 6}))\n'


def verify_body(tmp_path: Path, **overrides) -> dict:
    script = tmp_path / "quote.py"
    script.write_text(SCRIPT, encoding="utf-8")
    body = {
        "bundle": client._bundle(script),
        "entry": "quote.py",
        "cases": client._encode_cases([[]]),
        "case_count": 1,
    }
    return {**body, **overrides}


def request(url: str, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=30)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        connection.close()


def post_json(url: str, payload) -> tuple[int, dict]:
    return request(url, "POST", "/v1/verify", json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json"})


def test_verify_runs_bundle_with_fixed_policy_runner_and_interpreter(tmp_path):
    with FakeJiuwenbox() as box, RunningGateway(box.url) as gateway:
        status, data = post_json(gateway.url, verify_body(tmp_path))

    assert status == 200
    (result,) = data["results"]
    assert result["exit_code"] == 0 and json.loads(result["stdout"]) == {"total": 6}
    create, execute = box.bodies
    # 策略、解释器与执行器都由网关写死，与 jiuwenbox_client 的单一事实源一致
    assert create["policy_mode"] == "override" and create["policy"] == client.sandbox_policy()
    assert execute["command"][:4] == ["/usr/bin/python3", "-I", "-c", client.RUNNER]
    assert execute["env"] == client.SCRIPT_ENV
    assert box.calls[-1] == ("DELETE", f"/api/v1/sandboxes/{create['sandbox_id']}")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        # 调用方想自带策略、命令或环境变量：多出来的字段一律拒绝
        ({"policy": {"network": {"egress": {"default": "allow"}}}}, "只能包含"),
        ({"command": ["/bin/sh", "-c", "id"]}, "只能包含"),
        ({"entry": "../escape.py"}, "脚本文件名"),
        ({"entry": "run.sh"}, "脚本文件名"),
        ({"case_count": 0}, "1~16"),
        ({"case_count": 17}, "1~16"),
        ({"case_count": True}, "1~16"),
        ({"case_count": "1"}, "1~16"),
        ({"bundle": ""}, "非空字符串"),
        ({"cases": ["--flag"]}, "非空字符串"),
        ({"bundle": "A" * (96 * 1024 + 1)}, "上限"),
    ],
)
def test_verify_rejects_requests_that_try_to_steer_the_sandbox(tmp_path, overrides, message):
    with FakeJiuwenbox() as box, RunningGateway(box.url) as gateway:
        status, data = post_json(gateway.url, verify_body(tmp_path, **overrides))

    assert status == 400 and message in data["error"]
    # 不合法的请求连 jiuwenbox 都不会碰
    assert box.calls == []


def test_request_framing_limits(tmp_path):
    with FakeJiuwenbox() as box, RunningGateway(box.url) as gateway:
        too_large = request(gateway.url, "POST", "/v1/verify", b"x", {
            "Content-Length": str(sandbox_gateway.MAX_BODY_BYTES + 1),
        })
        not_json = request(gateway.url, "POST", "/v1/verify", b"not json")
        unknown = request(gateway.url, "POST", "/api/v1/sandboxes", b"{}")
        health = request(gateway.url, "GET", "/health")

    assert too_large[0] == 413 and not_json[0] == 400 and unknown[0] == 404
    # 容器健康检查只看网关自己，不碰 jiuwenbox
    assert health == (200, {"status": "ok"}) and box.calls == []


def test_unavailable_jiuwenbox_is_503_and_nothing_runs_in_the_gateway(tmp_path):
    with RunningGateway("unix:///nonexistent/jiuwenbox.sock") as gateway:
        status, data = post_json(gateway.url, verify_body(tmp_path))

    # 调用方据 503 回退到自己的容器内执行；网关自己不跑脚本，否则脚本就跑在了能连 socket 的容器里
    assert status == 503 and "未找到 jiuwenbox socket" in data["error"] and "results" not in data
    source = Path(sandbox_gateway.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in source and "os.system" not in source


def test_concurrency_is_bounded_and_overflow_is_rejected(tmp_path):
    gateway = sandbox_gateway.Gateway(
        socket_url="unix:///nonexistent/jiuwenbox.sock", max_concurrent=1, max_waiting=0, wait_seconds=0.05,
    )
    with gateway.slot() as held:
        assert held is True
        status, data = gateway.verify(verify_body(tmp_path))
        busy_status, _ = gateway.status()
    assert (status, busy_status) == (503, 503) and "繁忙" in data["error"]
    # 执行位释放后恢复正常处理（这里 jiuwenbox 不存在，所以是不可用的 503）
    status, data = gateway.verify(verify_body(tmp_path))
    assert status == 503 and "未找到 jiuwenbox socket" in data["error"]


def test_status_lists_sandbox_python_environment():
    with FakeJiuwenbox() as box, RunningGateway(box.url) as gateway:
        status, data = request(gateway.url, "GET", "/v1/status")

    assert status == 200 and data["connected"] is True and data["packages"]
    assert box.bodies[1]["command"][:2] == ["/usr/bin/python3", "-c"]


def test_bundle_is_decoded_only_inside_the_sandbox(tmp_path):
    """网关不解包、不落盘：脚本内容原样作为 exec 参数交给沙箱里的执行器。"""
    body = verify_body(tmp_path)
    with FakeJiuwenbox() as box, RunningGateway(box.url) as gateway:
        post_json(gateway.url, body)

    execute = box.bodies[1]
    assert execute["command"][4] == body["bundle"] and execute["command"][6] == body["cases"]
    assert base64.b64decode(body["bundle"])  # 是合法的 base64 zip，网关原样转交
