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
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config import settings

logger = logging.getLogger(__name__)

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

    chunks: Mapped[list["Chunk"]] = relationship(
        "Chunk",
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    document: Mapped["Document"] = relationship("Document", back_populates="chunks")

    __table_args__ = (
        # IVFFlat index for approximate nearest-neighbour search (cosine distance)
        Index(
            "ix_chunks_embedding_ivfflat",
            "embedding",
            postgresql_using="ivfflat",
            postgresql_with={"lists": 100},
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

        # Migrate embedding column dimension if the table already exists with wrong dim.
        # This happens when switching embedding models (e.g. all-MiniLM-L6-v2 → Voyage AI).
        # Old embeddings are incompatible anyway, so we NULL them out and retype the column.
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
                logger.info(
                    "Embedding column is %s — migrating to %s (old embeddings cleared)",
                    current_type, target_type,
                )
                await conn.execute(text("DROP INDEX IF EXISTS ix_chunks_embedding_ivfflat"))
                await conn.execute(text("UPDATE chunks SET embedding = NULL"))
                await conn.execute(text(
                    f"ALTER TABLE chunks ALTER COLUMN embedding TYPE {target_type}"
                ))
                await conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_chunks_embedding_ivfflat "
                    "ON chunks USING ivfflat (embedding vector_cosine_ops) "
                    "WITH (lists = 100)"
                ))
                logger.info("Embedding column migrated to %s", target_type)

        # Add file storage columns to existing documents table (idempotent)
        await conn.execute(text(
            "ALTER TABLE IF EXISTS documents ADD COLUMN IF NOT EXISTS file_key VARCHAR(1024)"
        ))
        await conn.execute(text(
            "ALTER TABLE IF EXISTS documents ADD COLUMN IF NOT EXISTS file_url TEXT"
        ))

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
