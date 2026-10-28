"""Per-knowledge-base settings (``knowledge_bases.config``).

Sections follow WeKnora's knowledge-base editor (chunking, parser rules,
summary, question generation, wiki, profile) plus the retrieval knobs WeKnora
keeps on agents and tenants; kb-service has no agents of its own, so they live
on the knowledge base and can be overridden per search request.

Stored JSON may be partial or in the original flat shape
(``{"chunk_size": .., "chunk_overlap": ..}``); :func:`load` always returns a
complete, validated config. Explicit zeros and ``false`` are kept as given.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .config import get_settings

DEFAULT_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ", "，", ", ", " "]


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ChunkingConfig(_Section):
    strategy: Literal["heading", "separator"] = "heading"
    chunk_size: int = Field(512, ge=100, le=4000)
    chunk_overlap: int = Field(80, ge=0, le=1000)
    separators: list[str] = Field(default_factory=lambda: list(DEFAULT_SEPARATORS))

    @model_validator(mode="after")
    def _check(self) -> "ChunkingConfig":
        if self.chunk_overlap > self.chunk_size // 2:
            raise ValueError("chunk_overlap 不能超过 chunk_size 的一半")
        self.separators = [s for s in self.separators if s] or list(DEFAULT_SEPARATORS)
        return self


class ParsingConfig(_Section):
    # file extension -> engine ("auto" keeps kb-service's own routing)
    engine_rules: dict[str, str] = Field(default_factory=dict)


class IndexingConfig(_Section):
    # Off = a wiki-only knowledge base: chunks are kept for citations but not
    # embedded and not returned by search.
    rag_enabled: bool = True


class SummaryConfig(_Section):
    enabled: bool = True
    model: str = ""
    instructions: str = Field("", max_length=4000)
    table_instructions: str = Field("", max_length=4000)
    input_chars: int = Field(6000, ge=1000, le=30000)


class QuestionConfig(_Section):
    enabled: bool = False
    count: int = Field(3, ge=1, le=10)
    instructions: str = Field("", max_length=4000)
    model: str = ""


class WikiConfig(_Section):
    enabled: bool = False
    model: str = ""
    granularity: Literal["focused", "standard", "exhaustive"] = "standard"
    extraction_instructions: str = Field("", max_length=4000)
    content_instructions: str = Field("", max_length=4000)
    max_pages_per_doc: int = Field(0, ge=0, le=200)
    include_in_search: bool = True
    boost: float = Field(1.3, ge=1.0, le=3.0)
    max_search_results: int = Field(2, ge=0, le=20)


class ProfileConfig(_Section):
    enabled: bool = False
    model: str = ""
    instructions: str = Field("", max_length=2000)


class RetrievalConfig(_Section):
    vector_enabled: bool = True
    keyword_enabled: bool = True
    use_questions: bool = True
    vector_top_k: int = Field(40, ge=1, le=200)
    keyword_top_k: int = Field(40, ge=1, le=200)
    vector_threshold: float = Field(0.2, ge=0.0, le=1.0)
    keyword_threshold: float = Field(0.0, ge=0.0, le=1.0)
    rrf_k: int = Field(60, ge=1, le=1000)
    vector_weight: float = Field(1.0, ge=0.0, le=5.0)
    keyword_weight: float = Field(1.0, ge=0.0, le=5.0)
    query_expansion: bool = False
    rerank_enabled: bool = True
    rerank_candidates: int = Field(30, ge=1, le=200)
    rerank_threshold: float = Field(0.2, ge=-10.0, le=10.0)
    composite_score: bool = True
    mmr_enabled: bool = True
    mmr_lambda: float = Field(0.7, ge=0.0, le=1.0)
    merge_adjacent: bool = True
    expand_enabled: bool = True
    expand_min_chars: int = Field(350, ge=0, le=4000)
    expand_max_chars: int = Field(850, ge=100, le=8000)
    parent_child: bool = False
    parent_chunk_size: int = Field(2048, ge=512, le=8192)

    @model_validator(mode="after")
    def _check(self) -> "RetrievalConfig":
        if self.expand_max_chars < self.expand_min_chars:
            raise ValueError("expand_max_chars 不能小于 expand_min_chars")
        return self


class KbConfig(_Section):
    language: str = Field("中文", max_length=40)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    parsing: ParsingConfig = Field(default_factory=ParsingConfig)
    indexing: IndexingConfig = Field(default_factory=IndexingConfig)
    summary: SummaryConfig = Field(default_factory=SummaryConfig)
    questions: QuestionConfig = Field(default_factory=QuestionConfig)
    wiki: WikiConfig = Field(default_factory=WikiConfig)
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)


SECTIONS = tuple(k for k in KbConfig.model_fields if k != "language")

# Per-document overrides accepted at upload time (WeKnora's upload dialog).
OVERRIDABLE = {
    "chunking": {"strategy", "chunk_size", "chunk_overlap", "separators"},
    "parsing": {"engine_rules"},
    "summary": {"enabled"},
    "questions": {"enabled", "count"},
    "wiki": {"enabled"},
}


class ConfigError(ValueError):
    pass


def _legacy(raw: dict) -> dict:
    data = {k: v for k, v in raw.items() if k in SECTIONS or k == "language"}
    flat = {k: raw[k] for k in ("chunk_size", "chunk_overlap") if k in raw and raw[k] is not None}
    if flat:
        data["chunking"] = {**flat, **(data.get("chunking") or {})}
    return data


def deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key != "engine_rules":
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _defaults() -> dict:
    """Code defaults with the deployment's env settings layered in."""

    settings = get_settings()
    base = KbConfig().model_dump()
    base["chunking"]["chunk_size"] = settings.chunk_size
    base["chunking"]["chunk_overlap"] = min(settings.chunk_overlap, settings.chunk_size // 2)
    base["summary"]["input_chars"] = settings.summary_input_chars
    base["retrieval"]["vector_top_k"] = settings.retrieval_candidates
    base["retrieval"]["keyword_top_k"] = settings.retrieval_candidates
    base["retrieval"]["rerank_candidates"] = settings.rerank_candidates
    return base


def defaults() -> KbConfig:
    return KbConfig.model_validate(_defaults())


def load(raw: dict | None, overrides: dict | None = None) -> KbConfig:
    """Stored (possibly partial or legacy) JSON -> complete config.

    Invalid stored values fall back to defaults section by section rather than
    failing ingestion or search.
    """

    merged = deep_merge(_defaults(), _legacy(raw or {}))
    if overrides:
        rules = (overrides.get("parsing") or {}).get("engine_rules")
        if isinstance(rules, dict):
            # per-upload rules add to the knowledge base's rules instead of replacing them
            overrides = deep_merge(
                overrides, {"parsing": {"engine_rules": {**merged["parsing"]["engine_rules"], **rules}}}
            )
        merged = deep_merge(merged, overrides)
    try:
        return KbConfig.model_validate(merged)
    except ValidationError:
        base = _defaults()
        out = dict(base)
        for key in (*SECTIONS, "language"):
            candidate = deep_merge(base, {key: merged.get(key, base[key])})
            try:
                KbConfig.model_validate(candidate)
                out[key] = candidate[key]
            except ValidationError:
                continue
        return KbConfig.model_validate(out)


def apply_patch(raw: dict | None, patch: dict) -> dict:
    """Validate a partial update and return the JSON to store (full config)."""

    unknown = set(patch) - set(SECTIONS) - {"language"}
    if unknown:
        raise ConfigError(f"未知配置项: {', '.join(sorted(unknown))}")
    current = load(raw).model_dump()
    try:
        return KbConfig.model_validate(deep_merge(current, patch)).model_dump()
    except ValidationError as exc:
        raise ConfigError(_first_error(exc)) from exc


def validate_overrides(overrides: dict | None) -> dict:
    """Keep only per-document overridable keys; raise on invalid values."""

    if not overrides:
        return {}
    clean: dict[str, dict] = {}
    for section, keys in OVERRIDABLE.items():
        values = overrides.get(section)
        if isinstance(values, dict):
            picked = {k: v for k, v in values.items() if k in keys and v is not None}
            if picked:
                clean[section] = picked
    try:
        KbConfig.model_validate(deep_merge(_defaults(), clean))
    except ValidationError as exc:
        raise ConfigError(_first_error(exc)) from exc
    return clean


def retrieval_overrides(options: dict | None) -> dict:
    if not options:
        return {}
    fields = RetrievalConfig.model_fields
    return {k: v for k, v in options.items() if k in fields and v is not None}


def merge_retrieval(configs: list[KbConfig], options: dict | None = None) -> RetrievalConfig:
    """Search-wide knobs when several knowledge bases are searched together.

    Per-hit thresholds still come from each hit's own knowledge base (see
    :func:`threshold_for`); this only decides counts and switches: counts take
    the maximum, switches are on when any knowledge base has them on.
    """

    if not configs:
        merged = defaults().retrieval.model_dump()
    else:
        dumps = [c.retrieval.model_dump() for c in configs]
        merged = dict(dumps[0])
        for key, value in dumps[0].items():
            values = [d[key] for d in dumps]
            if isinstance(value, bool):
                merged[key] = any(values)
            elif isinstance(value, int):
                merged[key] = max(values)
    merged.update(retrieval_overrides(options))
    try:
        return RetrievalConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(_first_error(exc)) from exc


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    where = ".".join(str(p) for p in err.get("loc", ()))
    return f"{where}: {err.get('msg', 'invalid')}" if where else str(err.get("msg", "invalid"))


def describe() -> dict[str, Any]:
    """Defaults plus field metadata for the settings UI."""

    settings = get_settings()
    return {
        "defaults": _defaults(),
        "models": {
            "chat": chat_models(),
            "default_chat": settings.summary_model,
            "embedding": settings.embed_model,
            "embedding_dim": settings.embed_dim,
            "rerank": settings.rerank_model if settings.rerank_url else None,
        },
        "overridable": {k: sorted(v) for k, v in OVERRIDABLE.items()},
    }


def chat_models() -> list[str]:
    settings = get_settings()
    names = [m.strip() for m in settings.chat_models.split(",") if m.strip()]
    if settings.summary_model not in names:
        names.insert(0, settings.summary_model)
    return names
