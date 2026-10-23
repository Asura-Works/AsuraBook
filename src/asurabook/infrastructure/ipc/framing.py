"""IPC framing (02章3節): 4byte big-endian 長 + UTF-8 JSON。最大frameは 8MiB。

画像や巨大CSV本文は流さず、管理object参照 (path・ID) を渡す。
"""

from __future__ import annotations

import json
import struct
from typing import Any, BinaryIO

MAX_FRAME = 8 * 1024 * 1024
_HEADER = struct.Struct(">I")


class FrameError(Exception):
    pass


def encode(obj: Any) -> bytes:
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise FrameError(f"frame が上限 {MAX_FRAME} bytes を超えています")
    return _HEADER.pack(len(body)) + body


def _read_exact(stream: BinaryIO, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            raise EOFError("接続が切れました")
        buf += chunk
    return buf


def read_frame(stream: BinaryIO) -> Any:
    (length,) = _HEADER.unpack(_read_exact(stream, _HEADER.size))
    if length > MAX_FRAME:
        raise FrameError("frame が上限を超えています")
    body = _read_exact(stream, length)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FrameError("frame が UTF-8 JSON ではありません") from exc


def write_frame(stream: BinaryIO, obj: Any) -> None:
    stream.write(encode(obj))
    stream.flush()
