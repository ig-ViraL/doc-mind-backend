"""
Application settings loaded from environment variables / .env file.
"""

from pydantic import field_validator
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

    @field_validator("database_url", mode="before")
    @classmethod
    def fix_db_driver(cls, v: str) -> str:
        import re
        # Normalize any postgres URL variant to postgresql+asyncpg://.
        # Handles: postgres://, postgresql://, postgresql+psycopg2://, etc.
        return re.sub(r"^postgres(?:ql)?(?:\+\w+)?://", "postgresql+asyncpg://", v)

    # OpenRouter
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "meta-llama/llama-3.1-8b-instruct:free"

    # Voyage AI embeddings
    voyage_api_key: str = ""
    voyage_model: str = "voyage-3-lite"
    embedding_dim: int = 512  # voyage-3-lite=512, voyage-3/voyage-3-large=1024

    # RAG parameters
    top_k: int = 5          # chunks sent to LLM (context window size)
    retrieval_k: int = 12   # candidate pool fetched + reranked before truncation
    chunk_size: int = 512
    chunk_overlap: int = 50

    # Hybrid search — Reciprocal Rank Fusion constant.
    # Higher values reduce the ranking advantage of top results.
    rrf_k: int = 60

    # Phase 2 parsing
    extract_images: bool = True          # set False to skip vision LLM calls on upload
    vision_model: str = "meta-llama/llama-3.2-11b-vision-instruct:free"  # must support vision

    # Cross-encoder reranking model (sentence-transformers compatible)
    cross_encoder_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    # Minimum image size in bytes — smaller images (icons, bullets) are skipped
    min_image_bytes: int = 5_000

    # Maximum upload file size in MB
    max_file_size_mb: int = 50

    # Cloudflare R2 storage (optional — omit to disable)
    cloudfare_account_id: str = ""
    cloudfare_r2_access_key_id: str = ""      # from R2 → Manage R2 API Tokens
    cloudfare_r2_secret_access_key: str = ""  # from R2 → Manage R2 API Tokens
    cloudfare_r2_bucket: str = "docmind"
    cloudfare_r2_public_url: str = ""  # e.g. https://pub-xxxx.r2.dev (set if bucket is public)


settings = Settings()
