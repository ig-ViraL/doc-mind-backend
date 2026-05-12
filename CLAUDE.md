# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview
Production-ready RAG (Retrieval-Augmented Generation) backend for document Q&A. Users upload PDFs, DOCX, or TXT files; the system chunks, embeds, stores in pgvector, and answers natural language queries with cited sources.

**Backend only** — frontend is a separate project.

## Tech Stack
- **Runtime**: Python, FastAPI (async)
- **LLM**: OpenRouter API (OpenAI-compatible) — default model `anthropic/claude-3.5-sonnet`, called via `openai` SDK
- **Embeddings**: `sentence-transformers` (`all-MiniLM-L6-v2`, 384 dims)
- **Vector Store**: PostgreSQL + pgvector extension
- **ORM**: SQLAlchemy (async) + asyncpg
- **Document Parsing**: pypdf, python-docx, unstructured
- **Containerization**: Docker Compose

## Development Commands

All commands run from `src/`:

```bash
# Start only Postgres (preferred during development)
docker-compose up postgres -d

# Install dependencies
cd src
pip install -r requirements.txt

# Run the dev server (hot-reload)
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Run tests
cd src
pytest

# Run a single test file
pytest tests/test_chunker.py -v

# Start the full stack (Postgres + backend) via Docker
docker-compose up --build
```

## API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/documents/upload` | Upload & index a document |
| GET | `/api/documents` | List all indexed documents |
| DELETE | `/api/documents/{document_id}` | Delete document + chunks |
| POST | `/api/query` | Ask a question, get answer + citations |
| GET | `/health` | Simple liveness check |
| GET | `/api/status` | Uptime, config, and DB connectivity |

## Environment Variables

Copy `src/.env.example` to `src/.env`. Required:

```
OPENROUTER_API_KEY=sk-or-...          # Required — from openrouter.ai/keys
OPENROUTER_MODEL=anthropic/claude-3.5-sonnet  # Optional — any OpenRouter model slug
DATABASE_URL=postgresql+asyncpg://docmind:secret@localhost:5432/docmind
EMBEDDING_MODEL=all-MiniLM-L6-v2
TOP_K=5
CHUNK_SIZE=512      # Character count (not tokens)
CHUNK_OVERLAP=50    # Character overlap
```

## Architecture

### RAG Pipeline Flow (`services/rag_pipeline.py`)
1. Embed query via `SentenceTransformer` → 384-dim vector
2. Cosine similarity search in pgvector (`top-K` chunks)
3. Cross-encoder reranking via `cross-encoder/ms-marco-MiniLM-L-6-v2` (lazy-loaded; degrades gracefully if unavailable)
4. Build context string with `Chunk N (from filename):` labels
5. Call OpenRouter via `openai.AsyncOpenAI` with a strict system prompt
6. Return `QueryResponse` with answer, `SourceChunk` list, and `latency_ms`

### Key Design Decisions
- **OpenRouter via `openai` SDK**: `_get_openrouter_client()` creates an `AsyncOpenAI` singleton pointed at `openrouter_base_url`. To swap models, change `OPENROUTER_MODEL` — no code changes needed.
- **Two chunkers**: `RecursiveCharacterChunker` (default, character-based with separator hierarchy) and `SemanticChunker` (embedding-based sentence grouping). Only `RecursiveCharacterChunker` is wired into `api/documents.py`; `SemanticChunker` is available but not active.
- **ORM models live in `db/database.py`**: `Document` and `Chunk` ORM classes, `init_db()` creates the pgvector extension and tables on startup via `Base.metadata.create_all`.
- **IVFFlat index**: `chunks.embedding` has `postgresql_using="ivfflat"`, `lists=100`, `vector_cosine_ops` for ANN search.
- **Config via `pydantic-settings`**: `src/config.py` exposes a `settings` singleton loaded from `.env`. All services import from here.

### Data Flow for Upload
`POST /api/documents/upload` → parse (pypdf / python-docx / unstructured) → `RecursiveCharacterChunker.chunk()` → `Embedder.embed()` (batch) → `vector_store.save_document()` + `vector_store.save_chunks()`

## Coding Standards
- Use `async/await` throughout — no blocking I/O
- Pydantic v2 models for all request/response schemas (`model_config = {"from_attributes": True}` where reading ORM objects)
- FastAPI dependency injection for DB sessions (`Depends(get_db)`)
- No print statements — use `logging.getLogger(__name__)`
- Type hints on all functions
