"""gRPC client for the WeKnora docreader service (image used unmodified)."""

from __future__ import annotations

import json
import logging
import uuid

import grpc

from ..config import get_settings
from . import Asset, ParseError, ParseResult, SourceBlock

logger = logging.getLogger(__name__)

_channel: grpc.aio.Channel | None = None


def _stub():
    from .._proto import docreader_pb2_grpc

    global _channel
    if _channel is None:
        limit = get_settings().docreader_max_message_mb * 1024 * 1024
        _channel = grpc.aio.insecure_channel(
            get_settings().docreader_addr,
            options=[
                ("grpc.max_send_message_length", limit),
                ("grpc.max_receive_message_length", limit),
            ],
        )
    return docreader_pb2_grpc.DocReaderStub(_channel)


async def aclose() -> None:
    global _channel
    if _channel is not None:
        await _channel.close()
        _channel = None


def _blocks(raw) -> list[SourceBlock]:
    out: list[SourceBlock] = []
    for block in raw:
        try:
            locator = json.loads(block.locator_json) if block.locator_json else {}
        except json.JSONDecodeError:
            locator = {}
        if block.end > block.start:
            out.append(SourceBlock(int(block.start), int(block.end), locator))
    return out


async def _read(request, *, engine: str) -> ParseResult:
    settings = get_settings()
    meta = None
    assets: list[Asset] = []
    try:
        stream = _stub().ReadStream(request, timeout=settings.docreader_timeout_s)
        async for frame in stream:
            kind = frame.WhichOneof("payload")
            if kind == "meta":
                meta = frame.meta
            elif kind == "image":
                image = frame.image
                name = image.original_ref or image.filename
                if image.image_data and name:
                    assets.append(Asset(name=name, data=bytes(image.image_data), mime_type=image.mime_type))
    except grpc.aio.AioRpcError as exc:
        retriable = exc.code() in {
            grpc.StatusCode.UNAVAILABLE,
            grpc.StatusCode.DEADLINE_EXCEEDED,
            grpc.StatusCode.RESOURCE_EXHAUSTED,
        }
        raise ParseError("docreader_error", f"docreader: {exc.code().name} {exc.details()}", retriable=retriable) from exc
    if meta is None:
        raise ParseError("docreader_error", "docreader 没有返回解析结果", retriable=True)
    if meta.error:
        raise ParseError("parse_failed", meta.error)
    return ParseResult(
        markdown=meta.markdown_content,
        engine=engine,
        source_blocks=_blocks(meta.source_blocks),
        assets=assets,
        metadata=dict(meta.metadata),
        title=dict(meta.metadata).get("title", ""),
    )


async def read_file(file_name: str, file_type: str, data: bytes, *, engine: str = "builtin") -> ParseResult:
    from .._proto import docreader_pb2

    request = docreader_pb2.ReadRequest(
        file_content=data,
        file_name=file_name,
        file_type=file_type,
        config=docreader_pb2.ReadConfig(parser_engine=engine),
        request_id=uuid.uuid4().hex,
    )
    return await _read(request, engine=f"docreader-{engine}")


async def list_engines() -> list[dict]:
    from .._proto import docreader_pb2

    try:
        resp = await _stub().ListEngines(docreader_pb2.ListEnginesRequest(), timeout=15)
    except grpc.aio.AioRpcError as exc:
        logger.warning("docreader ListEngines failed: %s", exc.code())
        return []
    return [
        {
            "name": e.name,
            "description": e.description,
            "file_types": list(e.file_types),
            "available": e.available,
            "unavailable_reason": e.unavailable_reason,
        }
        for e in resp.engines
    ]


async def read_url(url: str) -> ParseResult:
    from .._proto import docreader_pb2

    request = docreader_pb2.ReadRequest(
        url=url,
        config=docreader_pb2.ReadConfig(parser_engine="builtin"),
        request_id=uuid.uuid4().hex,
    )
    return await _read(request, engine="docreader-web")
