"""F13 分块上传（EV-31～EV-33）：乱序、重传、哈希不符、没传完、超限、每人上限、过期清理。"""

from __future__ import annotations

import hashlib
import time

from app.evidence import storage, uploads
from tests import f13_support

f13 = f13_support.f13

BASE = "/api/v1/evidence/uploads"


def _start(f13, size: int, headers=None, filename="demo.bin"):
    return f13.client.post(BASE, json={"filename": filename, "size": size}, headers=headers or {})


def test_out_of_order_and_resent_chunks_assemble_into_blob(f13):
    f13.monkeypatch.setattr(uploads, "CHUNK_BYTES", 4)
    data = b"0123456789"  # 3 块：4 + 4 + 2
    started = _start(f13, len(data))
    assert started.status_code == 201
    upload = started.json()
    assert upload["chunk_bytes"] == 4 and upload["max_bytes"] == 2048 * 1024 * 1024
    url = f"{BASE}/{upload['upload_id']}"
    assert f13.client.put(f"{url}/chunks/2", content=data[8:]).json() == {"received_bytes": 2, "received_chunks": 1}
    assert f13.client.put(f"{url}/chunks/0", content=b"XXXX").status_code == 200
    missing = f13.client.post(f"{url}/complete", json={"sha256": hashlib.sha256(data).hexdigest()})
    assert missing.status_code == 409 and missing.json()["details"]["missing"] == [1]
    assert f13.client.put(f"{url}/chunks/0", content=data[:4]).status_code == 200  # 重传以最后一次为准
    assert f13.client.put(f"{url}/chunks/1", content=data[4:8]).json()["received_chunks"] == 3
    done = f13.client.post(f"{url}/complete", json={"sha256": hashlib.sha256(data).hexdigest()})
    assert done.status_code == 200, done.text
    artifact = done.json()["artifact"]
    assert artifact == {"sha256": hashlib.sha256(data).hexdigest(), "size": 10, "filename": "demo.bin"}
    assert storage.blob_path(artifact["sha256"]).read_bytes() == data
    assert f13.client.post(f"{url}/complete", json={"sha256": artifact["sha256"]}).status_code == 404
    # 同一内容再传一次直接用仓里的
    again = f13.upload(data)
    assert again["sha256"] == artifact["sha256"]


def test_wrong_chunk_size_index_and_hash(f13):
    f13.monkeypatch.setattr(uploads, "CHUNK_BYTES", 4)
    data = b"abcdefgh"
    upload = _start(f13, len(data)).json()
    url = f"{BASE}/{upload['upload_id']}"
    assert f13.client.put(f"{url}/chunks/0", content=b"abc").status_code == 400
    assert f13.client.put(f"{url}/chunks/0", content=b"abcdef").status_code == 400
    assert f13.client.put(f"{url}/chunks/2", content=b"abcd").status_code == 400
    for index in range(2):
        f13.client.put(f"{url}/chunks/{index}", content=data[index * 4:(index + 1) * 4])
    wrong = f13.client.post(f"{url}/complete", json={"sha256": "0" * 64})
    assert wrong.status_code == 422 and wrong.json()["code"] == "upload_hash_mismatch"
    assert storage.blob_size("0" * 64) is None
    assert f13.client.post(f"{url}/complete", json={"sha256": "zz"}).status_code == 400
    assert f13.client.put(f"{BASE}/up-missing/chunks/0", content=b"a").status_code == 404


def test_size_limit_and_pending_quota(f13):
    f13.monkeypatch.setenv("ONTO_WEB_EVIDENCE_MAX_ARTIFACT_MB", "1")
    too_large = _start(f13, 1024 * 1024 + 1)
    assert too_large.status_code == 413 and too_large.json()["details"]["max_bytes"] == 1024 * 1024
    assert _start(f13, 0).status_code == 400
    assert _start(f13, 10).status_code == 201 and _start(f13, 10).status_code == 201
    third = _start(f13, 10)
    assert third.status_code == 429 and third.json()["code"] == "quota_exceeded"
    # 过期的上传清掉后名额回来
    meta = next((storage.root() / "uploads").glob("*/meta.json"))
    import json

    record = json.loads(meta.read_text(encoding="utf-8"))
    record["created_at"] = time.time() - uploads.EXPIRE_S - 5
    meta.write_text(json.dumps(record), encoding="utf-8")
    assert _start(f13, 10).status_code == 201
    assert not meta.parent.exists()


def test_uploads_belong_to_uploader(f13):
    alice = f13.login("alice")
    bob = f13.login("bob")
    upload = _start(f13, 4, headers=alice).json()
    url = f"{BASE}/{upload['upload_id']}"
    assert f13.client.put(f"{url}/chunks/0", content=b"abcd", headers=bob).status_code == 404
    assert f13.client.put(f"{url}/chunks/0", content=b"abcd", headers=alice).status_code == 200
    assert f13.client.post(f"{url}/complete", json={"sha256": hashlib.sha256(b"abcd").hexdigest()},
                           headers=bob).status_code == 404
    # 每人各自算名额
    assert _start(f13, 4, headers=bob).status_code == 201 and _start(f13, 4, headers=bob).status_code == 201
    assert _start(f13, 4, headers=alice).status_code == 201


def test_cancel_upload_frees_quota(f13):
    first, second = _start(f13, 4).json(), _start(f13, 4).json()
    assert _start(f13, 4).status_code == 429
    assert f13.client.delete(f"{BASE}/{first['upload_id']}").status_code == 204
    assert not (storage.root() / "uploads" / first["upload_id"]).exists()
    assert f13.client.delete(f"{BASE}/{first['upload_id']}").status_code == 404
    assert _start(f13, 4).status_code == 201
    f13.client.put(f"{BASE}/{second['upload_id']}/chunks/0", content=b"abcd")
    f13.client.post(f"{BASE}/{second['upload_id']}/complete", json={"sha256": hashlib.sha256(b"abcd").hexdigest()})
    assert f13.client.delete(f"{BASE}/{second['upload_id']}").status_code == 404  # 已完成的不能删
