"""Application configuration helpers."""

from dataclasses import dataclass, field
import posixpath
from pathlib import Path
import re
from typing import Any, Dict, Optional, Set

from config.system_config import (
    SystemConfigValidationError,
    load_system_config,
    resolve_llm_profile_config,
)
from config.ops_config import load_ops_config


@dataclass
class LLMConfig:
    """Configuration for LLM service."""

    base_url: Optional[str]
    model: Optional[str]
    api_key: Optional[str]
    timeout: int
    max_tokens: int
    temperature: float
    provider: str = "openai_compatible"
    profile_id: Optional[str] = None
    profile_name: Optional[str] = None

    @classmethod
    def from_mapping(
        cls,
        payload: Dict[str, Any],
        *,
        fallback: Optional["LLMConfig"] = None,
    ) -> "LLMConfig":
        """Build a config from a normalized model profile or section."""
        def pick_text(name: str, fallback_value: Optional[str]) -> Optional[str]:
            value = payload.get(name)
            if value is None or value == "":
                return fallback_value
            return str(value)

        def pick_number(name: str, fallback_value):
            value = payload.get(name)
            return fallback_value if value is None else value

        return cls(
            base_url=pick_text("base_url", fallback.base_url if fallback else None),
            model=pick_text("model", fallback.model if fallback else None),
            api_key=pick_text("api_key", fallback.api_key if fallback else None),
            timeout=int(pick_number("timeout", fallback.timeout if fallback else 600)),
            max_tokens=int(pick_number("max_tokens", fallback.max_tokens if fallback else 32768)),
            temperature=float(pick_number("temperature", fallback.temperature if fallback else 0.7)),
            provider=pick_text("provider", fallback.provider if fallback else "openai_compatible") or "openai_compatible",
            profile_id=pick_text("id", fallback.profile_id if fallback else None),
            profile_name=pick_text("name", fallback.profile_name if fallback else None),
        )

    @classmethod
    def from_profile_id(cls, profile_id: Optional[str] = None) -> "LLMConfig":
        """Load a model profile by id, falling back to the configured default."""
        return cls.from_mapping(resolve_llm_profile_config(profile_id))

    @classmethod
    def from_env(
        cls,
        prefix: str = "LLM",
        fallback: Optional["LLMConfig"] = None,
    ) -> "LLMConfig":
        """Load LLM configuration from system config with env/default fallback."""
        from config.system_config import resolve_role_config

        is_task = prefix.rstrip("_").upper() == "TASK_LLM"
        if is_task:
            base = fallback or cls.from_env("LLM")
            try:
                return cls.from_mapping(resolve_role_config("task"), fallback=base)
            except SystemConfigValidationError:
                return base

        try:
            return cls.from_mapping(resolve_role_config("main"), fallback=fallback)
        except SystemConfigValidationError:
            if fallback is not None:
                return fallback
            return cls(
                base_url=None,
                model=None,
                api_key=None,
                timeout=600,
                max_tokens=32768,
                temperature=0.7,
            )


@dataclass
class DirectExecutionConfig:
    """Configuration for the direct script execution branch."""

    enabled: bool = False
    selected_ontology_only: bool = True
    confidence_threshold: float = 0.85

    @classmethod
    def from_env(cls) -> "DirectExecutionConfig":
        config = load_system_config()["direct_execution"]
        return cls(
            enabled=bool(config["enabled"]),
            selected_ontology_only=bool(config["selected_ontology_only"]),
        )


@dataclass
class WebSearchConfig:
    """Configuration for the public web search tool."""

    enabled: bool = False
    provider: str = "tavily"
    base_url: str = "https://api.tavily.com"
    api_key: str = ""
    timeout_seconds: int = 20
    default_max_results: int = 5
    max_results_limit: int = 10
    search_depth: str = "basic"
    allowed_domains: str = ""
    blocked_domains: str = ""

    @classmethod
    def from_env(cls) -> "WebSearchConfig":
        config = load_system_config()["web_search"]
        return cls(
            enabled=bool(config["enabled"]),
            provider=str(config["provider"] or "tavily").strip().lower(),
            base_url=str(config["base_url"] or "https://api.tavily.com").strip().rstrip("/"),
            api_key=str(config.get("api_key") or "").strip(),
            timeout_seconds=max(1, int(config["timeout_seconds"])),
            default_max_results=max(1, int(config["default_max_results"])),
            max_results_limit=max(1, int(config["max_results_limit"])),
            search_depth=str(config["search_depth"] or "basic").strip().lower(),
            allowed_domains=str(config.get("allowed_domains") or "").strip(),
            blocked_domains=str(config.get("blocked_domains") or "").strip(),
        )

    def to_provider_config(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "timeout_seconds": self.timeout_seconds,
            "search_depth": self.search_depth,
        }


@dataclass
class LimitsConfig:
    """Business/runtime limits shared by route and storage boundaries.

    ``workspace_max_count`` is retained as a compatibility field name, but its
    scope is the number of workspaces owned by one user.
    """

    run_tps: int = 50
    user_max_count: int = 0
    workspace_max_count: int = 50
    ontology_max_count_per_workspace: int = 10
    ontology_object_max_count: int = 200
    ontology_operation_max_count: int = 100
    entity_attribute_max_count: int = 100
    data_connection_configs_max_per_workspace: int = 10
    model_profile_max_count: int = 10
    model_output_token_limit: int = 100000
    session_max_per_user_workspace: int = 100
    operation_max_chars: int = 15000
    tool_output_max_bytes: int = 10 * 1024 * 1024
    tool_output_run_max_bytes: int = 64 * 1024 * 1024
    tool_output_session_max_bytes: int = 256 * 1024 * 1024
    tool_output_run_max_count: int = 64
    tool_output_session_max_count: int = 256
    agent_log_retention_days: int = 180
    agent_log_compress_old: bool = True

    @classmethod
    def from_env(cls) -> "LimitsConfig":
        config = load_ops_config()["limits"]
        return cls(
            run_tps=max(1, int(config["run_tps"])),
            user_max_count=0,
            workspace_max_count=max(1, int(config["workspace_max_count"])),
            ontology_max_count_per_workspace=max(1, int(config["ontology_max_count_per_workspace"])),
            ontology_object_max_count=max(1, int(config["ontology_object_max_count"])),
            ontology_operation_max_count=max(1, int(config["ontology_operation_max_count"])),
            entity_attribute_max_count=max(1, int(config["entity_attribute_max_count"])),
            data_connection_configs_max_per_workspace=max(1, int(config["data_connection_configs_max_per_workspace"])),
            model_profile_max_count=max(1, int(config["model_profile_max_count"])),
            model_output_token_limit=max(1, int(config["model_output_token_limit"])),
            session_max_per_user_workspace=max(1, int(config["session_max_per_user_workspace"])),
            operation_max_chars=max(1, int(config["operation_max_chars"])),
            tool_output_max_bytes=max(1, int(config["tool_output_max_bytes"])),
            tool_output_run_max_bytes=max(1, int(config["tool_output_run_max_bytes"])),
            tool_output_session_max_bytes=max(1, int(config["tool_output_session_max_bytes"])),
            tool_output_run_max_count=max(1, int(config["tool_output_run_max_count"])),
            tool_output_session_max_count=max(1, int(config["tool_output_session_max_count"])),
            agent_log_retention_days=max(1, int(config["agent_log_retention_days"])),
            agent_log_compress_old=bool(config["agent_log_compress_old"]),
        )


@dataclass
class AppConfig:
    """Container for runtime configuration."""

    llm: LLMConfig
    task_llm: LLMConfig
    chat_storage_dir: str
    execution_storage_dir: str  # Workflow execution records
    project_root: str
    auth_users_file: str
    auth_secret: Optional[str]
    auth_token_ttl_seconds: int
    auth_server_url: str = ""
    auth_server_admin_token: str = ""
    auth_server_audience: str = "meta-wise"
    auth_server_issuer: str = ""

    @staticmethod
    def _is_windows_absolute_path(value: str) -> bool:
        return bool(re.match(r"^[A-Za-z]:[\\/]", value)) or value.startswith("\\\\")

    @staticmethod
    def _resolve_storage_path(
        value: str,
        *,
        project_root: str,
        default_relative: str | None = None,
    ) -> str:
        project_root_raw = str(project_root or "").strip()
        if not project_root_raw:
            return ""
        if project_root_raw.startswith("/"):
            base = posixpath.normpath(project_root_raw)
        else:
            base = str(Path(project_root_raw).expanduser().resolve())
        raw = str(value or "").strip()
        if not raw:
            raw = str(default_relative or "").strip()
            if not raw:
                return base
        if raw.startswith("/"):
            return posixpath.normpath(raw)
        if AppConfig._is_windows_absolute_path(raw):
            return str(Path(raw).expanduser().resolve())
        if base.startswith("/"):
            return posixpath.normpath(posixpath.join(base, raw))
        return str((Path(base) / raw).resolve())

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Load configuration from system config with env/default fallback."""
        config = load_system_config()
        llm = LLMConfig.from_env()
        task_llm = LLMConfig.from_env(prefix="TASK_LLM", fallback=llm)
        storage = config["storage"]
        auth = config["auth"]
        project_root = cls._resolve_storage_path(storage["project_root"], project_root=storage["project_root"])
        return cls(
            llm=llm,
            task_llm=task_llm,
            chat_storage_dir=cls._resolve_storage_path(
                storage["chat_storage_dir"],
                project_root=project_root,
                default_relative="data/chats",
            ),
            execution_storage_dir=cls._resolve_storage_path(
                storage["execution_storage_dir"],
                project_root=project_root,
                default_relative="data/executions",
            ),
            project_root=project_root,
            auth_users_file=cls._resolve_storage_path(
                storage["auth_users_file"],
                project_root=project_root,
                default_relative="users.json",
            ),
            auth_secret=auth.get("auth_secret") or None,
            auth_token_ttl_seconds=int(auth["auth_token_ttl_seconds"]),
            auth_server_url=str(auth.get("auth_server_url") or "").strip(),
            auth_server_admin_token=str(auth.get("auth_server_admin_token") or "").strip(),
            auth_server_audience=str(auth.get("auth_server_audience") or "meta-wise").strip(),
            auth_server_issuer=str(auth.get("auth_server_issuer") or "").strip(),
        )


@dataclass
class AgentConfig:
    """Configuration for agent execution parameters.

    Centralizes all hard-coded values scattered across the codebase:
    - Tool execution settings
    - Sub-agent parameters
    - LLM retry/round limits
    - Orchestrator settings
    """

    # Env-driven fields — defaults live in from_env()
    llm_connection_max_retries: int
    main_agent_max_iterations: int
    sub_agent_max_iterations: int
    sub_agent_max_concurrency: int
    sub_agent_max_depth: int
    parallel_default_max_workers: int
    context_max_rounds: int
    tool_timeout_seconds: int = 600
    shutdown_total_deadline_seconds: float = 30.0
    sse_subscriber_queue_size: int = 512
    sse_max_subscribers_per_run: int = 3
    sse_max_subscribers_total: int = 100
    completed_replay_ttl_seconds: float = 120.0
    short_io_workers: int = 8
    short_io_max_pending: int = 64
    short_io_capacity_wait_seconds: float = 1.0
    short_io_operation_timeout_seconds: float = 30.0
    external_io_workers: int = 16
    external_io_max_pending: int = 16
    external_io_capacity_wait_seconds: float = 0.1
    external_io_operation_timeout_seconds: float = 300.0
    tool_io_workers: int = 8
    tool_io_max_pending: int = 16
    tool_io_capacity_wait_seconds: float = 0.1
    tool_io_operation_timeout_seconds: float = 600.0
    runtime_warmup_enabled: bool = True
    execute_script_timeout: int = 300
    deterministic_execution_enabled: bool = False

    # Operation evolution
    evolution_min_occurrences: int = 3
    evolution_min_applied: int = 2
    evolution_max_rejection: int = 0
    evolution_passive_reflection_enabled: bool = False
    evolution_patch_auto_apply: bool = False
    evolution_patch_cooldown_sec: int = 3600
    evolution_sweep_interval_sec: int = 1800

    # Pure-code constants (not overridable by env) — defaults live on the field.
    orchestration_tools: Set[str] = field(default_factory=lambda: {
        "spawn_sub_agent",
    })
    memory_tools: Set[str] = field(default_factory=lambda: {
        "memory",
    })
    direct_execution: DirectExecutionConfig = field(default_factory=DirectExecutionConfig)
    web_search: WebSearchConfig = field(default_factory=WebSearchConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)

    @classmethod
    def from_env(cls) -> "AgentConfig":
        """Load agent configuration from system config with env/default fallback."""
        config = load_system_config()["agent"]
        return cls(
            llm_connection_max_retries=int(config["llm_connection_max_retries"]),
            main_agent_max_iterations=int(config["main_agent_max_iterations"]),
            sub_agent_max_iterations=int(config["sub_agent_max_iterations"]),
            sub_agent_max_concurrency=int(config["sub_agent_max_concurrency"]),
            sub_agent_max_depth=int(config["sub_agent_max_depth"]),
            parallel_default_max_workers=int(config["parallel_default_max_workers"]),
            context_max_rounds=int(config["context_max_rounds"]),
            tool_timeout_seconds=max(1, int(config.get("tool_timeout_seconds") or 600)),
            shutdown_total_deadline_seconds=float(
                config["shutdown_total_deadline_seconds"]
            ),
            sse_subscriber_queue_size=int(config["sse_subscriber_queue_size"]),
            sse_max_subscribers_per_run=int(
                config["sse_max_subscribers_per_run"]
            ),
            sse_max_subscribers_total=int(
                config["sse_max_subscribers_total"]
            ),
            completed_replay_ttl_seconds=float(
                config["completed_replay_ttl_seconds"]
            ),
            short_io_workers=int(config["short_io_workers"]),
            short_io_max_pending=int(config["short_io_max_pending"]),
            short_io_capacity_wait_seconds=float(
                config["short_io_capacity_wait_seconds"]
            ),
            short_io_operation_timeout_seconds=float(
                config["short_io_operation_timeout_seconds"]
            ),
            external_io_workers=int(config["external_io_workers"]),
            external_io_max_pending=int(config["external_io_max_pending"]),
            external_io_capacity_wait_seconds=float(
                config["external_io_capacity_wait_seconds"]
            ),
            external_io_operation_timeout_seconds=float(
                config["external_io_operation_timeout_seconds"]
            ),
            tool_io_workers=int(config["tool_io_workers"]),
            tool_io_max_pending=int(config["tool_io_max_pending"]),
            tool_io_capacity_wait_seconds=float(
                config["tool_io_capacity_wait_seconds"]
            ),
            tool_io_operation_timeout_seconds=float(
                config["tool_io_operation_timeout_seconds"]
            ),
            runtime_warmup_enabled=bool(config.get("runtime_warmup_enabled", True)),
            evolution_min_occurrences=int(config.get("evolution_min_occurrences", 3)),
            evolution_min_applied=int(config.get("evolution_min_applied", 2)),
            evolution_max_rejection=int(config.get("evolution_max_rejection", 0)),
            evolution_passive_reflection_enabled=bool(
                config.get("evolution_passive_reflection_enabled", False)
            ),
            evolution_patch_auto_apply=bool(config.get("evolution_patch_auto_apply", False)),
            evolution_patch_cooldown_sec=int(config.get("evolution_patch_cooldown_sec", 3600)),
            evolution_sweep_interval_sec=int(config.get("evolution_sweep_interval_sec", 1800)),
            execute_script_timeout=int(config["execute_script_timeout"]),
            deterministic_execution_enabled=bool(
                config.get("deterministic_execution_enabled", False)
            ),
            direct_execution=DirectExecutionConfig.from_env(),
            web_search=WebSearchConfig.from_env(),
            limits=LimitsConfig.from_env(),
        )
