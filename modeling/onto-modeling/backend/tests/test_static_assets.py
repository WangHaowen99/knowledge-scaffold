"""前端静态资源（F2 第 1 条：第一次切换页面白屏几秒）。

构建时预压缩的 .br / .gz 按 Accept-Encoding 发，
带哈希的 assets/ 长缓存，index.html 每次都向服务端确认。
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main
from app.static_assets import PrecompressedStaticFiles

JS = ("export const x = '" + "页面代码" * 400 + "';\n").encode()
IMMUTABLE = "public, max-age=31536000, immutable"


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    assets = tmp_path / "assets"
    assets.mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    (tmp_path / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (assets / "StudioPage-AbC123.js").write_bytes(JS)
    (assets / "StudioPage-AbC123.js.gz").write_bytes(gzip.compress(JS))
    (assets / "StudioPage-AbC123.js.br").write_bytes(b"synthetic-br-bytes")
    (assets / "tiny-Zz9.css").write_text("a{}", encoding="utf-8")
    app = FastAPI()
    app.mount("/", PrecompressedStaticFiles(directory=tmp_path, html=True), name="static")
    return TestClient(app)


def _get(client: TestClient, path: str, encoding: str | None):
    headers = {"Accept-Encoding": encoding} if encoding is not None else {"Accept-Encoding": "identity"}
    return client.get(path, headers=headers)


def test_brotli_preferred_when_accepted(client):
    r = _get(client, "/assets/StudioPage-AbC123.js", "gzip, deflate, br, zstd")
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "br"
    assert r.headers["content-type"].startswith(("text/javascript", "application/javascript"))
    assert r.headers["content-length"] == str(len(b"synthetic-br-bytes"))
    assert r.headers["vary"] == "Accept-Encoding"
    assert r.headers["cache-control"] == IMMUTABLE


def test_gzip_when_brotli_not_accepted(client):
    r = _get(client, "/assets/StudioPage-AbC123.js", "gzip, br;q=0")
    assert r.headers["content-encoding"] == "gzip"
    assert r.content == JS  # 客户端照常解压
    assert r.headers["vary"] == "Accept-Encoding"
    assert r.headers["cache-control"] == IMMUTABLE


def test_plain_when_no_encoding_accepted(client):
    r = _get(client, "/assets/StudioPage-AbC123.js", None)
    assert "content-encoding" not in r.headers
    assert r.content == JS
    assert r.headers["vary"] == "Accept-Encoding"
    assert r.headers["cache-control"] == IMMUTABLE


def test_file_without_variants_is_served_as_is(client):
    r = _get(client, "/assets/tiny-Zz9.css", "gzip, br")
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert "vary" not in r.headers
    assert r.headers["cache-control"] == IMMUTABLE


@pytest.mark.parametrize("path", ["/", "/index.html", "/favicon.svg"])
def test_entry_and_unhashed_files_revalidate(client, path):
    r = _get(client, path, "gzip, br")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"


def test_not_modified_keeps_encoding_headers(client):
    first = _get(client, "/assets/StudioPage-AbC123.js", "br")
    r = client.get("/assets/StudioPage-AbC123.js", headers={"Accept-Encoding": "br", "If-None-Match": first.headers["etag"]})
    assert r.status_code == 304
    assert r.headers["cache-control"] == IMMUTABLE
    assert r.headers["vary"] == "Accept-Encoding"


def test_missing_file_is_404(client):
    assert _get(client, "/assets/nope-123.js", "br").status_code == 404


def test_app_serves_frontend_with_precompressed_files():
    assert main.STATIC_FILES_CLASS is PrecompressedStaticFiles
