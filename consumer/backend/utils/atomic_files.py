# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Small, ownership-safe helpers for atomic file staging."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
from typing import Iterator


@contextmanager
def staged_bytes(
    *,
    directory: str | os.PathLike[str],
    prefix: str,
    suffix: str,
    payload: bytes,
) -> Iterator[Path]:
    """Write durable staged bytes and always release the temp resource."""

    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=prefix,
        suffix=suffix,
        dir=directory,
    )
    temporary = Path(temporary_name)
    try:
        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        yield temporary
    finally:
        try:
            if descriptor is not None:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)


__all__ = ["staged_bytes"]
