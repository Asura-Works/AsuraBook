"""原本byte列の保全 (05章3節、08章1節)。

内容アドレス (SHA-256) で spool に保存し、同内容の再投入は同じ原本を再利用する。
一時fileへ書いて fsync してから rename する。コピー元は削除しない。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from ..domain.errors import TaxDeskError
from ..domain.values import new_id

MAX_BYTES = 25 * 1024 * 1024  # 08章: 初期設計上限 25MiB

_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF-", "application/pdf"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_mime(data: bytes, filename: str = "") -> str:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc"):
        return "image/heic"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if filename.lower().endswith((".csv", ".txt")):
        return "text/csv"
    return "application/octet-stream"


@dataclass(frozen=True)
class StoredObject:
    sha256: str
    size_bytes: int
    mime: str
    local_ref: str  # spool からの相対path


def read_input(path: Path, *, max_bytes: int = MAX_BYTES) -> bytes:
    if not path.is_file():
        raise TaxDeskError("NOT_FOUND", "ファイルがありません", {"path": str(path)})
    size = path.stat().st_size
    if size > max_bytes:
        raise TaxDeskError("FILE_TOO_LARGE", f"ファイルが上限 {max_bytes // (1024 * 1024)}MiB を超えています",
                           {"path": str(path), "size_bytes": size})
    data = path.read_bytes()
    # 読取後再確認: コピー途中のファイルを読み始めない
    if path.stat().st_size != len(data):
        raise TaxDeskError("FILE_UNSTABLE", "読取中にファイルが変化しました。コピー完了後に再投入してください",
                           {"path": str(path)})
    return data


def store(spool: Path, kind: str, data: bytes, filename: str = "", *, write: bool = True) -> StoredObject:
    digest = hashlib.sha256(data).hexdigest()
    rel = Path(kind) / digest[:2] / digest
    target = spool / rel
    if write and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{digest}.{new_id()}.tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    if write:
        stored = target.read_bytes()
        if hashlib.sha256(stored).hexdigest() != digest:
            raise TaxDeskError("INTEGRITY_ERROR", "spool の原本 hash が一致しません", {"sha256": digest})
    return StoredObject(digest, len(data), sniff_mime(data, filename), rel.as_posix())


def open_object(spool: Path, local_ref: str) -> bytes:
    p = (spool / local_ref).resolve()
    if spool.resolve() not in p.parents:
        raise TaxDeskError("INTEGRITY_ERROR", "spool 外の参照です")
    return p.read_bytes()
