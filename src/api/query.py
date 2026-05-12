"""
Query endpoint — accepts a natural-language question and returns an AI-generated
answer with cited source chunks.

Routes (mounted at /api by main.py):
  POST /query
"""

from __future__ import annotations

import logging

import openai
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import get_db
from models.schemas import QueryRequest, QueryResponse
from services.rag_pipeline import answer_query

logger = logging.getLogger(__name__)

router = APIRouter(tags=["query"])


@router.post(
    "/query",
    response_model=QueryResponse,
    summary="Ask a question across indexed documents",
)
async def query_documents(
    request: QueryRequest,
    session: AsyncSession = Depends(get_db),
) -> QueryResponse:
    """
    Run the RAG pipeline:
      1. Embed the query
      2. Retrieve top-K similar chunks (optionally filtered by document_ids)
      3. Rerank with cross-encoder
      4. Generate answer with Claude
      5. Return answer + cited sources + latency_ms
    """
    logger.info(
        "Query received: '%s' (k=%d, document_ids=%s)",
        request.query[:80],
        request.k,
        request.document_ids,
    )

    try:
        response = await answer_query(
            query=request.query,
            document_ids=request.document_ids,
            k=request.k,
            session=session,
        )
    except openai.APIStatusError as exc:
        logger.error("OpenRouter API status error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM API error: {exc.message}",
        )
    except openai.APIConnectionError as exc:
        logger.error("OpenRouter API connection error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not connect to the LLM API. Please try again.",
        )
    except Exception as exc:
        logger.exception("Unexpected error during query: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while processing the query.",
        )

    return response
