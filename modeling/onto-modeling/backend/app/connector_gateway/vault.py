"""数据源凭据库（ADR-0104；design/data-sources.md 第 7 节）。凭据只存在网关里。

`ONTO_CONNECTOR_SECRETS_DIR`（卷 connector-secrets，只有网关挂）下 `<项目>/<引用>.bin`，AES-256-GCM 加密，附加数据绑定
`<项目>/<引用>`，文件挪到别的项目、别的引用下解不开。主密钥来自 `ONTO_CONNECTOR_MASTER_KEY_FILE`（32 字节 base64）；
没配时首次用到时在凭据目录生成 `.master.key`（0600）。写入时和已存的合并（只改一个字段时其余照旧）。

没有读出明文的接口：`read` 只给子进程运行器用，经 stdin 交给插件子进程。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..evidence.sources import MAX_SECRET_VALUE, valid_secret_ref
from .errors import GatewayError

_log = logging.getLogger(__name__)

SECRETS_DIR_ENV = "ONTO_CONNECTOR_SECRETS_DIR"
MASTER_KEY_FILE_ENV = "ONTO_CONNECTOR_MASTER_KEY_FILE"
DEFAULT_DIR = "/var/lib/connector/secrets"
GENERATED_KEY = ".master.key"
MAX_FIELDS = 20
_MAGIC = b"ODS1"
_NONCE_BYTES = 12
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _unavailable(message: str) -> GatewayError:
    return GatewayError(503, "secrets_unavailable", message)


class Vault:
    def __init__(self, directory: Path | str | None = None, key_file: Path | str | None = None):
        self.directory = Path(directory or os.environ.get(SECRETS_DIR_ENV) or DEFAULT_DIR)
        self.key_file = str(key_file if key_file is not None else os.environ.get(MASTER_KEY_FILE_ENV, "")).strip()
        self._key: bytes | None = None
        self._key_lock = threading.Lock()
        self._lock = threading.Lock()

    # ---- 主密钥 ----

    def _master(self) -> bytes:
        with self._key_lock:
            if self._key is not None:
                return self._key
            path = Path(self.key_file) if self.key_file else self.directory / GENERATED_KEY
            try:
                if not self.key_file and not path.exists():
                    self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(base64.b64encode(os.urandom(32)) + b"\n")
                    _log.info("凭据库主密钥已生成在凭据目录里（要分开存时配 %s）", MASTER_KEY_FILE_ENV)
                key = base64.b64decode(path.read_bytes().strip(), validate=True)
            except FileExistsError:  # 另一个线程刚生成
                key = base64.b64decode(path.read_bytes().strip(), validate=True)
            except (OSError, ValueError) as exc:
                raise _unavailable(f"凭据库主密钥不可用（{type(exc).__name__}）") from None
            if len(key) != 32:
                raise _unavailable("凭据库主密钥要 32 字节")
            self._key = key
            return key

    def mask_salt(self, project: str) -> str:
        """遮蔽用的盐：按项目从主密钥派生，同项目内各表、各次采集一致，不出网关。"""
        return hmac.new(self._master(), f"mask-v1:{project}".encode("utf-8"), hashlib.sha256).hexdigest()

    # ---- 读写 ----

    def _path(self, project: str, ref: str) -> Path:
        if not isinstance(project, str) or not _PROJECT_RE.match(project) or not valid_secret_ref(ref):
            raise GatewayError(400, "bad_request", "项目或凭据引用不对")
        return self.directory / project / f"{ref}.bin"

    @staticmethod
    def _aad(project: str, ref: str) -> bytes:
        return f"onto-datasource-secret/1|{project}|{ref}".encode("utf-8")

    def _decrypt(self, project: str, ref: str) -> dict | None:
        path = self._path(project, ref)
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            return None
        if not blob.startswith(_MAGIC) or len(blob) < len(_MAGIC) + _NONCE_BYTES + 16:
            raise GatewayError(500, "secret_corrupt", "凭据文件损坏")
        nonce, data = blob[len(_MAGIC):len(_MAGIC) + _NONCE_BYTES], blob[len(_MAGIC) + _NONCE_BYTES:]
        try:
            fields = json.loads(AESGCM(self._master()).decrypt(nonce, data, self._aad(project, ref)).decode("utf-8"))
        except InvalidTag:
            # 主密钥换了或文件被挪动：只能重新填
            raise GatewayError(409, "secret_undecryptable", "凭据解不开，要重新填写") from None
        return fields if isinstance(fields, dict) else {}

    def put(self, project: str, ref: str, fields) -> list[str]:
        """和已存的合并后写回；返回存着的字段名。"""
        if not isinstance(fields, dict) or len(fields) > MAX_FIELDS:
            raise GatewayError(400, "bad_request", f"fields 要是最多 {MAX_FIELDS} 个字段的对象")
        for name, value in fields.items():
            if not _FIELD_RE.match(str(name)) or not isinstance(value, str) or len(value) > MAX_SECRET_VALUE:
                raise GatewayError(400, "bad_request", "字段名或取值不对")
        path = self._path(project, ref)
        aead = AESGCM(self._master())
        with self._lock:
            try:
                merged = self._decrypt(project, ref) or {}
            except GatewayError:
                merged = {}
            merged.update(fields)
            nonce = os.urandom(_NONCE_BYTES)
            data = aead.encrypt(nonce, json.dumps(merged, ensure_ascii=False).encode("utf-8"), self._aad(project, ref))
            try:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                tmp = path.with_name(f".{path.name}.tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(_MAGIC + nonce + data)
                    handle.flush()
                    os.fsync(handle.fileno())
                tmp.replace(path)
            except OSError as exc:
                raise _unavailable(f"凭据目录写不进去（{type(exc).__name__}）") from None
        _log.info("凭据已写入 project=%s ref=%s fields=%s", project, ref, ",".join(sorted(merged)))
        return sorted(merged)

    def delete(self, project: str, ref: str) -> bool:
        path = self._path(project, ref)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        _log.info("凭据已删除 project=%s ref=%s", project, ref)
        return True

    def read(self, project: str, ref: str) -> dict | None:
        """给子进程运行器：解密出字段；不存在时 None。不经任何接口回出去。"""
        return self._decrypt(project, ref)
