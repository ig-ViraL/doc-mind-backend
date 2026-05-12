"""
Application settings loaded from environment variables / .env file.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    database_url: str = "postgresql+asyncpg://docmind:secret@localhost:5432/docmind"

    # OpenRouter
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "anthropic/claude-3.5-sonnet"

    # Embedding model
    embedding_model: str = "all-MiniLM-L6-v2"

    # RAG parameters
    top_k: int = 5
    chunk_size: int = 512
    chunk_overlap: int = 50

    # Hybrid search — Reciprocal Rank Fusion constant.
    # Higher values reduce the ranking advantage of top results.
    rrf_k: int = 60


settings = Settings()
