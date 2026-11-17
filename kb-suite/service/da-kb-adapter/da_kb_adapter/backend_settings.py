"""Contract backend settings (section 9.4) over DA's ``/api/settings`` routes (pure functions).

The settings are grouped as DA stores them; every field names the DA key it
maps to, so the schema, the values and the writes come from one table. The
model services and the per-file-type pipelines are listed from DA itself,
so their part of the schema follows DA's current configuration.

``raw`` holds one entry per DA settings route the backend serves (see
``offering.SETTINGS_ROUTES``); a group whose route is missing is left out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kb_provider_kit import ProviderError

MASK = "******"
ROLES = (
    ("main", "主模型", None),
    ("summarizer", "摘要模型", None),
    ("embedding", "向量模型", "改动后 DA 会给全部知识库重新生成向量，耗时并产生模型费用"),
    ("vlm", "视觉模型", None),
    ("audio_transcribe", "语音转写", None),
    ("video_understand", "视频理解", None),
)
PIPELINE_LABELS = {
    "mineru": "MinerU", "docling": "Docling", "docling-npu": "Docling（NPU）", "native": "内置解析",
    "asr": "语音识别", "text": "文本读取", "doc_converter": "格式转换", "image-vlm": "视觉模型",
    "image-ocr": "OCR", "video-vlm": "视觉模型", "video-asr": "语音识别",
}
FILE_TYPE_LABELS = {
    "pdf": "PDF", "docx": "Word", "pptx": "PowerPoint", "image": "图片", "audio": "音频", "video": "视频",
    "spreadsheet": "表格", "txt": "纯文本", "text": "Markdown / HTML", "doc_legacy": "旧版 Word",
}


@dataclass(frozen=True)
class Field:
    key: str
    da_key: str
    type: str
    title: str
    enum: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    description: str | None = None
    scale: int = 1  # DA value = contract value * scale

    def schema(self) -> dict:
        out: dict[str, Any] = {"type": self.type, "title": self.title}
        if self.enum:
            out["enum"] = list(self.enum)
            if self.labels:
                out["x-enum-labels"] = dict(zip(self.enum, self.labels, strict=True))
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.description:
            out["description"] = self.description
        return out


# group -> (title, DA settings route, fields); the routes merge a partial body into the stored config
CONFIGS: dict[str, tuple[str, str, tuple[Field, ...]]] = {
    "docling": ("Docling 解析", "/docling-config", (
        Field("ocr", "ocr", "boolean", "OCR 识别"),
        Field("ocr_engine", "ocr_engine", "string", "OCR 引擎", ("rapidocr", "easyocr", "tesseract")),
        Field("table_mode", "table_mode", "string", "表格识别", ("accurate", "fast"), ("精确", "快速")),
        Field("use_vlm", "use_vlm", "boolean", "用视觉模型解析"),
        Field("vlm_mode", "vlm_mode", "string", "视觉模型运行方式", ("inline", "api"), ("内置", "接口")),
        Field("vlm_api_url", "vlm_api_url", "string", "视觉模型接口地址"),
        Field("parallelism", "parallelism", "integer", "同时处理的文档数", minimum=1, maximum=32),
    )),
    "mineru": ("MinerU 解析", "/mineru-config", (
        Field("enabled", "enabled", "boolean", "启用"),
        Field("api_url", "apiUrl", "string", "服务地址"),
        Field("backend", "defaultBackend", "string", "后端"),
        Field("lang", "defaultLang", "string", "默认语言"),
        Field("formula", "formulaEnable", "boolean", "公式识别"),
        Field("table", "tableEnable", "boolean", "表格识别"),
        Field("image_analysis", "imageAnalysis", "boolean", "图片分析"),
        Field("timeout", "timeout", "integer", "超时（秒）", minimum=30, maximum=7200),
    )),
    "vlm": ("视觉模型调用", "/vlm-config", (
        Field("thinking", "thinkingEnabled", "boolean", "思考模式"),
        Field("max_tokens", "maxTokens", "integer", "最大输出 token", minimum=0, description="0 表示不限"),
        Field("timeout", "timeoutMs", "integer", "超时（秒）", minimum=10, maximum=3600, scale=1000),
    )),
}
PROCESSING = (
    Field("auto_process", "auto_process", "boolean", "上传后自动处理"),
    Field("parsing_mode", "mode", "string", "解析优先级", ("quality", "efficiency", "auto"), ("质量优先", "效率优先", "自动")),
)
PROVIDER_FIELDS = (
    Field("endpoint", "endpoint", "string", "接口地址"),
    Field("model", "model", "string", "模型"),
    Field("api_key", "apiKey", "string", "API Key"),
    Field("enabled", "enabled", "boolean", "启用"),
)
DIMENSION = Field("dimension", "dimension", "integer", "向量维度", minimum=1, maximum=8192)
EMBEDDING_NOTE = "改了模型、地址或维度要执行「重建向量」，否则语义检索不准"


def _providers(provider_settings: dict | None) -> list[dict]:
    return [p for p in (provider_settings or {}).get("providers") or [] if isinstance(p, dict) and p.get("id")]


def _provider_fields(provider: dict) -> tuple[Field, ...]:
    return PROVIDER_FIELDS + ((DIMENSION,) if "dimension" in provider else ())


def _choices(strategies: list | None) -> dict[str, list[str]]:
    """File type -> its pipelines in priority order, for file types with a choice."""

    out = {}
    for strategy in strategies or []:
        if not isinstance(strategy, dict) or not strategy.get("fileType"):
            continue
        entries = sorted((p for p in strategy.get("pipelines") or [] if isinstance(p, dict) and p.get("pipeline")),
                         key=lambda p: p.get("priority") or 0)
        names = list(dict.fromkeys(p["pipeline"] for p in entries))
        if len(names) > 1:
            out[strategy["fileType"]] = names
    return out


def _group(title: str, fields: tuple[Field, ...] | list[Field], description: str | None = None) -> dict:
    out: dict[str, Any] = {"type": "object", "title": title, "properties": {f.key: f.schema() for f in fields}}
    if description:
        out["description"] = description
    return out


def _role_field(key: str, title: str, note: str | None, providers: list[dict], current: str) -> Field:
    if not providers:
        return Field(key, key, "string", title, description=note)
    names = {p["id"]: str(p.get("name") or p["id"]) for p in providers}
    if current and current not in names:
        # a role still pointing at a removed service keeps its value selectable
        names[current] = current
    return Field(key, key, "string", title, ("", *names), ("未设置", *names.values()), description=note)


def _processing(raw: dict) -> tuple[Field, ...]:
    return tuple(f for f in PROCESSING if f.key in raw)


def schema(raw: dict) -> dict:
    props: dict[str, Any] = {}
    if "providers" in raw:
        provider_settings = raw.get("providers") or {}
        providers = _providers(provider_settings)
        defaults = provider_settings.get("defaults") or {}
        props["models"] = _group("模型分工", [_role_field(k, t, n, providers, str(defaults.get(k) or ""))
                                          for k, t, n in ROLES])
        services = {}
        for p in providers:
            group = _group(str(p.get("name") or p["id"]), _provider_fields(p),
                           EMBEDDING_NOTE if p["id"] == defaults.get("embedding") else None)
            group["properties"]["api_key"]["x-secret"] = True
            services[p["id"]] = group
        if services:
            props["providers"] = {"type": "object", "title": "模型服务", "properties": services}
    if _processing(raw):
        props["processing"] = _group("文档处理", _processing(raw))
    choices = _choices(raw.get("strategies"))
    if choices:
        props["pipelines"] = {"type": "object", "title": "首选解析管线", "properties": {
            file_type: {"type": "string", "title": FILE_TYPE_LABELS.get(file_type, file_type), "enum": names,
                        "x-enum-labels": {n: PIPELINE_LABELS.get(n, n) for n in names}}
            for file_type, names in choices.items()
        }}
    for group, (title, _, fields) in CONFIGS.items():
        if group in raw:
            props[group] = _group(title, fields)
    return {"type": "object", "properties": props}


def _from_da(f: Field, value: Any) -> Any:
    if value is None:
        return None
    if f.type == "integer" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) // f.scale
    return value


def _auto_process(value: Any) -> bool:
    # DA stores it as jsonb and reads it back as text; only "false" turns it off
    return str(value).lower() != "false" if value is not None else True


def values(raw: dict) -> dict:
    """Current values from DA's routes: ``providers`` (providers + defaults), ``strategies``, ``parsing_mode``,
    ``auto_process`` and one entry per group of ``CONFIGS``, each only when the backend serves it."""

    out: dict[str, Any] = {}
    if "providers" in raw:
        provider_settings = raw.get("providers") or {}
        defaults = provider_settings.get("defaults") or {}
        out["models"] = {k: defaults.get(k) or "" for k, _, _ in ROLES}
        providers = _providers(provider_settings)
        if providers:
            out["providers"] = {
                p["id"]: {f.key: (MASK if p.get("apiKey") else None) if f.key == "api_key" else _from_da(f, p.get(f.da_key))
                          for f in _provider_fields(p)}
                for p in providers
            }
    processing = {"auto_process": lambda: _auto_process(raw.get("auto_process")),
                  "parsing_mode": lambda: (raw.get("parsing_mode") or {}).get("mode") or "quality"}
    if _processing(raw):
        out["processing"] = {f.key: processing[f.key]() for f in _processing(raw)}
    choices = _choices(raw.get("strategies"))
    if choices:
        out["pipelines"] = {file_type: names[0] for file_type, names in choices.items()}
    for group, (_, _, fields) in CONFIGS.items():
        if group in raw:
            stored = raw.get(group) or {}
            out[group] = {f.key: _from_da(f, stored.get(f.da_key)) for f in fields}
    return out


def _bad(message: str) -> ProviderError:
    return ProviderError(400, "bad_request", message)


def _coerce(f: Field, value: Any, where: str) -> Any:
    if f.type == "boolean":
        if not isinstance(value, bool):
            raise _bad(f"{where} 应为布尔值")
        return value
    if f.type == "integer":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise _bad(f"{where} 应为整数")
        value = int(value)
        below = f.minimum is not None and value < f.minimum
        above = f.maximum is not None and value > f.maximum
        if below or above:
            raise _bad(f"{where} 超出范围")
        return value
    if not isinstance(value, str):
        raise _bad(f"{where} 应为字符串")
    value = value.strip()
    if f.enum and value not in f.enum:
        raise _bad(f"{where} 取值不合法：{value}")
    return value


def _object(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise _bad(f"{where} 应为对象")
    return value


def _fields_by_key(fields) -> dict[str, Field]:
    return {f.key: f for f in fields}


def changes(schema_: dict, current: dict, patch: dict) -> dict:
    """The validated fields of ``patch`` whose value differs from ``current``, grouped as in the schema.

    A secret field changes when it is sent as anything but ``"******"`` or null;
    ``""`` clears it. Unknown groups or fields are refused.
    """

    props = schema_.get("properties") or {}
    out: dict[str, Any] = {}
    for group, value in _object(patch, "设置").items():
        if group not in props:
            raise _bad(f"未知的设置项：{group}")
        value = _object(value, group)
        if group == "providers":
            for pid, fields in value.items():
                provider_schema = (props["providers"].get("properties") or {}).get(pid)
                if provider_schema is None:
                    raise _bad(f"没有这个模型服务：{pid}")
                found = _changed(_spec(provider_schema), (current.get("providers") or {}).get(pid) or {},
                                 _object(fields, pid), f"providers.{pid}")
                if found:
                    out.setdefault("providers", {})[pid] = found
            continue
        found = _changed(_spec(props[group]), current.get(group) or {}, value, group)
        if found:
            out[group] = found
    return out


def _spec(group_schema: dict) -> dict[str, tuple[Field, bool]]:
    """Fields of a schema group, rebuilt from the schema so dynamic enums are checked too."""

    out = {}
    for key, s in (group_schema.get("properties") or {}).items():
        enum = tuple(s.get("enum") or ())
        out[key] = (Field(key, key, s.get("type") or "string", s.get("title") or key, enum,
                          minimum=s.get("minimum"), maximum=s.get("maximum")), bool(s.get("x-secret")))
    return out


def _changed(spec: dict[str, tuple[Field, bool]], current: dict, patch: dict, where: str) -> dict:
    out = {}
    for key, value in patch.items():
        if key not in spec:
            raise _bad(f"未知的设置项：{where}.{key}")
        f, secret = spec[key]
        if secret:
            if value is None or value == MASK:
                continue
            out[key] = _coerce(f, value, f"{where}.{key}")
            continue
        if value is None:
            continue
        value = _coerce(f, value, f"{where}.{key}")
        if value != current.get(key):
            out[key] = value
    return out


def config_body(group: str, fields: dict) -> dict:
    """The partial body of a ``CONFIGS`` group's DA route."""

    spec = _fields_by_key(CONFIGS[group][2])
    return {spec[k].da_key: v * spec[k].scale if spec[k].type == "integer" else v for k, v in fields.items()}


def provider_body(stored: dict, fields: dict) -> dict:
    """The whole provider for DA's ``PUT /providers/:id`` (DA replaces the row).

    ``stored`` comes from DA with its keys masked; DA maps a masked key back to
    the stored one, so an unchanged key is sent back as it came.
    """

    spec = {f.key: f for f in PROVIDER_FIELDS + (DIMENSION,)}
    body = dict(stored)
    for key, value in fields.items():
        body[spec[key].da_key] = value
    return body


def reorder(strategies: list, first: dict[str, str]) -> list:
    """DA's pipeline strategies with the chosen pipeline of each file type moved to priority 1."""

    out = []
    for strategy in strategies:
        chosen = first.get(strategy.get("fileType")) if isinstance(strategy, dict) else None
        if not chosen:
            out.append(strategy)
            continue
        entries = sorted(strategy.get("pipelines") or [], key=lambda p: p.get("priority") or 0)
        entries = [p for p in entries if p.get("pipeline") == chosen] + [p for p in entries if p.get("pipeline") != chosen]
        out.append({**strategy, "pipelines": [{**p, "priority": i + 1} for i, p in enumerate(entries)]})
    return out
