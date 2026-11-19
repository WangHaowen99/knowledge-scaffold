"""数据源连接器的清单与连接字段（ADR-0103；design/data-sources.md 第 1、4 节）：内核、网关、插件检查共用一份。

连接器网关镜像也带这个模块，不能导入后端其他模块，只用标准库。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

DATASOURCE_SUFFIX = ".datasource"
DATASOURCE_FORMAT = "onto-datasource/1"

FIELD_TYPES = ("text", "number", "host", "port", "secret", "select")
MAX_FIELDS = 20
MAX_OPTIONS = 20
MAX_TEXT_VALUE = 500
MAX_SECRET_VALUE = 4096

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_ENTRY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*\.py$")
_VENDOR_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,39}$")
# 主机：域名或 IP 字面量，不收端口、路径、用户信息
_HOST_RE = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|[0-9A-Fa-f:.]{2,45})$")
_SECRET_REF_RE = re.compile(r"^ds-[0-9a-f]{12}$")


class SpecError(ValueError):
    """清单里的 `source` 段写得不对：消息给包作者看。"""


@dataclass(frozen=True)
class SourceField:
    key: str
    label: str
    type: str
    required: bool = False
    default: str | int | None = None
    placeholder: str = ""
    options: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "type": self.type, "required": self.required,
                "default": self.default, "placeholder": self.placeholder,
                "options": [{"value": value, "label": label} for value, label in self.options]}


@dataclass(frozen=True)
class SourceInfo:
    """数据源连接器的描述，来自包清单的 `source` 段。"""

    id: str
    version: str
    label: str
    description: str
    entry: str
    license_note: str
    fields: tuple[SourceField, ...]
    vendor: str = ""
    official: bool = False
    extra: dict = field(default_factory=dict, compare=False)

    @property
    def secret_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.fields if item.type == "secret")

    def field_of_type(self, kind: str) -> SourceField | None:
        return next((item for item in self.fields if item.type == kind), None)

    def to_dict(self) -> dict:
        return {"id": self.id, "version": self.version, "label": self.label, "description": self.description,
                "license": self.license_note, "official": self.official, "fields": [item.to_dict() for item in self.fields]}


def _text(value, limit: int, what: str, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise SpecError(f"{what} 要是文字")
    value = value.strip()
    if required and not value:
        raise SpecError(f"{what} 不能为空")
    if len(value) > limit:
        raise SpecError(f"{what} 最多 {limit} 字")
    return value


def _field(raw, index: int) -> SourceField:
    if not isinstance(raw, dict):
        raise SpecError(f"fields[{index}] 要是对象")
    key = raw.get("key")
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise SpecError(f"fields[{index}].key 要匹配 {_KEY_RE.pattern}")
    kind = raw.get("type")
    if kind not in FIELD_TYPES:
        raise SpecError(f"fields[{index}].type 只能是 {'、'.join(FIELD_TYPES)}")
    label = _text(raw.get("label"), 20, f"fields[{index}].label", required=True)
    placeholder = _text(raw.get("placeholder"), 60, f"fields[{index}].placeholder")
    default = raw.get("default")
    if default is not None:
        if kind == "secret":
            raise SpecError(f"fields[{index}]：secret 字段不能有 default")
        if isinstance(default, bool) or not isinstance(default, (str, int)):
            raise SpecError(f"fields[{index}].default 要是字符串或整数")
    options: list[tuple[str, str]] = []
    if kind == "select":
        items = raw.get("options")
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_OPTIONS:
            raise SpecError(f"fields[{index}].options 要有 1～{MAX_OPTIONS} 项")
        for position, item in enumerate(items):
            if not isinstance(item, dict):
                raise SpecError(f"fields[{index}].options[{position}] 要是对象")
            value = _text(item.get("value"), 60, f"fields[{index}].options[{position}].value", required=True)
            options.append((value, _text(item.get("label"), 20, f"fields[{index}].options[{position}].label",
                                         required=True)))
    required = raw.get("required", False)
    if not isinstance(required, bool):
        raise SpecError(f"fields[{index}].required 要是 true 或 false")
    return SourceField(key=key, label=label, type=kind, required=required, default=default,
                       placeholder=placeholder, options=tuple(options))


def parse_spec(manifest: dict, *, official: bool = False) -> SourceInfo:
    """清单 → SourceInfo；写得不对抛 SpecError。只校验 `source` 段和名字、版本，不看入口文件在不在。"""
    spec = manifest.get("source")
    if not isinstance(spec, dict):
        raise SpecError("缺少 source 段")
    if manifest.get("converter") is not None:
        raise SpecError("converter 和 source 只能写一个")
    entry = spec.get("entry")
    if not isinstance(entry, str) or not _ENTRY_RE.match(entry):
        raise SpecError("source.entry 要是包根目录下的单个 .py 文件")
    vendor = spec.get("vendor") or ""
    if vendor and (not isinstance(vendor, str) or not _VENDOR_RE.match(vendor)):
        raise SpecError("source.vendor 要是包根目录下的一个目录名")
    raw_fields = spec.get("fields")
    if not isinstance(raw_fields, list) or not 1 <= len(raw_fields) <= MAX_FIELDS:
        raise SpecError(f"source.fields 要有 1～{MAX_FIELDS} 项")
    fields = tuple(_field(item, index) for index, item in enumerate(raw_fields))
    keys = [item.key for item in fields]
    if len(set(keys)) != len(keys):
        raise SpecError("source.fields 的 key 重复")
    for kind in ("host", "port"):
        if sum(1 for item in fields if item.type == kind) > 1:
            raise SpecError(f"{kind} 类字段最多一个")
    return SourceInfo(
        id=str(manifest.get("name") or ""), version=str(manifest.get("version") or "0.0.0"),
        label=_text(manifest.get("label"), 40, "label") or str(manifest.get("name") or ""),
        description=_text(manifest.get("description"), 200, "description"),
        entry=entry, license_note=_text(spec.get("license"), 200, "source.license"),
        fields=fields, vendor=vendor, official=official,
    )


# ---------------------------------------------------------------------------
# 连接配置
# ---------------------------------------------------------------------------


def coerce_config(info: SourceInfo, config, secrets=None, *, stored_secrets: tuple = ()) -> tuple[dict, dict, dict]:
    """按字段校验、转换。返回 (config, secrets, errors)：config 只有非密钥字段（缺省值已补），secrets 只有给了的密钥字段，
    errors 是 {key: "required"|"invalid"}。`stored_secrets` 是已存过的密钥字段名，这些字段没给也不算缺。"""
    config = config if isinstance(config, dict) else {}
    secrets = secrets if isinstance(secrets, dict) else {}
    out: dict = {}
    given_secrets: dict = {}
    errors: dict = {}
    known = {item.key for item in info.fields}
    for key in list(config) + list(secrets):
        if key not in known:
            errors[key] = "invalid"
    for item in info.fields:
        if item.type == "secret":
            if item.key in config:
                # 密钥放进了 config：会写进数据源资料，一律拒绝
                errors[item.key] = "invalid"
                continue
            value = secrets.get(item.key)
            if value in (None, ""):
                if item.required and item.key not in stored_secrets:
                    errors[item.key] = "required"
                continue
            if not isinstance(value, str) or len(value) > MAX_SECRET_VALUE:
                errors[item.key] = "invalid"
                continue
            given_secrets[item.key] = value
            continue
        if item.key in secrets:
            errors[item.key] = "invalid"
        value = config.get(item.key)
        if value is None or (isinstance(value, str) and not value.strip()):
            value = item.default
        if value is None or value == "":
            if item.required:
                errors[item.key] = "required"
            elif item.type in ("text", "host", "select"):
                out[item.key] = ""
            continue
        converted = _convert(item, value)
        if converted is None:
            errors[item.key] = "invalid"
        else:
            out[item.key] = converted
    return out, given_secrets, errors


def _convert(item: SourceField, value):
    if isinstance(value, bool):
        return None
    if item.type in ("number", "port"):
        try:
            number = int(str(value).strip())
        except ValueError:
            return None
        if item.type == "port" and not 1 <= number <= 65535:
            return None
        return number
    if not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    if len(text) > MAX_TEXT_VALUE or any(ord(char) < 32 for char in text):
        return None
    if item.type == "host" and not _HOST_RE.match(text):
        return None
    if item.type == "select" and text not in {option for option, _ in item.options}:
        return None
    return text


def egress(info: SourceInfo, config: dict) -> list[dict]:
    """出口白名单：`host` 类字段的值配 `port` 类字段的值；没有 host 字段时为空（连接器不连网络）。"""
    host_field, port_field = info.field_of_type("host"), info.field_of_type("port")
    if host_field is None:
        return []
    host = str(config.get(host_field.key) or "").strip()
    port = config.get(port_field.key) if port_field else None
    if not host or not isinstance(port, int):
        return []
    return [{"host": host.strip("[]"), "port": port}]


# ---------------------------------------------------------------------------
# 数据源资料（resource/excel/<名称>.datasource）
# ---------------------------------------------------------------------------


def is_datasource(path: str) -> bool:
    return str(path or "").lower().endswith(DATASOURCE_SUFFIX)


def valid_secret_ref(value) -> bool:
    return isinstance(value, str) and bool(_SECRET_REF_RE.match(value))


def dump_document(connector: str, config: dict, secret_ref: str, secret_fields) -> bytes:
    document = {"format": DATASOURCE_FORMAT, "connector": connector, "config": dict(config),
                "secret": secret_ref, "secret_fields": sorted(set(secret_fields))}
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def load_document(data: bytes | str) -> dict | None:
    """数据源资料 → {connector, config, secret, secret_fields}；格式不对时 None。"""
    try:
        document = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("format") != DATASOURCE_FORMAT:
        return None
    connector, config = document.get("connector"), document.get("config")
    if not isinstance(connector, str) or not connector or not isinstance(config, dict):
        return None
    secret = document.get("secret")
    fields = document.get("secret_fields") if isinstance(document.get("secret_fields"), list) else []
    return {"connector": connector, "config": config, "secret": secret if valid_secret_ref(secret) else "",
            "secret_fields": [str(item) for item in fields if isinstance(item, str)]}


def info_from_dict(data: dict) -> SourceInfo | None:
    """网关 /v1/providers 的 sources 一项 → SourceInfo（内核按网关里实际加载的那一版校验字段）；不合法时 None。"""
    if not isinstance(data, dict):
        return None
    try:
        fields = tuple(_field(item, index) for index, item in enumerate(data.get("fields") or []))
    except SpecError:
        return None
    if not fields or not data.get("id"):
        return None
    return SourceInfo(id=str(data["id"]), version=str(data.get("version") or ""), label=str(data.get("label") or data["id"]),
                      description=str(data.get("description") or ""), entry="", license_note=str(data.get("license") or ""),
                      fields=fields, official=bool(data.get("official")))
