"""I1 审查修复第 11 条：V-03 `download=1` 只打 `ontology/` 下的文件。

读不出文件（如 git 超时）时报错，不返回空 zip；超过上限回 413。
"""

import io
import zipfile

from app.services import version_views
from tests.test_i1_versions import api, commit, obj  # noqa: F401  （夹具按名字注入）


def test_download_is_limited_to_ontology(api):  # noqa: F811
    client, pid, root = api
    commit(root, "ontology/object/门店.md", obj("门店", "合成"), "web: 门店")
    base = f"/api/v1/projects/{pid}/versions"
    for prefix in ("", ".onto/", ".git/", "resource/"):
        resp = client.get(f"{base}/HEAD/files", params={"download": 1, "prefix": prefix})
        assert resp.status_code == 400, (prefix, resp.status_code)
    ok = client.get(f"{base}/HEAD/files", params={"download": 1, "prefix": "ontology/object/"})
    assert ok.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(ok.content)).namelist()
    assert "ontology/object/门店.md" in names and all(name.startswith("ontology/object/") for name in names)


def test_unreadable_blobs_are_an_error_not_an_empty_zip(api, monkeypatch):  # noqa: F811
    client, pid, root = api
    commit(root, "ontology/object/门店.md", obj("门店", "合成"), "web: 门店")
    monkeypatch.setattr(version_views, "cat_files", lambda *_args, **_kw: {})  # 模拟 git cat-file 超时
    resp = client.get(f"/api/v1/projects/{pid}/versions/HEAD/files", params={"download": 1})
    assert resp.status_code == 503 and resp.json()["code"] == "service_unavailable", resp.text[:200]


def test_download_over_limit_is_413(api, monkeypatch):  # noqa: F811
    client, pid, root = api
    commit(root, "ontology/object/门店.md", obj("门店", "合成"), "web: 门店")
    monkeypatch.setattr(version_views, "ZIP_BYTES_MAX", 10)
    resp = client.get(f"/api/v1/projects/{pid}/versions/HEAD/files", params={"download": 1})
    assert resp.status_code == 413 and resp.json()["code"] == "payload_too_large", resp.text[:200]
