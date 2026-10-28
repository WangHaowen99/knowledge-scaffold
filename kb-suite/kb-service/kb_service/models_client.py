"""OpenAI-compatible clients for embedding, rerank and chat (summary)."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Sequence

import httpx

from .config import get_settings

logger = logging.getLogger(__name__)

_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class ModelError(Exception):
    def __init__(self, message: str, *, retriable: bool) -> None:
        super().__init__(message)
        self.retriable = retriable


_client: httpx.AsyncClient | None = None
_embed_sem: asyncio.Semaphore | None = None
_llm_sem_obj: asyncio.Semaphore | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0), trust_env=False)
    return _client


def _sem() -> asyncio.Semaphore:
    global _embed_sem
    if _embed_sem is None:
        _embed_sem = asyncio.Semaphore(get_settings().embed_concurrency)
    return _embed_sem


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _post_json(url: str, body: dict, *, attempts: int = 4) -> dict:
    settings = get_settings()
    if not settings.llm_api_key:
        raise ModelError("model API key is not configured", retriable=False)
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    delay = 1.0
    last: str = ""
    for attempt in range(1, attempts + 1):
        try:
            resp = await _http().post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code < 300:
                return resp.json()
            last = f"HTTP {resp.status_code}: {resp.text[:300]}"
            if resp.status_code not in _RETRY_STATUS:
                raise ModelError(last, retriable=False)
        if attempt < attempts:
            await asyncio.sleep(delay + random.random() * 0.5)
            delay *= 2
    raise ModelError(last or "model request failed", retriable=True)


def _base() -> str:
    return get_settings().llm_base_url.rstrip("/")


async def embed(texts: Sequence[str]) -> list[list[float]]:
    settings = get_settings()
    out: list[list[float]] = []
    for start in range(0, len(texts), settings.embed_batch):
        batch = [t if t.strip() else " " for t in texts[start : start + settings.embed_batch]]
        async with _sem():
            data = await _post_json(
                f"{_base()}/embeddings",
                {"model": settings.embed_model, "input": batch, "dimensions": settings.embed_dim},
            )
        items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
        if len(items) != len(batch):
            raise ModelError("embedding response size mismatch", retriable=True)
        out.extend(item["embedding"] for item in items)
    return out


async def rerank(query: str, documents: Sequence[str], top_n: int) -> list[tuple[int, float]]:
    settings = get_settings()
    if not settings.rerank_url or not documents:
        return []
    data = await _post_json(
        settings.rerank_url,
        {
            "model": settings.rerank_model,
            "query": query,
            "documents": list(documents),
            "top_n": min(top_n, len(documents)),
        },
        attempts=2,
    )
    results = data.get("results") or data.get("output", {}).get("results") or []
    return [(int(r["index"]), float(r.get("relevance_score", 0.0))) for r in results]


def _llm_sem() -> asyncio.Semaphore:
    global _llm_sem_obj
    if _llm_sem_obj is None:
        _llm_sem_obj = asyncio.Semaphore(get_settings().llm_concurrency)
    return _llm_sem_obj


async def complete(
    messages: list[dict],
    *,
    model: str = "",
    max_tokens: int = 600,
    temperature: float = 0.2,
    json_mode: bool = False,
) -> tuple[str, str]:
    """One chat completion; returns (content, finish_reason)."""

    settings = get_settings()
    body: dict = {
        "model": model or settings.summary_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    async with _llm_sem():
        try:
            data = await _post_json(f"{_base()}/chat/completions", body)
        except ModelError as exc:
            if not json_mode or exc.retriable or "response_format" not in str(exc):
                raise
            body.pop("response_format")
            data = await _post_json(f"{_base()}/chat/completions", body)
    choices = data.get("choices") or []
    if not choices:
        raise ModelError("chat response has no choices", retriable=True)
    choice = choices[0]
    return (choice.get("message", {}).get("content") or "").strip(), str(choice.get("finish_reason") or "")


async def chat(
    prompt: str,
    *,
    system: str = "",
    max_tokens: int = 600,
    model: str = "",
    temperature: float = 0.2,
    json_mode: bool = False,
) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    text, _ = await complete(
        messages, model=model, max_tokens=max_tokens, temperature=temperature, json_mode=json_mode
    )
    return text


CONTINUE_PROMPT = (
    "Your previous message was cut off because it reached the output length limit. Continue the SAME "
    "message from exactly where it stopped. Do NOT repeat anything you already wrote; your reply is "
    "appended verbatim. If you stopped in the middle of a Markdown table row, finish that row first. "
    "Output the continuation only, with no preamble. If the message was already complete, reply with "
    "exactly: (complete)"
)


async def chat_long(
    messages: list[dict],
    *,
    model: str = "",
    max_tokens: int = 4096,
    temperature: float = 0.3,
    max_rounds: int = 3,
) -> tuple[str, bool]:
    """Chat that resumes on ``finish_reason=length``; returns (text, complete)."""

    text, reason = await complete(messages, model=model, max_tokens=max_tokens, temperature=temperature)
    rounds = 0
    while reason == "length" and rounds < max_rounds:
        rounds += 1
        more, reason = await complete(
            [*messages, {"role": "assistant", "content": text}, {"role": "user", "content": CONTINUE_PROMPT}],
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if more.strip() == "(complete)":
            reason = "stop"
            break
        text += more
    return text, reason != "length"
