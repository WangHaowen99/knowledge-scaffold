from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    value = int(raw) if raw else default
    return min(maximum, max(minimum, value))


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, "").strip()
    value = float(raw) if raw else default
    return min(maximum, max(minimum, value))


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    core_base_url: str
    service_token: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout_seconds: float
    llm_max_tokens: int
    max_iterations: int
    host: str
    port: int
    skills_dir: Path

    @property
    def runtime_db(self) -> Path:
        return self.data_dir / "runtime.sqlite"

    @property
    def checkpoint_db(self) -> Path:
        return self.data_dir / "checkpoints.sqlite"

    @classmethod
    def from_env(cls) -> "Settings":
        package_root = Path(__file__).resolve().parent.parent
        return cls(
            data_dir=Path(os.getenv("MODELING_AGENT_DATA_DIR", "./data/agent")).resolve(),
            core_base_url=os.getenv("MODELING_CORE_BASE_URL", "http://127.0.0.1:5100").rstrip("/"),
            service_token=os.getenv("MODELING_SERVICE_TOKEN", ""),
            llm_base_url=os.getenv("MODELING_LLM_BASE_URL", "").rstrip("/"),
            llm_api_key=os.getenv("MODELING_LLM_API_KEY", ""),
            llm_model=os.getenv("MODELING_LLM_MODEL", ""),
            llm_timeout_seconds=_number("MODELING_LLM_TIMEOUT_SECONDS", 300.0, 10.0, 1800.0),
            llm_max_tokens=_integer("MODELING_LLM_MAX_TOKENS", 32768, 1024, 131072),
            max_iterations=_integer("MODELING_AGENT_MAX_ITERATIONS", 200, 20, 400),
            host=os.getenv("MODELING_AGENT_HOST", "0.0.0.0"),
            port=_integer("MODELING_AGENT_PORT", 5110, 1, 65535),
            skills_dir=Path(os.getenv("MODELING_AGENT_SKILLS_DIR", str(package_root / "skills"))).resolve(),
        )

    def with_runtime_llm(self, config: dict[str, object]) -> "Settings":
        max_tokens = int(config.get("maxTokens") or self.llm_max_tokens)
        return replace(
            self,
            llm_base_url=str(config.get("baseUrl") or "").rstrip("/"),
            llm_api_key=str(config.get("apiKey") or ""),
            llm_model=str(config.get("model") or ""),
            llm_max_tokens=min(self.llm_max_tokens, max_tokens),
        )

    def public_config(self) -> dict[str, object]:
        return {
            "coreBaseUrl": self.core_base_url,
            "modelConfigured": bool(self.llm_base_url and self.llm_model),
            "dynamicModelConfig": len(self.service_token) >= 32,
            "maxIterations": self.max_iterations,
        }
