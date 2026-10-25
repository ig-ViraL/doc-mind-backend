# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview
Agentic RAG backend for document Q&A over long documents. Users upload PDF / DOCX / TXT / MD files; the system stores pages, a section tree, summaries and section-aware chunks in Postgres + pgvector, then answers questions in one of four modes (auto, fast, deep, exhaustive) with page-level citations checked against evidence.

**Backend only** — the frontend is a separate project. This is a local demo: no auth, tenancy or rate limiting by design.

## Tech Stack
- **Runtime**: Python 3.11+, FastAPI (async)
- **LLMs** (OpenRouter via the `openai` SDK): `OPENROUTER_MODEL` (default `anthropic/claude-sonnet-4.5`) plans and answers; `FAST_MODEL` (default `anthropic/claude-haiku-4.5`) does summaries, routing, the map step and checking
- **Embeddings**: `Qwen/Qwen3-Embedding-0.6B` (1024-d) via sentence-transformers, MPS/CUDA/CPU auto, fp16 on GPU, instruction prompt on queries only
- **Reranker**: `BAAI/bge-reranker-v2-m3` cross-encoder, same device logic
- **Store**: PostgreSQL + pgvector (HNSW cosine index) + GIN full-text indexes
- **ORM**: SQLAlchemy 2 async + asyncpg
- **Parsing**: pymupdf (text, outline, fonts, images, links), pdfplumber (tables), python-docx, unstructured fallback

## Development Commands

```bash
docker-compose up postgres -d          # Postgres + pgvector on :5433
pip install -r requirements.txt
cp .env.example .env                   # repo root or src/ — both are read

cd src && uvicorn main:app --reload --port 8000

python -m pytest -q                    # from repo root; no DB/GPU/API key needed
python scripts/reindex.py              # after changing embedding model or EMBEDDING_DIM
python scripts/eval.py --pdf X.pdf --questions q.json   # page recall + citation accuracy
```

## API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/documents/upload` | Store file, index in background → 202 `{document_id, status: processing}` |
| GET | `/api/documents/{id}/status` | `processing` + stage, `ready`, or `failed` + error |
| GET | `/api/documents/{id}/outline` | Section tree with pages and summaries |
| GET | `/api/documents/{id}/pages/{n}` | One page's text |
| GET | `/api/documents` | List documents |
| GET | `/api/documents/{id}/chunks` | Chunks with pages and section paths |
| DELETE | `/api/documents/{id}` | Delete document (pages/sections/chunks cascade) |
| POST | `/api/query` | `{query, document_ids?, k?, mode?}` → answer, sources, citations, steps, budget |
| POST | `/api/query/stream` | SSE: `mode`, `step`…, `sources`, `token`…, `citations`, `done` (`error` on failure) |
| GET | `/health`, `/api/status` | Liveness; config, DB status, `reindex_required` |

## Architecture

### Upload (`services/indexer.py`, background task)
`parser.parse_bytes` → `ParsedDocument(pages, headings)` → `structure.assemble` / `locate_headings` / `build_sections` → `structure.chunk_sections` (TokenChunker per section body, `Document: … / Section: …` header) → `embedder.embed(header + text)` → save pages, sections, chunks → `summarizer.summarize` (cheap model) → status `ready`.
Status/stage live on the `documents` row. Jobs left `processing` by a restart are marked `failed` at startup.

### Query (`services/rag_pipeline.py`)
`stream_answer()` is the single code path; `answer_query()` collects its events into a `QueryResponse`.
1. Meta-query guard (identity/chitchat → fixed intro, no retrieval)
2. `mode=auto` → `agent/router.route`: regex rules first, cheap model for ambiguous questions
3. **fast**: `retrieval.retrieve` (embed → `vector_store.hybrid_search` (HNSW + FTS, RRF) → bge rerank in a thread → top-k) → streamed answer
4. **deep**: `agent/deep.run_deep` — tool loop with `agent/tools.TOOL_SPECS` against an `agent/store.DocStore`; passages get ids `P1…`, sections `S1…`, documents `D1…` (`agent/workspace.py`)
5. **exhaustive**: `agent/exhaustive.run_exhaustive` — `build_map_units` packs section bodies, cheap model extracts quoted findings per unit, `MAP_CONCURRENCY` at a time
6. Deep/exhaustive: `agent/answer.generate` from evidence only (numbered `[n]`) → `agent/answer.check` removes unsupported sentences → `citations` maps used markers to pages
7. `Budget` (steps, tokens, seconds) bounds deep and exhaustive; each tool call / map batch is one step

### Key Design Decisions
- **One SSE generator**: blocking and streaming endpoints share `stream_answer`, so they can't drift.
- **`DocStore` interface**: tools never touch SQL directly; `PgStore` implements it, `tests/fakes.FakeStore` replaces it in tests along with `ScriptedLLM` (monkeypatched over `services.llm.chat`).
- **`services/llm.chat`** returns content, tool calls and token usage; every LLM call in the agent goes through it so budgets are enforced. Fast-mode streaming uses `llm_client.get_openrouter_client()` directly.
- **No silent re-embedding**: `init_db()` only flags a vector-dimension mismatch (`database.embedding_dim_mismatch`); uploads return 409 until `scripts/reindex.py` runs.
- **Schema migrations** are idempotent `ALTER TABLE … ADD COLUMN IF NOT EXISTS` in `init_db()` plus `create_all`.
- **Config** is `src/config.py` (`settings` singleton); `.env` is read from repo root, `src/`, or cwd.

## Coding Standards
- `async/await` throughout; blocking work (models, parsing) via `asyncio.to_thread`
- Pydantic v2 schemas for all request/response bodies
- FastAPI `Depends(get_db)` for request sessions; background jobs open their own `AsyncSessionLocal()`
- `vector_store` functions never commit; callers own the transaction
- No print statements in `src/` — use `logging.getLogger(__name__)`
- Type hints on all functions
- New agent behaviour gets a test in `tests/test_agent_*.py` using the fakes
