"""
Thin wrapper over the OpenRouter chat API used by the summarizer and the agent.

``chat()`` returns content, tool calls and a token count so callers can enforce
budgets. Tests monkeypatch ``chat`` instead of the HTTP client.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from config import settings
from services.llm_client import get_openrouter_client

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens: int = 0
    message: dict[str, Any] = field(default_factory=dict)  # assistant message to append to history


def estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


async def chat(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    tools: list[dict[str, Any]] | None = None,
) -> LLMResult:
    kwargs: dict[str, Any] = {
        "model": model or settings.openrouter_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    response = await get_openrouter_client().chat.completions.create(**kwargs)
    msg = response.choices[0].message
    content = msg.content or ""

    calls: list[ToolCall] = []
    raw_calls: list[dict[str, Any]] = []
    for tc in msg.tool_calls or []:
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args if isinstance(args, dict) else {}))
        raw_calls.append({
            "id": tc.id,
            "type": "function",
            "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
        })

    usage = getattr(response, "usage", None)
    tokens = (
        int(getattr(usage, "total_tokens", 0) or 0)
        if usage
        else estimate_tokens(json.dumps(messages)) + estimate_tokens(content)
    )
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if raw_calls:
        message["tool_calls"] = raw_calls
    return LLMResult(content=content, tool_calls=calls, tokens=tokens, message=message)


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json(text: str) -> Any:
    """Best-effort JSON extraction from a model reply (handles code fences and chatter)."""
    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(open_ch), cleaned.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"No JSON found in model output: {text[:200]!r}")
