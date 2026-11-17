"""Speech to text for audio files (KBS_ASR_*).

Uses the OpenAI-compatible ``POST {base}/audio/transcriptions`` call that the
DA knowledge pipeline platform sends to its local Whisper service and to remote
Whisper providers. Segments of a ``verbose_json`` answer become one line each,
tied to their time range; a plain ``{"text": ...}`` answer is kept as is.
"""

from __future__ import annotations

import httpx

from ..config import get_settings
from . import ParseError, ParseResult, SourceBlock, service_slot

_CONNECT_TIMEOUT_S = 15.0
# Tests put an httpx.MockTransport here.
_transport: httpx.AsyncBaseTransport | None = None


async def transcribe(file_name: str, data: bytes) -> ParseResult:
    settings = get_settings()
    url = settings.asr_api_url.strip().rstrip("/")
    if not url:
        raise ParseError("asr_error", "已开启语音转写（KBS_ASR_ENABLED），但没有配置地址 KBS_ASR_API_URL")
    if not url.endswith("/audio/transcriptions"):
        url += "/audio/transcriptions"
    headers = {"Authorization": f"Bearer {settings.asr_api_key}"} if settings.asr_api_key else {}
    form = {"model": settings.asr_model, "response_format": "verbose_json"}
    timeout = httpx.Timeout(settings.asr_timeout_s, connect=_CONNECT_TIMEOUT_S)
    async with service_slot("asr", settings.asr_concurrency, settings.asr_timeout_s):
        try:
            async with httpx.AsyncClient(transport=_transport, trust_env=False, timeout=timeout) as client:
                resp = await client.post(url, data=form, files={"file": (file_name, data)}, headers=headers)
        except httpx.TimeoutException as exc:
            raise ParseError(
                "asr_error", f"语音转写在 {settings.asr_timeout_s:g} 秒内没有完成（KBS_ASR_TIMEOUT_S），录音较长时可调大后重新解析"
            ) from exc
        except httpx.HTTPError as exc:
            raise ParseError("asr_error", f"连不上语音转写服务（{type(exc).__name__}），稍后会重试", retriable=True) from exc
    if not resp.is_success:
        retriable = resp.status_code >= 500 or resp.status_code == 429
        detail = resp.text.strip()[:300]
        raise ParseError(
            "asr_error" if retriable else "parse_failed",
            f"语音转写失败（HTTP {resp.status_code}{'：' + detail if detail else ''}）",
            retriable=retriable,
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise ParseError("parse_failed", "语音转写服务返回的不是 JSON") from exc
    if not isinstance(body, dict):
        body = {}
    markdown, blocks = _segments(body.get("segments"))
    if not markdown:
        markdown = str(body.get("text") or "").strip()
    if not markdown:
        raise ParseError("empty_content", "语音转写没有得到文字（录音可能是静音或纯音乐）")
    metadata = {"asr_model": settings.asr_model}
    language = body.get("language")
    if isinstance(language, str):
        metadata["language"] = language
    return ParseResult(markdown=markdown, engine="asr", source_blocks=blocks, metadata=metadata)


def _segments(raw) -> tuple[str, list[SourceBlock]]:
    lines: list[str] = []
    blocks: list[SourceBlock] = []
    offset = 0
    for segment in raw if isinstance(raw, list) else []:
        text = str(segment.get("text") or "").strip() if isinstance(segment, dict) else ""
        if not text:
            continue
        if lines:
            offset += 1
        locator: dict = {"type": "audio"}
        start, end = segment.get("start"), segment.get("end")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            locator.update(start_ms=int(start * 1000), end_ms=int(end * 1000))
        blocks.append(SourceBlock(offset, offset + len(text), locator))
        lines.append(text)
        offset += len(text)
    return "\n".join(lines), blocks
