# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Versioned framed hashing for ontology evidence identities."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal, Sequence


HashType = Literal["s", "n", "i", "b", "h", "o", "l"]
_CONTRACT_RE = re.compile(r"^[a-z0-9_.-]+/v[1-9][0-9]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PREFIX = b"OntologyEvidenceHash\x1ev1\x1e"
_UNIT_SEPARATOR = b"\x1f"
_RECORD_SEPARATOR = b"\x1e"


@dataclass(frozen=True)
class HashValue:
    type: HashType
    value: Any


@dataclass(frozen=True)
class HashField:
    name: str
    value: HashValue


def value_s(value: str) -> HashValue:
    return HashValue("s", value)


def value_n() -> HashValue:
    return HashValue("n", None)


def value_i(value: int) -> HashValue:
    return HashValue("i", value)


def value_b(value: bool) -> HashValue:
    return HashValue("b", value)


def value_h(value: str) -> HashValue:
    return HashValue("h", value)


def value_o(*fields: HashField) -> HashValue:
    return HashValue("o", tuple(fields))


def value_l(*values: HashValue) -> HashValue:
    return HashValue("l", tuple(values))


def field_s(name: str, value: str) -> HashField:
    return HashField(name, value_s(value))


def field_n(name: str) -> HashField:
    return HashField(name, value_n())


def field_i(name: str, value: int) -> HashField:
    return HashField(name, value_i(value))


def field_b(name: str, value: bool) -> HashField:
    return HashField(name, value_b(value))


def field_h(name: str, value: str) -> HashField:
    return HashField(name, value_h(value))


def field_o(name: str, *fields: HashField) -> HashField:
    return HashField(name, value_o(*fields))


def field_l(name: str, *values: HashValue) -> HashField:
    return HashField(name, value_l(*values))


def hash_frames(contract: str, fields: Sequence[HashField]) -> str:
    """Hash an explicitly typed ordered field sequence using HashFrame/v1."""
    normalized_contract = _normalize_contract(contract)
    payload = bytearray(_PREFIX)
    payload.extend(normalized_contract.encode("ascii"))
    payload.extend(_RECORD_SEPARATOR)
    for field in fields:
        if not isinstance(field, HashField):
            raise TypeError("hash fields must be HashField instances")
        payload.extend(_encode_frame(field.name, field.value))
    return hashlib.sha256(payload).hexdigest()


def normalize_relative_path(value: str) -> str:
    """Return a safe NFC POSIX relative path for identity contracts."""
    text = unicodedata.normalize("NFC", str(value or ""))
    if not text or "\\" in text or text.startswith("/"):
        raise ValueError("path must be a non-empty POSIX relative path")
    path = PurePosixPath(text)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("path cannot contain empty, dot, or parent segments")
    normalized = path.as_posix()
    if normalized != text:
        raise ValueError("path must already be normalized")
    return normalized


def _normalize_contract(contract: str) -> str:
    normalized = unicodedata.normalize("NFC", str(contract or ""))
    if not _CONTRACT_RE.fullmatch(normalized):
        raise ValueError("invalid hash contract name")
    return normalized


def _normalize_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError("field name must be a string")
    return unicodedata.normalize("NFC", name)


def _encode_frame(name: str, value: HashValue) -> bytes:
    normalized_name = _normalize_name(name)
    if not isinstance(value, HashValue):
        raise TypeError("field value must be a HashValue")
    name_bytes = normalized_name.encode("utf-8")
    payload = _encode_value_payload(value)
    return b"".join(
        (
            str(len(name_bytes)).encode("ascii"),
            _UNIT_SEPARATOR,
            name_bytes,
            _UNIT_SEPARATOR,
            value.type.encode("ascii"),
            _UNIT_SEPARATOR,
            str(len(payload)).encode("ascii"),
            _UNIT_SEPARATOR,
            payload,
            _RECORD_SEPARATOR,
        )
    )


def _encode_value_payload(value: HashValue) -> bytes:
    if value.type == "s":
        if not isinstance(value.value, str):
            raise TypeError("s values must be strings")
        return unicodedata.normalize("NFC", value.value).encode("utf-8")
    if value.type == "n":
        if value.value is not None:
            raise TypeError("n values must be null")
        return b""
    if value.type == "i":
        if isinstance(value.value, bool) or not isinstance(value.value, int):
            raise TypeError("i values must be integers")
        return str(value.value).encode("ascii")
    if value.type == "b":
        if not isinstance(value.value, bool):
            raise TypeError("b values must be booleans")
        return b"1" if value.value else b"0"
    if value.type == "h":
        if not isinstance(value.value, str) or not _SHA256_RE.fullmatch(value.value):
            raise ValueError("h values must be lowercase SHA-256 hex")
        return value.value.encode("ascii")
    if value.type == "o":
        return _encode_object(value.value)
    if value.type == "l":
        return _encode_list(value.value)
    raise ValueError("unsupported hash value type")


def _encode_object(value: Any) -> bytes:
    if not isinstance(value, tuple) or not all(isinstance(item, HashField) for item in value):
        raise TypeError("o values must be a tuple of HashField")
    normalized: dict[str, HashField] = {}
    for field in value:
        name = _normalize_name(field.name)
        if name in normalized:
            raise ValueError("object field names must be unique after NFC normalization")
        normalized[name] = HashField(name, field.value)
    ordered = sorted(normalized.values(), key=lambda item: item.name.encode("utf-8"))
    return b"".join(_encode_frame(field.name, field.value) for field in ordered)


def _encode_list(value: Any) -> bytes:
    if not isinstance(value, tuple) or not all(isinstance(item, HashValue) for item in value):
        raise TypeError("l values must be a tuple of HashValue")
    return b"".join(_encode_frame(str(index), item) for index, item in enumerate(value))
