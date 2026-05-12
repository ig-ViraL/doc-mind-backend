"""
Shared OpenRouter async client singleton.
Import get_openrouter_client() anywhere that needs to call the LLM API.
"""

from __future__ import annotations

import openai

from config import settings

_client: openai.AsyncOpenAI | None = None


def get_openrouter_client() -> openai.AsyncOpenAI:
    global _client
    if _client is None:
        _client = openai.AsyncOpenAI(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            default_headers={
                "HTTP-Referer": "https://github.com/docmind",
                "X-Title": "DocMind RAG",
            },
        )
    return _client
