"""
DocMind API — FastAPI application entry point.

Startup:
  - Initialises the database (creates pgvector extension + tables)
  - Registers CORS middleware
  - Mounts /api/documents and /api/query routers

Health check:
  GET /health → { "status": "ok" }
"""

from __future__ import annotations

import asyncio
import logging
import logging.config
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from api.documents import router as documents_router
from api.query import router as query_router
from config import settings
from db.database import AsyncSessionLocal, init_db
from services.embedder import warm_up as warm_up_embedder
from services.retrieval import warm_up_cross_encoder

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOGGING_CONFIG: dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            "datefmt": "%Y-%m-%dT%H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "stream": "ext://sys.stdout",
        },
    },
    "root": {
        "level": "INFO",
        "handlers": ["console"],
    },
    "loggers": {
        "uvicorn": {"level": "INFO"},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"level": "WARNING"},
        "sqlalchemy.engine": {"level": "WARNING"},
    },
}

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger(__name__)

_start_time: float = 0.0


# ---------------------------------------------------------------------------
# Lifespan — startup / shutdown
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Run DB init and model pre-warming on startup; clean up on shutdown."""
    global _start_time
    _start_time = time.time()
    logger.info("DocMind API starting up — initialising database …")
    await init_db()
    from services.vector_store import fail_interrupted

    async with AsyncSessionLocal() as session:
        if n := await fail_interrupted(session):
            logger.warning("Marked %d interrupted indexing job(s) as failed", n)
        await session.commit()
    logger.info("Database ready — pre-warming ML models …")
    # Both models do blocking file I/O on load; run in thread to keep loop responsive.
    await asyncio.gather(
        asyncio.to_thread(warm_up_embedder),
        asyncio.to_thread(warm_up_cross_encoder),
    )
    logger.info("Models ready — API accepting requests")
    yield
    logger.info("DocMind API shutting down")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


app = FastAPI(
    title="DocMind API",
    description=(
        "Production-ready RAG backend for document Q&A. "
        "Upload PDFs, DOCX, or TXT files; ask natural-language questions."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS — allow all origins in development
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(documents_router, prefix="/api")
app.include_router(query_router, prefix="/api")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health", tags=["health"], summary="Service health check")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/status", tags=["health"], summary="Detailed server status")
async def server_status() -> dict:
    """Returns uptime, config, and live database connectivity."""
    uptime_seconds = round(time.time() - _start_time, 1) if _start_time else 0.0

    db_status = "ok"
    db_error: str | None = None
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        db_status = "error"
        db_error = str(exc)
        logger.warning("Status check — DB unreachable: %s", exc)

    from db import database

    mismatch = database.embedding_dim_mismatch
    return {
        "status": "ok" if db_status == "ok" and not mismatch else "degraded",
        "reindex_required": bool(mismatch),
        "version": app.version,
        "uptime_seconds": uptime_seconds,
        "database": db_status,
        "database_error": db_error,
        "config": {
            "embedding_provider": settings.embedding_provider,
            "embedding_model": settings.local_embedding_model if settings.embedding_provider == "local" else settings.voyage_model,
            "reranker_model": settings.cross_encoder_model,
            "embedding_dim": settings.embedding_dim,
            "top_k": settings.top_k,
            "chunk_size": settings.chunk_size,
            "chunk_overlap": settings.chunk_overlap,
        },
    }
