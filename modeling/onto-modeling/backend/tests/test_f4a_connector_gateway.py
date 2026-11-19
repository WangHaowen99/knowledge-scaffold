"""F4a 连接器网关第一版（ADR-0073）：一次性票据、每个任务一个子进程（超时、内存、输出上限、崩溃）、HTTP 接口。

转换器用测试里现写的假转换器（只用标准库），不依赖转换库；真转换器走网关的那条在最后，缺库时跳过。
"""

import json
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from app.connector_gateway import registry, runner, server
from app.evidence.convert import ConvertLimits
from app.evidence.tickets import Issuer, TicketError, Verifier, sha256_hex

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PACKS = Path(__file__).resolve().parents[2] / "assets" / "packs"

FAKES = {
    "fake-ok": """
        from app.evidence.convert import Conversion, page_marker, parse_locations
        def convert(data, filename, limits):
            md = page_marker(1) + "\\n" + data.decode() + "\\n"
            return Conversion(state="converted", markdown=md, locations=parse_locations(md))
    """,
    "fake-slow": """
        import time
        def convert(data, filename, limits):
            time.sleep(30)
    """,
    "fake-crash": """
        import os
        def convert(data, filename, limits):
            os._exit(3)
    """,
    "fake-flood": """
        from app.evidence.convert import Conversion
        def convert(data, filename, limits):
            return Conversion(state="converted", markdown="x" * (4 * 1024 * 1024))
    """,
    "fake-hog": """
        def convert(data, filename, limits):
            blocks = []
            while True:
                blocks.append(bytearray(64 * 1024 * 1024))
    """,
    "fake-raise": """
        def convert(data, filename, limits):
            raise ValueError("parser blew up")
    """,
}


@pytest.fixture()
def converters(tmp_path):
    for name, code in FAKES.items():
        folder = tmp_path / name
        folder.mkdir()
        (folder / "manifest.yaml").write_text(
            f"name: {name}\nversion: 0.1.0\nkind: provider\nlabel: {name}\n"
            f"converter: {{entry: converter.py, types: ['.txt'], license: MIT}}\n", encoding="utf-8")
        (folder / "converter.py").write_text(textwrap.dedent(code), encoding="utf-8")
    # 清单不完整的不加载
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "manifest.yaml").write_text("name: broken\nkind: provider\n", encoding="utf-8")
    return registry.load(tmp_path)


def _run(converters, name, data=b"hello", **limits):
    return runner.run(converters[name], data, "a.txt", ConvertLimits(), runner.JobLimits(**limits))


# ---- 票据 ----


def test_ticket_roundtrip_and_single_use(tmp_path):
    issuer = Issuer(tmp_path / "k.pem")
    verifier = Verifier(issuer.public_jwk())
    token = issuer.issue(job_id="j1", operation="convert", scope_hash="abc", budget={"max_bytes": 10},
                         claims={"converter": "convert-pdf"})
    claims = verifier.verify(token, operation="convert", scope_hash="abc")
    assert claims["job"] == "j1" and claims["converter"] == "convert-pdf" and claims["exp"] - claims["iat"] == 600
    with pytest.raises(TicketError, match="已用过"):
        verifier.verify(token, operation="convert", scope_hash="abc")
    # 密钥落在密钥目录里，权限 600；再开一次用同一把
    assert (tmp_path / "k.pem").stat().st_mode & 0o777 == 0o600
    assert Issuer(tmp_path / "k.pem").public_jwk() == issuer.public_jwk()


@pytest.mark.parametrize("case", ["scope", "operation", "expired", "other_key", "tampered"])
def test_ticket_rejections(tmp_path, case):
    issuer = Issuer(tmp_path / "k.pem")
    verifier = Verifier(issuer.public_jwk())
    now = time.time()
    if case == "other_key":
        token = Issuer(tmp_path / "other.pem").issue(job_id="j", operation="convert", scope_hash="abc", budget={})
    elif case == "expired":
        token = issuer.issue(job_id="j", operation="convert", scope_hash="abc", budget={}, now=now - 700)
    else:
        token = issuer.issue(job_id="j", operation="convert", scope_hash="abc", budget={})
    if case == "tampered":
        head, body, sig = token.split(".")
        token = ".".join([head, body[:-2] + ("AA" if body[-2:] != "AA" else "BB"), sig])
    with pytest.raises(TicketError):
        verifier.verify(token, operation="snapshot" if case == "operation" else "convert",
                        scope_hash="xyz" if case == "scope" else "abc")


# ---- 子进程 ----


def test_registry_reads_manifests_only(converters):
    assert set(converters) == set(FAKES)
    assert converters["fake-ok"].info.types == (".txt",)


def test_runner_ok(converters):
    result = _run(converters, "fake-ok", b"body text")
    assert result["state"] == "converted"
    assert result["markdown"] == "<!-- 第 1 页 -->\nbody text\n"
    assert result["locations"][0]["label"] == "第 1 页"


def test_runner_timeout_kills(converters):
    started = time.time()
    assert _run(converters, "fake-slow", timeout_s=1.5)["error_code"] == "timeout"
    assert time.time() - started < 10


def test_runner_crash_and_exception(converters):
    assert _run(converters, "fake-crash")["error_code"] == "crashed"
    result = _run(converters, "fake-raise")
    assert result == {"state": "failed", "error_code": "crashed", "error_message": "转换出错"}


def test_runner_output_cap(converters):
    assert _run(converters, "fake-flood", max_output_bytes=1024 * 1024)["error_code"] == "output_too_large"


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS 只在 Linux 上生效（容器里）")
def test_runner_memory_cap(converters):
    assert _run(converters, "fake-hog", memory_bytes=256 * 1024 * 1024, timeout_s=30)["error_code"] == "memory"


# ---- HTTP 接口 ----


@pytest.fixture()
def gateway(tmp_path, converters):
    issuer = Issuer(tmp_path / "issuer.pem")
    instance = server.Gateway(converters, server.KeySource(url="test", fetch=issuer.public_jwk),
                              job_limits=runner.JobLimits(timeout_s=2), max_concurrent=1, max_waiting=0)
    httpd = server.serve(0, instance, host="127.0.0.1")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", issuer, instance
    httpd.shutdown()


def _post(url, data, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(f"{url}/v1/convert", data=data, method="POST", headers=headers)
    try:
        with OPENER.open(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _ticket(issuer, data, converter="fake-ok", **claims):
    return issuer.issue(job_id="j", operation="convert", scope_hash=sha256_hex(data),
                        budget={"max_bytes": 1 << 20, "max_duration_s": 60}, claims={"converter": converter, **claims})


def test_http_health_providers_and_convert(gateway):
    url, issuer, _ = gateway
    assert json.loads(OPENER.open(f"{url}/health").read()) == {"status": "ok", "converters": len(FAKES), "sources": 0}
    items = json.loads(OPENER.open(f"{url}/v1/providers").read())["items"]
    assert {item["id"] for item in items} == set(FAKES)
    status, body = _post(url, b"abc", _ticket(issuer, b"abc"))
    assert status == 200 and body["state"] == "converted" and body["converter"] == {"id": "fake-ok", "version": "0.1.0"}


def test_http_rejects_without_valid_ticket(gateway):
    url, issuer, _ = gateway
    assert _post(url, b"abc")[0] == 401
    assert _post(url, b"abc", "not-a-jwt")[0] == 401
    # 票据绑定的是另一份内容
    assert _post(url, b"abc", _ticket(issuer, b"other"))[1]["error"] == "ticket_invalid"
    token = _ticket(issuer, b"abc")
    assert _post(url, b"abc", token)[0] == 200
    assert _post(url, b"abc", token)[0] == 401  # 重放
    assert _post(url, b"abc", _ticket(issuer, b"abc", converter="nope"))[0] == 404


def test_http_busy_and_job_failure_keep_gateway_alive(gateway):
    url, issuer, _ = gateway
    slow = threading.Thread(target=_post, args=(url, b"slow", _ticket(issuer, b"slow", converter="fake-slow")))
    slow.start()
    time.sleep(0.5)
    status, body = _post(url, b"x", _ticket(issuer, b"x"))
    assert status == 503 and body["error"] == "busy"
    slow.join()
    status, body = _post(url, b"y", _ticket(issuer, b"y", converter="fake-crash"))
    assert status == 200 and body["state"] == "failed" and body["error_code"] == "crashed"
    assert _post(url, b"z", _ticket(issuer, b"z"))[1]["state"] == "converted"


def test_http_issuer_key_unavailable(converters):
    def unreachable():
        raise urllib.error.URLError("down")

    instance = server.Gateway(converters, server.KeySource(url="test", fetch=unreachable))
    issuer = Issuer(Path(converters["fake-ok"].directory).parent / "k.pem")
    status, body = instance.convert(_ticket(issuer, b"a"), b"a")
    assert status == 503 and body["error"] == "issuer_unavailable"


def test_real_converters_through_gateway(tmp_path):
    pytest.importorskip("pdfplumber")
    import conversion_samples as samples

    issuer = Issuer(tmp_path / "issuer.pem")
    instance = server.Gateway(registry.load(PACKS), server.KeySource(url="test", fetch=issuer.public_jwk))
    assert {"convert-pdf", "convert-docx", "convert-xlsx"} <= set(instance.converters)
    data = samples.pdf([{"lines": ["A page with enough text in it."]}])
    status, body = instance.convert(_ticket(issuer, data, converter="convert-pdf", filename="a.pdf"), data)
    assert status == 200 and body["state"] == "converted" and body["markdown"].startswith("<!-- 第 1 页 -->")
    bomb = samples.zip_bomb("xl/worksheets/sheet1.xml", 120)
    status, body = instance.convert(_ticket(issuer, bomb, converter="convert-xlsx"), bomb)
    assert body["state"] == "failed" and body["error_code"] == "too_large"
