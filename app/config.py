"""Runtime configuration, loaded from environment variables and an optional .env file."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal, Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- LLM ---------------------------------------------------------------
    llm_provider: Literal["openai", "mock"] = Field(
        default="openai",
        description="'openai' for any OpenAI-compatible server (LM Studio, Ollama, vLLM, OpenAI); "
        "'mock' for a deterministic offline analyzer (demos and tests).",
    )
    llm_base_url: str = Field(
        default="http://127.0.0.1:1234/v1",
        validation_alias=AliasChoices("LLM_BASE_URL", "OPENAI_API_BASE", "OPENAI_BASE_URL"),
    )
    llm_api_key: str = Field(
        default="not-needed",
        validation_alias=AliasChoices("LLM_API_KEY", "OPENAI_API_KEY"),
    )
    llm_model: str = Field(default="auto", validation_alias=AliasChoices("LLM_MODEL", "OPENAI_MODEL"))
    llm_temperature: float = 0.0
    llm_timeout_seconds: float = 120.0
    llm_max_validation_retries: int = Field(
        default=2, ge=0, description="Re-prompts when the model returns JSON that fails schema validation."
    )

    # --- Pipeline ----------------------------------------------------------
    worker_concurrency: int = Field(default=2, ge=1, le=64)
    max_job_attempts: int = Field(default=3, ge=1, description="Attempts per job before it is marked failed.")
    retry_backoff_seconds: float = Field(default=2.0, ge=0)
    max_batch_size: int = Field(default=100, ge=1)
    max_text_length: int = Field(default=20_000, ge=1)

    # --- Storage / server --------------------------------------------------
    database_path: str = "data/triaxa.db"
    api_key: Optional[str] = Field(
        default=None, description="If set, requests must send it in the X-API-Key header."
    )
    cors_origins: str = Field(default="*", description="Comma-separated list of allowed origins.")
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
