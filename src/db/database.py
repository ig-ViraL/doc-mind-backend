"""
Async SQLAlchemy engine, session factory, ORM models, and DB initialisation.
"""

import logging
import uuid
from datetime import datetime
from typing import AsyncGenerator

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config import settings

logger = logging.getLogger(__name__)

# (current_type, expected_type) when the stored vectors don't match EMBEDDING_DIM.
embedding_dim_mismatch: tuple[str, str] | None = None

# ---------------------------------------------------------------------------
# Engine & session factory
# ---------------------------------------------------------------------------

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


# ---------------------------------------------------------------------------
# ORM base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_type: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    file_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    file_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Indexing lifecycle: processing -> ready | failed. `stage` says where processing is.
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready", server_default="ready")
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # False for DOCX/TXT, whose "pages" are ~3000-char virtual pages.
    has_real_pages: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    chunks: Mapped[list["Chunk"]] = relationship(
        "Chunk",
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Page(Base):
    __tablename__ = "pages"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (UniqueConstraint("document_id", "page_number", name="uq_pages_doc_page"),)


class Section(Base):
    __tablename__ = "sections"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("sections.id", ondelete="CASCADE"), nullable=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)  # document order
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)        # "Chapter 2 > 2.1 Scope"
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")  # own text, children excluded
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dim), nullable=True)
    start_char: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    end_char: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("sections.id", ondelete="SET NULL"), nullable=True
    )
    section_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "Document: x.pdf\nSection: A > B" — embedded and reranked together with the text.
    header: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    document: Mapped["Document"] = relationship("Document", back_populates="chunks")

    __table_args__ = (
        # HNSW index for approximate nearest-neighbour search (cosine distance).
        # Unlike IVFFlat it needs no training data, so recall stays good on small/new tables.
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


# ---------------------------------------------------------------------------
# DB initialisation
# ---------------------------------------------------------------------------


async def init_db() -> None:
    """Create pgvector extension and all tables if they do not exist."""
    async with engine.begin() as conn:
        # Enable pgvector extension (idempotent)
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        logger.info("pgvector extension ensured")

        # Detect an embedding column whose dimension doesn't match the configured model.
        # We never wipe embeddings at startup: re-embedding is an explicit step
        # (`python scripts/reindex.py`) so a config typo can't destroy the index.
        global embedding_dim_mismatch
        embedding_dim_mismatch = None
        result = await conn.execute(text(
            "SELECT format_type(atttypid, atttypmod) "
            "FROM pg_attribute "
            "WHERE attrelid = to_regclass('chunks') "
            "  AND attname = 'embedding' "
            "  AND attnum > 0 "
            "  AND NOT attisdropped"
        ))
        row = result.fetchone()
        if row is not None:
            current_type = row[0]  # e.g. "vector(384)"
            target_type = f"vector({settings.embedding_dim})"
            if current_type != target_type:
                embedding_dim_mismatch = (current_type, target_type)
                logger.error(
                    "Embedding column is %s but EMBEDDING_DIM expects %s. Uploads and vector "
                    "search are disabled until you run `python scripts/reindex.py`.",
                    current_type, target_type,
                )

        # The old IVFFlat index is replaced by HNSW (created by create_all below).
        await conn.execute(text("DROP INDEX IF EXISTS ix_chunks_embedding_ivfflat"))

        # Add file storage columns to existing documents table (idempotent)
        await conn.execute(text(
            "ALTER TABLE IF EXISTS documents ADD COLUMN IF NOT EXISTS file_key VARCHAR(1024)"
        ))
        await conn.execute(text(
            "ALTER TABLE IF EXISTS documents ADD COLUMN IF NOT EXISTS file_url TEXT"
        ))

        # Columns added for structure-aware indexing (idempotent; old rows count as ready).
        for ddl in (
            "ALTER TABLE IF EXISTS documents ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'ready'",
            "ALTER TABLE IF EXISTS documents ADD COLUMN IF NOT EXISTS stage VARCHAR(32)",
            "ALTER TABLE IF EXISTS documents ADD COLUMN IF NOT EXISTS error TEXT",
            "ALTER TABLE IF EXISTS documents ADD COLUMN IF NOT EXISTS summary TEXT",
            "ALTER TABLE IF EXISTS documents ADD COLUMN IF NOT EXISTS has_real_pages BOOLEAN NOT NULL DEFAULT TRUE",
            "ALTER TABLE IF EXISTS chunks ADD COLUMN IF NOT EXISTS page_start INTEGER",
            "ALTER TABLE IF EXISTS chunks ADD COLUMN IF NOT EXISTS page_end INTEGER",
            "ALTER TABLE IF EXISTS chunks ADD COLUMN IF NOT EXISTS section_id UUID",
            "ALTER TABLE IF EXISTS chunks ADD COLUMN IF NOT EXISTS section_path TEXT",
            "ALTER TABLE IF EXISTS chunks ADD COLUMN IF NOT EXISTS header TEXT",
        ):
            await conn.execute(text(ddl))

        # Create tables / indexes that don't exist yet
        await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables created / verified")

        # GIN index for full-text search (idempotent — IF NOT EXISTS)
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_chunks_text_gin "
                "ON chunks USING GIN (to_tsvector('english', text))"
            )
        )
        logger.info("FTS GIN index ensured")

        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_chunks_section_id ON chunks (section_id)"))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pages_text_gin ON pages USING GIN (to_tsvector('english', text))"
        ))


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async DB session; roll back on error, always close."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
