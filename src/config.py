"""
Application settings loaded from environment variables / .env file.
"""

from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Works whether you start from the repo root or from src/.
        env_file=(_ROOT / ".env", _ROOT / "src" / ".env", ".env"),
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
        v = re.sub(r"^postgres(?:ql)?(?:\+\w+)?://", "postgresql+asyncpg://", v)
        # asyncpg rejects libpq-style params (sslmode, channel_binding) that Neon/Railway URLs carry.
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parts = urlsplit(v)
        query = [
            ("ssl" if k == "sslmode" else k, val)
            for k, val in parse_qsl(parts.query)
            if k != "channel_binding"
        ]
        return urlunsplit(parts._replace(query=urlencode(query)))

    # OpenRouter
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Strong model: agent planning, routing fallbacks and final answers.
    openrouter_model: str = "anthropic/claude-sonnet-4.5"
    # Cheap model: section/document summaries, map step, answer checking, routing.
    fast_model: str = "anthropic/claude-haiku-4.5"

    # Agent budgets (deep and exhaustive modes)
    agent_max_steps: int = 8
    agent_token_budget: int = 120_000     # prompt + completion tokens across all LLM calls
    agent_time_limit_s: float = 120.0
    exhaustive_max_units: int = 48        # sections read in the map step
    exhaustive_unit_tokens: int = 3_000   # target size of one map unit
    map_concurrency: int = 6
    verify_answers: bool = True           # run the checker in deep/exhaustive modes

    # Indexing
    summarize_sections: bool = True
    max_section_summaries: int = 150
    summary_concurrency: int = 6
    max_vision_images: int = 20           # per document
    vision_concurrency: int = 4
    max_concurrent_indexing: int = 1      # documents indexed at once (GPU contention)

    # Embeddings: "local" (free, in-process) or "voyage" (API)
    embedding_provider: Literal["local", "voyage"] = "local"
    local_embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    voyage_api_key: str = ""
    voyage_model: str = "voyage-3-lite"
    # Must match the model: Qwen3-Embedding-0.6B=1024, MiniLM=384, voyage-3-lite=512.
    # Changing it requires `python scripts/reindex.py`.
    embedding_dim: int = 1024
    embedding_device: str = "auto"      # auto | mps | cuda | cpu
    embedding_fp16: bool = True         # applied on mps/cuda only
    embedding_batch_size: int = 16
    embedding_max_seq_length: int = 1024
    # Instruction prepended to queries only (Qwen3-Embedding is trained this way).
    # Set to "" for models without query instructions (e.g. MiniLM).
    embedding_query_prompt: str = (
        "Instruct: Given a question about a document, retrieve the passages that answer it\nQuery: "
    )

    # LLM call limits
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2

    # RAG parameters
    top_k: int = 5          # chunks sent to LLM (context window size)
    retrieval_k: int = 50   # candidate pool fetched + reranked before truncation
    chunk_size: int = 400   # tokens (embedding model tokenizer when loaded)
    chunk_overlap: int = 60 # tokens

    # HNSW search breadth (higher = better recall, slower). Must be >= retrieval_k.
    hnsw_ef_search: int = 100

    # Hybrid search — Reciprocal Rank Fusion constant.
    # Higher values reduce the ranking advantage of top results.
    rrf_k: int = 60

    # Phase 2 parsing
    extract_images: bool = True          # set False to skip vision LLM calls on upload
    vision_model: str = "meta-llama/llama-3.2-11b-vision-instruct:free"  # must support vision

    # Cross-encoder reranking model (sentence-transformers compatible)
    cross_encoder_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_device: str = "auto"
    reranker_fp16: bool = True
    reranker_max_length: int = 512
    reranker_batch_size: int = 16

    # Minimum image size in bytes — smaller images (icons, bullets) are skipped
    min_image_bytes: int = 5_000

    # Comma-separated allowed browser origins; "*" allows any (fine for a public demo API)
    cors_origins: str = "*"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # Maximum upload file size in MB
    max_file_size_mb: int = 50

    # Cloudflare R2 storage (optional — omit to disable)
    cloudfare_account_id: str = ""
    cloudfare_r2_access_key_id: str = ""      # from R2 → Manage R2 API Tokens
    cloudfare_r2_secret_access_key: str = ""  # from R2 → Manage R2 API Tokens
    cloudfare_r2_bucket: str = "docmind"
    cloudfare_r2_public_url: str = ""  # e.g. https://pub-xxxx.r2.dev (set if bucket is public)


settings = Settings()
