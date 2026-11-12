"""连接型提供方的凭据库（证据采集接口 §8；ADR-0086）。

凭据只存在网关：`ONTO_CONNECTOR_SECRETS_DIR`（卷 connector-secrets，只网关挂）下 `<源>/<版本>.bin`，AES-256-GCM，
主密钥来自 `ONTO_CONNECTOR_MASTER_KEY_FILE`（32 字节 base64）。附加数据绑定源和版本，文件挪到别的源、别的版本下解不开。
写入时与上一版合并（只轮换一个字段时其余字段照旧），版本递增；每个源保留最近 2 个版本，更旧的在没有任务用时删除。
没有读出明文的接口：`open` 只给子进程运行器用，解密结果经 stdin 交给提供方子进程。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..evidence.registry import SOURCE_ID_PATTERN
from .errors import GatewayError

_log = logging.getLogger(__name__)

SECRETS_DIR_ENV = "ONTO_CONNECTOR_SECRETS_DIR"
MASTER_KEY_FILE_ENV = "ONTO_CONNECTOR_MASTER_KEY_FILE"
DEFAULT_DIR = "/secrets"
KEEP_VERSIONS = 2
MAX_FIELDS = 16
MAX_VALUE_CHARS = 8192
_MAGIC = b"OCS1"
_NONCE_BYTES = 12
_SOURCE_RE = re.compile(SOURCE_ID_PATTERN)
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_REF_RE = re.compile(r"^cred://([a-z0-9][a-z0-9-]{1,40})/([1-9][0-9]{0,8})$")
_VERSION_FILE_RE = re.compile(r"^([1-9][0-9]{0,8})\.bin$")


def parse_ref(cred_ref) -> tuple[str, int] | None:
    match = _REF_RE.match(cred_ref) if isinstance(cred_ref, str) else None
    return (match.group(1), int(match.group(2))) if match else None


def _unavailable(message: str) -> GatewayError:
    return GatewayError(503, "secrets_unavailable", message)


class SecretStore:
    def __init__(self, directory: Path | str | None = None, key_file: Path | str | None = None):
        self.directory = Path(directory or os.environ.get(SECRETS_DIR_ENV) or DEFAULT_DIR)
        self.key_file = str(key_file if key_file is not None else os.environ.get(MASTER_KEY_FILE_ENV, "")).strip()
        self._aead: AESGCM | None = None
        self._warned = False
        self._lock = threading.Lock()
        # 正在用的凭据版本（任务子进程没结束）：轮换时不删
        self._in_use: dict[str, int] = {}

    # ---- 主密钥 ----

    def _cipher(self) -> AESGCM | None:
        with self._lock:
            if self._aead is None and self.key_file:
                try:
                    key = base64.b64decode(Path(self.key_file).read_bytes().strip(), validate=True)
                    if len(key) != 32:
                        raise ValueError("主密钥要 32 字节")
                    self._aead = AESGCM(key)  # NOSONAR(S4787) 凭据按设计用 AES-GCM 认证加密，密钥来自受保护的主密钥文件
                except (OSError, ValueError) as exc:
                    if not self._warned:
                        self._warned = True
                        _log.warning("连接器主密钥不可用（%s）：凭据接口回 503，连接型提供方不可用", type(exc).__name__)
            return self._aead

    def ready(self) -> bool:
        return self._cipher() is not None

    def _require(self) -> AESGCM:
        aead = self._cipher()
        if aead is None:
            raise _unavailable("网关没有配置主密钥")
        return aead

    # ---- 读写 ----

    def _source_dir(self, source_id: str) -> Path:
        if not isinstance(source_id, str) or not _SOURCE_RE.match(source_id):
            raise GatewayError(400, "bad_request", "数据源 ID 不对")
        return self.directory / source_id

    def versions(self, source_id: str) -> list[int]:
        directory = self._source_dir(source_id)
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            return []
        return sorted(int(match.group(1)) for match in map(_VERSION_FILE_RE.match, names) if match)

    @staticmethod
    def _aad(source_id: str, version: int) -> bytes:
        return f"onto-connector-secret/1|{source_id}|{version}".encode("ascii")

    def _decrypt(self, aead: AESGCM, source_id: str, version: int) -> dict:
        try:
            blob = (self._source_dir(source_id) / f"{version}.bin").read_bytes()
        except FileNotFoundError:
            raise GatewayError(409, "cred_not_found", "凭据不存在或已删除") from None
        if not blob.startswith(_MAGIC) or len(blob) < len(_MAGIC) + _NONCE_BYTES + 16:
            raise GatewayError(500, "cred_corrupt", "凭据文件损坏")
        nonce, data = blob[len(_MAGIC):len(_MAGIC) + _NONCE_BYTES], blob[len(_MAGIC) + _NONCE_BYTES:]
        try:
            fields = json.loads(aead.decrypt(nonce, data, self._aad(source_id, version)).decode("utf-8"))
        except InvalidTag:
            # 主密钥换了或文件被挪动：只能重新录入
            raise GatewayError(409, "cred_undecryptable", "凭据解不开，要重新录入") from None
        return fields if isinstance(fields, dict) else {}

    def put(self, source_id: str, fields) -> dict:
        """写一个新版本：与上一版合并。回 `{"cred_ref", "written_at"}`。"""
        aead = self._require()
        if not isinstance(fields, dict) or not fields or len(fields) > MAX_FIELDS:
            raise GatewayError(400, "bad_request", f"fields 要是 1～{MAX_FIELDS} 个字段的对象")
        for name, value in fields.items():
            if not _FIELD_RE.match(name) or not isinstance(value, str) or len(value) > MAX_VALUE_CHARS:
                raise GatewayError(400, "bad_request", "字段名或取值不对")
        directory = self._source_dir(source_id)
        with self._lock:
            existing = self.versions(source_id)
            merged = {}
            if existing:
                try:
                    merged = self._decrypt(aead, source_id, existing[-1])
                except GatewayError:
                    merged = {}
            merged.update(fields)
            if len(merged) > MAX_FIELDS:
                raise GatewayError(400, "bad_request", f"字段超过 {MAX_FIELDS} 个")
            version = (existing[-1] if existing else 0) + 1
            nonce = os.urandom(_NONCE_BYTES)
            data = aead.encrypt(nonce, json.dumps(merged, ensure_ascii=False).encode("utf-8"),
                                self._aad(source_id, version))
            try:
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                tmp = directory / f".{version}.tmp"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(_MAGIC + nonce + data)
                    handle.flush()
                    os.fsync(handle.fileno())
                tmp.replace(directory / f"{version}.bin")
            except OSError as exc:
                raise _unavailable(f"凭据目录写不进去（{type(exc).__name__}）") from None
            self._prune(source_id)
        _log.info("凭据已写入 source=%s version=%d fields=%s", source_id, version, ",".join(sorted(merged)))
        return {"cred_ref": f"cred://{source_id}/{version}", "written_at": _iso(time.time())}

    def delete(self, source_id: str) -> dict:
        directory = self._source_dir(source_id)
        with self._lock:
            count = len(self.versions(source_id))
            shutil.rmtree(directory, ignore_errors=True)
        _log.info("凭据已删除 source=%s versions=%d", source_id, count)
        return {"source": source_id, "deleted": count}

    def exists(self, cred_ref, source_id: str) -> bool:
        parsed = parse_ref(cred_ref)
        if parsed is None or parsed[0] != source_id:
            return False
        return (self._source_dir(source_id) / f"{parsed[1]}.bin").is_file()

    def decrypt(self, cred_ref: str) -> dict:
        """解密一个版本给子进程运行器；不经任何接口回出去。"""
        aead = self._require()
        parsed = parse_ref(cred_ref)
        if parsed is None:
            raise GatewayError(400, "bad_request", "cred_ref 不对")
        return self._decrypt(aead, *parsed)

    # ---- 版本保留 ----

    def acquire(self, cred_ref: str) -> None:
        with self._lock:
            self._in_use[cred_ref] = self._in_use.get(cred_ref, 0) + 1

    def release(self, cred_ref: str) -> None:
        parsed = parse_ref(cred_ref)
        with self._lock:
            left = self._in_use.get(cred_ref, 0) - 1
            if left > 0:
                self._in_use[cred_ref] = left
            else:
                self._in_use.pop(cred_ref, None)
            if parsed:
                self._prune(parsed[0])

    def _prune(self, source_id: str) -> None:
        """调用方持锁。最近 KEEP_VERSIONS 个以外、没有任务在用的版本删掉。"""
        for version in self.versions(source_id)[:-KEEP_VERSIONS]:
            if f"cred://{source_id}/{version}" in self._in_use:
                continue
            try:
                (self._source_dir(source_id) / f"{version}.bin").unlink()
            except FileNotFoundError:
                pass


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
