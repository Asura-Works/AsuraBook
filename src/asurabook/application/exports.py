"""帳票ファイル出力 (15章9節)。

出力は export root 配下に限定し、canonical path・symlink/junction・管理領域を検査する。
既定は no-clobber。安全な一時fileへ書いてから atomic rename する。
CSVは表計算向けの数式注入を防ぐ。
"""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path

from ..domain.errors import AsuraBookError, invalid
from ..domain.values import new_id
from ..infrastructure.paths import AppPaths

_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: object) -> object:
    if isinstance(value, (int, float)) or value is None:
        return "" if value is None else value
    text = str(value)
    if text.startswith(_FORMULA_PREFIX):
        return "'" + text
    return text


def resolve_export_path(paths: AppPaths, export_root: Path, requested: str) -> Path:
    root = export_root.resolve()
    candidate = Path(requested)
    if not candidate.is_absolute():
        candidate = root / candidate
    # 存在する親は symlink/junction を解決して比較する
    parent = candidate.parent
    if not parent.exists():
        raise invalid("出力先フォルダがありません", path=str(parent))
    real_parent = parent.resolve(strict=True)
    try:
        real_parent.relative_to(root)
    except ValueError:
        raise AsuraBookError("EXPORT_PATH_DENIED", "出力先は export root 配下に限られます",
                           {"export_root": str(root), "path": str(candidate)}) from None
    target = real_parent / candidate.name
    if os.path.islink(target) or (target.exists() and target.resolve() != target):
        raise AsuraBookError("EXPORT_PATH_DENIED", "symlink/junction への出力は拒否します", {"path": str(target)})
    for protected in paths.protected_paths():
        p = protected.resolve() if protected.exists() else protected
        if target == p or p in target.parents:
            raise AsuraBookError("EXPORT_PATH_DENIED", "管理領域 (DB/原本/設定) へは出力できません", {"path": str(target)})
    if target.suffix.lower() in (".db", ".sqlite", ".exe", ".dll", ".bat", ".cmd", ".ps1"):
        raise AsuraBookError("EXPORT_PATH_DENIED", "この拡張子には出力できません", {"path": str(target)})
    if target.exists():
        raise AsuraBookError("EXPORT_EXISTS", "同名ファイルがあります (上書きしません)", {"path": str(target)})
    return target


def write_atomic(target: Path, data: bytes) -> None:
    tmp = target.with_name(f".{target.name}.{new_id()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # no-clobber: POSIX の link と Windows の rename は既存の同名を置換せず失敗する
        try:
            if os.name == "nt":
                os.rename(tmp, target)
            else:
                os.link(tmp, target)
        except FileExistsError:
            raise AsuraBookError("EXPORT_EXISTS", "同名ファイルがあります (上書きしません)",
                               {"path": str(target)}) from None
    finally:
        if tmp.exists():
            tmp.unlink()


def rows_to_csv(header: list[str], rows: list[list[object]], *, bom: bool = True) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([safe_cell(h) for h in header])
    for r in rows:
        writer.writerow([safe_cell(v) for v in r])
    text = buf.getvalue()
    return (("﻿" if bom else "") + text).encode("utf-8")
