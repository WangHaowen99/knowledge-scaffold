"""Application configuration helpers."""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from .system_config import (
    SystemConfigValidationError,
    load_system_config,
    resolve_llm_profile_config,
)
from .ops_config import load_ops_config


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
            max_tokens=int(pick_number("max_tokens", fallback.max_tokens if fallback else 4096)),
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
        config = load_system_config()
        section = "task_llm" if prefix.rstrip("_").upper() == "TASK_LLM" else "llm"
        base_fallback = fallback
        if section == "llm":
            try:
                return cls.from_mapping(resolve_llm_profile_config(), fallback=base_fallback)
            except SystemConfigValidationError:
                return cls(
                    base_url=base_fallback.base_url if base_fallback else None,
                    model=base_fallback.model if base_fallback else None,
                    api_key=base_fallback.api_key if base_fallback else None,
                    timeout=base_fallback.timeout if base_fallback else 600,
                    max_tokens=base_fallback.max_tokens if base_fallback else 4096,
                    temperature=base_fallback.temperature if base_fallback else 0.7,
                    provider=base_fallback.provider if base_fallback else "openai_compatible",
                    profile_id=base_fallback.profile_id if base_fallback else None,
                    profile_name=base_fallback.profile_name if base_fallback else None,
                )

        llm_config = config[section]
        if section == "task_llm" and base_fallback is None:
            base_fallback = cls.from_env("LLM")

        def pick_text(name: str, fallback_value: Optional[str]) -> Optional[str]:
            value = llm_config.get(name)
            if value is None or value == "":
                return fallback_value
            return str(value)

        def pick_number(name: str, fallback_value):
            value = llm_config.get(name)
            return fallback_value if value is None else value

        if section == "task_llm":
            return cls(
                base_url=pick_text("base_url", base_fallback.base_url if base_fallback else None),
                model=pick_text("model", base_fallback.model if base_fallback else None),
                api_key=pick_text("api_key", base_fallback.api_key if base_fallback else None),
                timeout=int(pick_number("timeout", 600)),
                max_tokens=int(pick_number("max_tokens", base_fallback.max_tokens if base_fallback else 4096)),
                temperature=float(pick_number("temperature", base_fallback.temperature if base_fallback else 0.7)),
                provider=base_fallback.provider if base_fallback else "openai_compatible",
                profile_id="task_llm",
                profile_name="轻量模型",
            )

        return cls(
            base_url=pick_text("base_url", base_fallback.base_url if base_fallback else None),
            model=pick_text("model", base_fallback.model if base_fallback else None),
            api_key=pick_text("api_key", base_fallback.api_key if base_fallback else None),
            timeout=int(pick_number("timeout", base_fallback.timeout if base_fallback else 600)),
            max_tokens=int(pick_number("max_tokens", base_fallback.max_tokens if base_fallback else 4096)),
            temperature=float(pick_number("temperature", base_fallback.temperature if base_fallback else 0.7)),
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
    """Business/runtime limits shared by route and storage boundaries."""

    run_tps: int = 50
    user_max_count: int = 10
    workspace_max_count: int = 5
    ontology_max_count_per_workspace: int = 10
    ontology_object_max_count: int = 200
    ontology_operation_max_count: int = 100
    entity_attribute_max_count: int = 100
    data_connection_configs_max_per_workspace: int = 10
    model_profile_max_count: int = 10
    model_input_token_limit: int = 50000
    model_output_token_limit: int = 100000
    session_max_per_user_workspace: int = 100
    operation_max_chars: int = 15000
    agent_log_retention_days: int = 180
    agent_log_compress_old: bool = True

    @classmethod
    def from_env(cls) -> "LimitsConfig":
        config = load_ops_config()["limits"]
        return cls(
            run_tps=max(1, int(config["run_tps"])),
            user_max_count=max(1, int(config["user_max_count"])),
            workspace_max_count=max(1, int(config["workspace_max_count"])),
            ontology_max_count_per_workspace=max(1, int(config["ontology_max_count_per_workspace"])),
            ontology_object_max_count=max(1, int(config["ontology_object_max_count"])),
            ontology_operation_max_count=max(1, int(config["ontology_operation_max_count"])),
            entity_attribute_max_count=max(1, int(config["entity_attribute_max_count"])),
            data_connection_configs_max_per_workspace=max(1, int(config["data_connection_configs_max_per_workspace"])),
            model_profile_max_count=max(1, int(config["model_profile_max_count"])),
            model_input_token_limit=max(1, int(config["model_input_token_limit"])),
            model_output_token_limit=max(1, int(config["model_output_token_limit"])),
            session_max_per_user_workspace=max(1, int(config["session_max_per_user_workspace"])),
            operation_max_chars=max(1, int(config["operation_max_chars"])),
            agent_log_retention_days=max(1, int(config["agent_log_retention_days"])),
            agent_log_compress_old=bool(config["agent_log_compress_old"]),
        )


@dataclass
class AppConfig:
    """Container for runtime configuration."""

    llm: LLMConfig
    task_llm: LLMConfig
    project_root: str
    auth_users_file: str
    auth_secret: Optional[str]
    auth_token_ttl_seconds: int

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Load configuration from system config with env/default fallback."""
        config = load_system_config()
        llm = LLMConfig.from_env()
        task_llm = LLMConfig.from_env(prefix="TASK_LLM", fallback=llm)
        storage = config["storage"]
        auth = config["auth"]
        return cls(
            llm=llm,
            task_llm=task_llm,
            project_root=storage["project_root"],
            auth_users_file=storage["auth_users_file"],
            auth_secret=auth.get("auth_secret") or None,
            auth_token_ttl_seconds=int(auth["auth_token_ttl_seconds"]),
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
    context_prune_tools: Set[str]
    execute_script_timeout: int = 300
    runtime_warmup_enabled: bool = True

    # Operation evolution (see docs/operation-evolution.md)
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
        raw_prune = str(config.get("context_prune_tools") or "").strip()
        prune_tools: Set[str] = set()
        if raw_prune:
            prune_tools = {s.strip() for s in raw_prune.split(",") if s.strip()}
        return cls(
            llm_connection_max_retries=int(config["llm_connection_max_retries"]),
            main_agent_max_iterations=int(config["main_agent_max_iterations"]),
            sub_agent_max_iterations=int(config["sub_agent_max_iterations"]),
            sub_agent_max_concurrency=int(config["sub_agent_max_concurrency"]),
            sub_agent_max_depth=int(config["sub_agent_max_depth"]),
            parallel_default_max_workers=int(config["parallel_default_max_workers"]),
            context_max_rounds=int(config["context_max_rounds"]),
            context_prune_tools=prune_tools,
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
            runtime_warmup_enabled=bool(config.get("runtime_warmup_enabled", True)),
            direct_execution=DirectExecutionConfig.from_env(),
            web_search=WebSearchConfig.from_env(),
            limits=LimitsConfig.from_env(),
        )
