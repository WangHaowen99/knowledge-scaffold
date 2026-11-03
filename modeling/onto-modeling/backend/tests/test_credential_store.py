"""凭据存储公共件：0600 原子写、has_* 视图、留空沿用、可靠删除。fixture 全部为合成数据。"""

import json
import os
import stat
import threading
from pathlib import Path

import pytest

from app import config
from app.services import credential_store, model_settings, personal_model_settings
from app.services.credential_store import SecretStore


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位")
def test_private_json_is_0600_and_leaves_no_temporary(tmp_path):
    path = tmp_path / "nested" / "secrets.json"
    credential_store.write_private_json(path, {"token": "synthetic-token"})
    assert mode(path) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {"token": "synthetic-token"}
    assert [p.name for p in path.parent.iterdir()] == ["secrets.json"]


def test_failed_write_keeps_previous_content(tmp_path):
    path = tmp_path / "secrets.json"
    credential_store.write_private_json(path, {"token": "synthetic-old"})
    with pytest.raises(TypeError):
        credential_store.write_private_json(path, {"token": object()})
    assert json.loads(path.read_text(encoding="utf-8")) == {"token": "synthetic-old"}
    assert [p.name for p in tmp_path.iterdir()] == ["secrets.json"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位")
def test_private_dir_is_0700(tmp_path):
    folder = credential_store.ensure_private_dir(tmp_path / "vault")
    assert mode(folder) == 0o700


def test_public_view_replaces_secrets_with_has_flags():
    record = {"name": "合成数据源", "password": "synthetic-password", "token": ""}
    view = credential_store.public_view(record, ("password", "token"))
    assert view == {"name": "合成数据源", "has_password": True, "has_token": False}
    assert "synthetic-password" not in json.dumps(view)
    assert record["password"] == "synthetic-password"  # 不改入参


def test_keep_secrets_reuses_saved_value_when_blank():
    previous = {"password": "synthetic-old", "token": "synthetic-token"}
    merged = credential_store.keep_secrets({"password": "", "token": "synthetic-new"}, previous, ("password", "token"))
    assert merged == {"password": "synthetic-old", "token": "synthetic-new"}
    assert credential_store.keep_secrets({"password": " "}, None, ("password",)) == {"password": " "}


def test_store_update_is_serialized(tmp_path):
    store = SecretStore(tmp_path / "counter.json")

    def bump(data):
        data["count"] = data.get("count", 0) + 1

    threads = [threading.Thread(target=lambda: [store.update(bump) for _ in range(20)]) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.load() == {"count": 100}


def test_store_path_follows_data_dir(tmp_path, monkeypatch):
    store = SecretStore(lambda: Path(config.DATA_DIR) / "store.json")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "a")
    store.save({"v": 1})
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "b")
    assert store.load() == {}
    assert (tmp_path / "a" / "store.json").is_file()


def test_corrupt_store_reads_as_empty(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    assert SecretStore(path).load() == {}
    path.write_text("[1, 2]", encoding="utf-8")
    assert SecretStore(path).load() == {}


def test_purge_removes_files_dirs_and_partial_writes(tmp_path):
    secret_file = tmp_path / "sources.json"
    secret_file.write_text("{}", encoding="utf-8")
    partial = tmp_path / ".sources.json.abc123.tmp"
    partial.write_text("{}", encoding="utf-8")
    secret_dir = tmp_path / "vault"
    (secret_dir / "nested").mkdir(parents=True)
    (secret_dir / "nested" / "p1.json").write_text("{}", encoding="utf-8")
    keep = tmp_path / "keep.json"
    keep.write_text("{}", encoding="utf-8")

    assert credential_store.purge_paths([secret_file, secret_dir, tmp_path / "missing.json"]) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.json"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位")
def test_model_settings_files_are_written_private(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    model_settings.save("https://gateway.example.com", "synthetic-admin-key", "synthetic-model")
    model_settings.clear_key()
    assert mode(model_settings._file()) == 0o600
    personal_model_settings._write({"users": {}})
    assert mode(personal_model_settings._file()) == 0o600
