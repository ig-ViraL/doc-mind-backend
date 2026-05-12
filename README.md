# DocMind

A production-ready RAG (Retrieval-Augmented Generation) backend for document Q&A. Upload PDFs, DOCX, or TXT files — DocMind extracts text, tables, images, and hyperlinks, chunks and embeds the content, stores it in PostgreSQL with pgvector, and answers natural-language questions with cited sources.

**Backend only.** Frontend is a separate project.

---

## Table of Contents

- [What It Does](#what-it-does)
- [Tech Stack](#tech-stack)
- [Architecture](#architecture)
- [API Reference](#api-reference)
- [Project Structure](#project-structure)
- [Environment Variables](#environment-variables)
- [Running Locally](#running-locally)
  - [Without Docker (dev)](#option-1--without-docker-recommended-for-development)
  - [Full Docker Compose](#option-2--full-docker-compose)
- [Database Schema](#database-schema)
- [Key Design Decisions](#key-design-decisions)
- [Running Tests](#running-tests)
- [What's Built](#whats-built-feature-status)
- [Challenges & How We Solved Them](#challenges--how-we-solved-them)

---

## What It Does

1. **Ingest** — Upload a document. DocMind parses it and extracts:
   - Plain text (all pages)
   - Tables → converted to Markdown
   - Images → described by a vision LLM (Claude)
   - Hyperlinks → embedded inline with anchor text

2. **Index** — Text is split into overlapping chunks, each chunk is embedded into a 384-dimensional vector via `all-MiniLM-L6-v2`, and stored in PostgreSQL with the pgvector extension.

3. **Retrieve** — At query time, DocMind runs **hybrid search** (vector similarity + full-text search), fuses results via **Reciprocal Rank Fusion (RRF)**, then reranks the candidate pool with a **cross-encoder** model.

4. **Answer** — The top-ranked chunks are assembled into a context window and passed to Claude (via OpenRouter) with a strict system prompt. The response includes the answer and cited source documents.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.11, FastAPI (async) |
| LLM | OpenRouter API → `anthropic/claude-3.5-sonnet` (via `openai` SDK) |
| Vision LLM | Same model — used for image description during upload |
| Embeddings | `sentence-transformers` — `all-MiniLM-L6-v2` (384 dims) |
| Reranking | `cross-encoder/ms-marco-MiniLM-L-6-v2` |
| Vector Store | PostgreSQL 16 + `pgvector` extension |
| ORM | SQLAlchemy 2.x (async) + `asyncpg` |
| PDF Parsing | `pypdf` (text + links) + `pdfplumber` (tables) + `pymupdf` (images) |
| DOCX Parsing | `python-docx` |
| Fallback Parser | `unstructured` |
| Containerisation | Docker + Docker Compose |
| Config | `pydantic-settings` (`.env` file) |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Upload Flow                          │
│                                                             │
│  POST /api/documents/upload                                 │
│       │                                                     │
│       ▼                                                     │
│  Parser (Phase 2)                                           │
│  ├─ Text extraction (pypdf / python-docx)                   │
│  ├─ Table extraction → Markdown  (pdfplumber / python-docx) │
│  ├─ Image extraction → Vision LLM description (pymupdf)     │
│  └─ Hyperlink extraction (pypdf annotations / docx XML)     │
│       │                                                     │
│       ▼                                                     │
│  RecursiveCharacterChunker  (chunk_size=512, overlap=50)    │
│       │                                                     │
│       ▼                                                     │
│  Embedder  (all-MiniLM-L6-v2, batch)                        │
│       │                                                     │
│       ▼                                                     │
│  PostgreSQL + pgvector                                      │
│  ├─ documents table  (id, filename, file_type, page_count)  │
│  └─ chunks table     (text, embedding vector[384], ...)     │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                        Query Flow                           │
│                                                             │
│  POST /api/query                                            │
│       │                                                     │
│       ▼                                                     │
│  Meta-query guard  (identity / chitchat → instant reply)    │
│       │                                                     │
│       ▼                                                     │
│  Embedder  (embed query → 384-dim vector)                   │
│       │                                                     │
│       ▼                                                     │
│  Hybrid Search  (retrieval_k=12 candidates)                 │
│  ├─ Vector arm:   pgvector ANN cosine search                │
│  ├─ Lexical arm:  PostgreSQL FTS (ts_rank_cd + GIN index)   │
│  └─ Fusion:       Reciprocal Rank Fusion (RRF, k=60)        │
│       │                                                     │
│       ▼                                                     │
│  Cross-encoder Reranker  (ms-marco-MiniLM-L-6-v2)           │
│       │                                                     │
│       ▼  truncate to top_k=5                                │
│  LLM Context Builder  ([Source: filename] labels)           │
│       │                                                     │
│       ▼                                                     │
│  OpenRouter → Claude  (strict system prompt, max 1024 tok)  │
│       │                                                     │
│       ▼                                                     │
│  QueryResponse  (answer, sources[], latency_ms)             │
└─────────────────────────────────────────────────────────────┘
```

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/documents/upload` | Upload and index a document |
| `GET` | `/api/documents` | List all indexed documents with chunk counts |
| `GET` | `/api/documents/{id}/chunks` | List all chunks for a document |
| `DELETE` | `/api/documents/{id}` | Delete document and all its chunks |
| `POST` | `/api/query` | Ask a question, get answer + cited sources |
| `GET` | `/health` | Liveness check — `{"status": "ok"}` |
| `GET` | `/api/status` | Uptime, config, and DB connectivity |

Interactive docs available at `http://localhost:8000/docs` when running.

### Upload a document

```bash
curl -X POST http://localhost:8000/api/documents/upload \
  -F "file=@report.pdf"
```

```json
{
  "document_id": "3fa85f64-...",
  "filename": "report.pdf",
  "chunk_count": 47,
  "status": "indexed"
}
```

### Ask a question

```bash
curl -X POST http://localhost:8000/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "What is the Q3 revenue?", "k": 5}'
```

```json
{
  "answer": "Q3 revenue was $3.1M, up 35% from Q2 (report.pdf).",
  "sources": [
    {
      "chunk_index": 12,
      "text": "[TABLE 1 | Page 4]\n| Quarter | Revenue |...",
      "score": 0.031746,
      "document_id": "3fa85f64-...",
      "filename": "report.pdf"
    }
  ],
  "latency_ms": 1842.5
}
```

**Query parameters:**

| Field | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | Natural-language question |
| `document_ids` | string[] | null | Restrict to specific documents. Null = search all |
| `k` | int 1–10 | 5 | Top-ranked chunks sent to LLM |

---

## Project Structure

```
docmind/
├── docker-compose.yml          # Postgres + backend services
├── README.md
└── src/
    ├── Dockerfile
    ├── requirements.txt
    ├── .env.example
    ├── main.py                 # FastAPI app, lifespan, CORS, routers
    ├── config.py               # pydantic-settings — all config from .env
    ├── api/
    │   ├── documents.py        # Upload, list, chunks, delete endpoints
    │   └── query.py            # Query endpoint
    ├── db/
    │   └── database.py         # SQLAlchemy engine, ORM models, init_db()
    ├── models/
    │   └── schemas.py          # Pydantic request/response schemas
    └── services/
        ├── parser.py           # Phase 2 document parser (text+tables+images+links)
        ├── chunker.py          # RecursiveCharacterChunker + SemanticChunker
        ├── embedder.py         # SentenceTransformer singleton
        ├── vector_store.py     # pgvector CRUD, similarity_search, hybrid_search
        ├── rag_pipeline.py     # Full RAG orchestration (query → answer)
        └── llm_client.py       # Shared OpenRouter async client singleton
```

---

## Environment Variables

Copy `src/.env.example` to `src/.env` and fill in your values.

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENROUTER_API_KEY` | **Yes** | — | API key from [openrouter.ai/keys](https://openrouter.ai/keys) |
| `OPENROUTER_MODEL` | No | `anthropic/claude-3.5-sonnet` | Any OpenRouter model slug |
| `DATABASE_URL` | No | local postgres | asyncpg connection string |
| `EMBEDDING_MODEL` | No | `all-MiniLM-L6-v2` | Any SentenceTransformers model |
| `TOP_K` | No | `5` | Chunks sent to LLM per query (1–10) |
| `RETRIEVAL_K` | No | `12` | Candidate pool for reranking (should be > TOP_K) |
| `CHUNK_SIZE` | No | `512` | Max characters per chunk |
| `CHUNK_OVERLAP` | No | `50` | Character overlap between chunks |
| `RRF_K` | No | `60` | RRF fusion constant |
| `EXTRACT_IMAGES` | No | `true` | Set `false` to skip vision LLM calls on upload |
| `VISION_MODEL` | No | `anthropic/claude-3.5-sonnet` | Model used for image description (must support vision) |

---

## Running Locally

### Option 1 — Without Docker (recommended for development)

**Prerequisites:** Python 3.11+, a running PostgreSQL instance with pgvector.

#### 1. Start PostgreSQL with pgvector

The easiest way is Docker just for the database:

```bash
docker-compose up postgres -d
```

This starts Postgres on port **5433** (mapped from 5432 inside the container).

#### 2. Install dependencies

```bash
cd src
pip install -r requirements.txt
```

> **Note:** `pymupdf` installs as `fitz`. `pdfplumber` requires no extra system deps on Windows/Mac. On Linux, `unstructured` may need `libmagic` (`apt install libmagic1`).

#### 3. Configure environment

```bash
cp .env.example .env
# Edit .env — set OPENROUTER_API_KEY at minimum
# DATABASE_URL defaults to postgresql+asyncpg://docmind:secret@localhost:5433/docmind
```

#### 4. Run the dev server

```bash
cd src
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

The API is available at `http://localhost:8000`.
Interactive docs at `http://localhost:8000/docs`.

---

### Option 2 — Full Docker Compose

Runs both PostgreSQL and the backend in containers.

#### 1. Set your API key

```bash
# Windows PowerShell
$env:OPENROUTER_API_KEY="sk-or-your-key-here"

# Linux / macOS
export OPENROUTER_API_KEY="sk-or-your-key-here"
```

#### 2. Build and start

```bash
docker-compose up --build
```

This will:
- Pull `pgvector/pgvector:pg16` for Postgres
- Build the Python backend image from `src/Dockerfile`
- Start both services; the backend waits for Postgres to be healthy
- Expose the API on port **8000**

#### 3. Verify

```bash
curl http://localhost:8000/health
# {"status": "ok"}
```

#### Useful Docker commands

```bash
# Start in background
docker-compose up -d --build

# View logs
docker-compose logs -f backend

# Stop everything
docker-compose down

# Stop and remove data volumes
docker-compose down -v
```

---

## Database Schema

```sql
-- Documents table
CREATE TABLE documents (
    id          UUID PRIMARY KEY,
    filename    VARCHAR(512) NOT NULL,
    file_type   VARCHAR(64)  NOT NULL,
    page_count  INTEGER      NOT NULL DEFAULT 0,
    uploaded_at TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Chunks table with pgvector embedding column
CREATE TABLE chunks (
    id          UUID PRIMARY KEY,
    document_id UUID REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER   NOT NULL,
    text        TEXT      NOT NULL,
    embedding   vector(384),          -- all-MiniLM-L6-v2 output
    start_char  BIGINT    NOT NULL,
    end_char    BIGINT    NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- IVFFlat ANN index for vector search
CREATE INDEX ix_chunks_embedding_ivfflat ON chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- GIN index for full-text search
CREATE INDEX ix_chunks_text_gin ON chunks
    USING GIN (to_tsvector('english', text));
```

All indexes are created automatically by `init_db()` on startup.

---

## Key Design Decisions

### Hybrid Search + RRF
Pure vector search misses exact keyword matches (product codes, names, numbers). Pure lexical search misses paraphrases. Hybrid search runs both and merges results using **Reciprocal Rank Fusion**: `score = Σ 1/(k + rank + 1)` where k=60. This is the same algorithm used by Azure AI Search and ElasticSearch.

### Retrieve → Rerank → Truncate
The pipeline fetches `RETRIEVAL_K=12` candidates via hybrid search, passes all 12 to the cross-encoder for precise re-scoring, then truncates to `TOP_K=5` before the LLM. This gives the reranker a larger pool to choose from (better precision) while keeping LLM costs low.

### Phase 2 Document Parsing
Text-only extraction misses a significant portion of document content. DocMind extracts tables (converted to Markdown), images (described by Claude's vision API), and hyperlinks, then assembles them into structured text the LLM can reason over. Set `EXTRACT_IMAGES=false` to skip vision calls during development.

### OpenRouter via OpenAI SDK
The `openai` SDK is pointed at `https://openrouter.ai/api/v1`. To swap models (e.g. from Claude to GPT-4o), change `OPENROUTER_MODEL` in `.env` — no code changes needed.

### No File Storage
The original uploaded file is not stored. Only the extracted text chunks and their embeddings are persisted. This keeps storage simple for v1. A future phase would add object storage (S3/MinIO) for re-indexing and download support.

---

## Running Tests

```bash
cd src
pytest

# Single file with verbose output
pytest tests/test_chunker.py -v
```

---

## What's Built (Feature Status)

| Feature | Status |
|---|---|
| PDF text extraction | ✅ |
| DOCX text extraction | ✅ |
| TXT support | ✅ |
| Table extraction → Markdown | ✅ |
| Image extraction + vision description | ✅ |
| Hyperlink extraction | ✅ |
| Recursive character chunker | ✅ |
| Semantic chunker (embedding-based) | ✅ available, not wired by default |
| Batch embedding (all-MiniLM-L6-v2) | ✅ |
| pgvector ANN index (IVFFlat) | ✅ |
| Vector similarity search | ✅ |
| Full-text search (PostgreSQL FTS + GIN) | ✅ |
| Hybrid search with RRF | ✅ |
| Cross-encoder reranking | ✅ |
| Retrieve → rerank → truncate pipeline | ✅ |
| Meta-query guard (identity / chitchat) | ✅ |
| Scope enforcement (no code generation) | ✅ |
| OpenRouter LLM integration | ✅ |
| Document CRUD API | ✅ |
| Chunk listing API | ✅ |
| CORS middleware | ✅ |
| Structured logging | ✅ |
| Docker Compose setup | ✅ |
| File storage (S3/MinIO) | ⬜ planned |
| Re-indexing without re-upload | ⬜ planned |
| Authentication / API keys | ⬜ planned |
| Rate limiting | ⬜ planned |
| Async background indexing | ⬜ planned |

---

## Challenges & How I Solved Them with my Pair Programmer, Claude Code !

This section documents the real decisions and failures encountered while building DocMind — not just bugs, but design choices that evolved through observation, testing, and iterating on actual LLM behaviour.

---

### 1. Plain text extraction wasn't enough — built Phase 2 parsing

The first version of the parser extracted only text using `pypdf` and `python-docx`. Real documents are not just paragraphs — they contain tables with structured data, charts, and hyperlinks.

When users asked questions like "what was Q3 revenue?" or "what does the diagram show?", retrieval either failed completely or returned chunks that mentioned a table existed without containing its data.

**What we built:**
- **Tables** — `pdfplumber` extracts table cells per PDF page; `python-docx` reads table rows natively. Both serialised to Markdown (`| col | col |`) so the LLM reasons over values accurately.
- **Images** — `pymupdf` extracts embedded image bytes; each image is sent to Claude's vision API concurrently via `asyncio.gather` and embedded inline as `[IMAGE 1 | Page 3]: <description>`.
- **Hyperlinks** — `pypdf` walks `/Annots` objects for URI actions; `python-docx` reads `w:hyperlink` elements from the XML.

Everything is assembled into one enriched text string that the chunker and embedder consume without modification. Tables and image descriptions become fully searchable.

---

### 2. Vector search alone missed exact matches — moved to hybrid search

Pure semantic (vector) search excels at paraphrases but silently fails on exact terms — product codes, names, numbers, technical identifiers. Pure lexical search has the opposite problem.

**Solution:** Hybrid search combining both arms, fused with **Reciprocal Rank Fusion (RRF)**:

```
Vector arm   → top-N by cosine ANN  (pgvector IVFFlat index)
Lexical arm  → top-N by ts_rank_cd  (PostgreSQL GIN index + websearch_to_tsquery)
RRF fusion   → score = Σ 1 / (60 + rank + 1)  across both arms
```

RRF is rank-based, not score-based — it handles the fact that cosine similarity and BM25 scores live on completely different scales. A chunk that ranks #1 in both arms gets the highest fused score regardless of what the raw numbers were.

---

### 3. The system prompt went through multiple iterations — each driven by a real observed failure

The system prompt was not designed upfront. It was refined each time the LLM produced a wrong or unsafe output. Here is the exact sequence:

**Iteration 1 — Initial prompt (too basic)**

The prompt said: `Always cite the source chunk index (e.g., [Chunk 3]).`

`[Chunk 3]` means nothing to a user — it exposes a pipeline implementation detail. Removed chunk citations entirely, replaced with filename citations: `(report.pdf)`.

**Iteration 2 — Static rejection string**

The prompt had a hardcoded fallback: `"I don't have enough information in the provided documents."`

This fired even on partial matches, cutting off useful partial answers. Replaced with an open-ended instruction — answer what you can, state what is missing, suggest a rephrased question.

**Iteration 3 — No negative constraints → LLM went out of scope**

When asked *"Write code to find if a number is prime"*, the LLM correctly said the documents didn't contain it — then added: *"I can write one for you."* It slipped into being a general-purpose assistant.

The prompt only said what to do. Added an explicit **Hard scope limits** block:
```
- Do NOT write, generate, or produce any content not derived from uploaded documents.
- Do NOT offer to generate something as an alternative when documents lack the answer.
- Do NOT act as a general-purpose assistant.
```

**Iteration 4 — Identity confusion from retrieved context**

With a resume uploaded, asking *"Who are you?"* retrieved chunks like *"I'm Viral Deshle, a full-stack developer..."* — and the LLM answered as the person in the document. The system prompt instruction about identity couldn't compete with a strongly relevant retrieved chunk.

System prompt instructions cannot reliably override retrieved context. Fixed structurally with a **pre-retrieval meta-query guard**:

```python
if _is_meta_query(query):   # "who are you", "what can you do", "hello", etc.
    return QueryResponse(answer=_DOCMIND_INTRO, sources=[], ...)
```

No retrieval runs. No context is built. Response is deterministic every time.

**Result — the final system prompt has three layers:**
1. Positive instructions (what to do, how to cite, how to handle tables and images)
2. Hard negative constraints (what never to do under any circumstance)
3. Fallback guidance (how to respond when documents don't cover the question)

---

### 4. Sending too many chunks to the LLM was expensive and noisy

The frontend was passing `k=12` and all 12 chunks went to the LLM context. Two problems:

- **Cost:** 12 chunks × ~512 chars each × query volume adds up fast in token billing.
- **Quality:** Lower-ranked chunks (positions 8–12) are tangentially related at best. Sending them introduces noise that dilutes the answer.

The cross-encoder reranker made this worse — it was reranking a pool of 12 to return 12. The reranker's value is precision, not recall. It needs more candidates than the final answer requires.

**Solution — split `retrieval_k` from `context_k`:**

```
RETRIEVAL_K = 12   →  hybrid search fetches 12 candidates
                   →  cross-encoder reranks all 12  (large pool = better precision)
                   →  truncate to top 5
TOP_K = 5          →  only the 5 best chunks go to the LLM
```

The reranker now does its best work sorting 12 to find the true top 5. The LLM only sees the highest-confidence chunks. Token cost drops ~60% for equivalent or better answer quality.

---

### 5. Scores showing as ~1.6% — misleading but technically correct

After hybrid search was wired in, the API showed `"similarity": 0.016` on every result. It looked like retrieval was broken — scores near zero suggest completely unrelated content.

Real cause: the `similarity` field had been repurposed to carry the **RRF fusion score**, not cosine similarity. An RRF score for rank 0 with k=60 is always `1/(60+0+1) = 0.0163`. Correct score, wrong field name.

**Fix:** Renamed `similarity` → `score` across the Pydantic schema, all internal dicts, and logging. The API contract now accurately describes what the number represents.

When a pipeline change repurposes a field's semantics, rename it immediately. A misleading field name in a public schema propagates confusion to every consumer — frontend, tests, logs, and future developers.

---

### 6. Missing route discovered by the frontend in production — 404 on chunks

The frontend called `GET /api/documents/{id}/chunks` to display indexed content. The backend returned 404 — the route had never been defined.

Fix was straightforward: add the schema, DB query, and route. The deeper lesson: when building a backend-only API where the frontend is a separate project, route gaps are invisible until the frontend exercises them. The fix also surfaced a FastAPI subtlety — `GET /documents/{id}/chunks` must be registered **before** `DELETE /documents/{id}`, otherwise FastAPI captures `chunks` as the `document_id` path parameter and the wrong handler runs.

---

### 7. `text().label()` crashed SQLAlchemy — a surprisingly common pitfall

When the PostgreSQL full-text search rank expression was written as:

```python
text("ts_rank_cd(to_tsvector('english', chunks.text), ...)").label("fts_rank")
# ❌ NotImplementedError
```

`text()` returns a `TextClause` — an opaque SQL string SQLAlchemy cannot introspect. It has no concept of column labels or ordering.

**Fix:** `func.*` constructs return proper `ColumnElement` objects that support `.label()`, `.desc()`, and operator composition. User query bound via `bindparam()` to prevent SQL injection:

```python
func.ts_rank_cd(
    func.to_tsvector(literal_column("'english'"), Chunk.text),
    func.websearch_to_tsquery(literal_column("'english'"), bindparam("fts_query", value=query))
).label("fts_rank")
```

Rule of thumb: use `text()` only for SQL fragments that never need to be composed further. Use `func.*` for anything that participates in the expression API.
