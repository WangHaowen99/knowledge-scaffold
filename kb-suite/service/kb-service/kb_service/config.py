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

    # Concurrent searches: query embeddings have their own limit, apart from
    # embed_concurrency (ingestion batches); the API keeps db_pool_size
    # connections open; PostgreSQL parallel workers stay off for kb-service
    # sessions (each one adds a process start to a short query and multiplies
    # the CPU a search uses when many run at once).
    query_embed_concurrency: int = 16
    db_pool_size: int = 20
    db_parallel_workers: int = 0

    # MinerU (mineru-api 3.x task API) reads text from images and scanned PDFs.
    # Off keeps docreader-only parsing; an OCR task can take several GB of
    # memory on the MinerU side, so calls are capped per worker process.
    mineru_enabled: bool = False
    mineru_api_url: str = ""
    mineru_timeout_s: float = 600.0
    mineru_concurrency: int = 1
    # Images in this list go to MinerU; a PDF goes there only when docreader
    # finds fewer than mineru_scanned_chars_per_page characters per page.
    mineru_file_types: str = "pdf,png,jpg,jpeg,bmp,tif,tiff,webp,gif"
    mineru_scanned_chars_per_page: int = 50
    mineru_backend: str = "pipeline"

    # Speech to text through an OpenAI-compatible /audio/transcriptions endpoint.
    asr_enabled: bool = False
    asr_api_url: str = ""
    asr_api_key: str = ""
    asr_model: str = "whisper-1"
    asr_timeout_s: float = 900.0
    asr_concurrency: int = 1


@lru_cache
def get_settings() -> Settings:
    return Settings()
