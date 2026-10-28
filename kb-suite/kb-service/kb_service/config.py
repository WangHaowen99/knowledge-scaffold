from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, all read from ``KBS_*`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="KBS_", extra="ignore")

    database_url: str = "postgresql://kbs:kbs@kbs-postgres:5432/kbs"
    files_dir: str = "/data/files"

    docreader_addr: str = "kbs-docreader:50051"
    docreader_timeout_s: float = 600.0
    docreader_max_message_mb: int = 256

    # OpenAI-compatible endpoints. Credentials only ever come from the environment.
    llm_base_url: str = ""
    llm_api_key: str = ""
    embed_model: str = "text-embedding-v4"
    embed_dim: int = 1024
    embed_batch: int = 10
    embed_concurrency: int = 2
    rerank_url: str = ""
    rerank_model: str = "qwen3-rerank"
    summary_model: str = "qwen-plus"
    summary_input_chars: int = 6000
    # Extra chat models selectable per knowledge base (comma separated).
    chat_models: str = ""
    llm_concurrency: int = 4

    chunk_size: int = 512
    chunk_overlap: int = 80

    max_upload_mb: int = 200
    upload_part_mb: int = 4

    worker_concurrency: int = 2
    job_lease_s: int = 1800
    job_poll_s: float = 2.0

    retrieval_candidates: int = 40
    rerank_candidates: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
