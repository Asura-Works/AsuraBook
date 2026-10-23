"""SQLite接続とtransaction。

02章5節: WAL、foreign_keys=ON、synchronous=FULL、busy_timeout=5000ms。
Python sqlite3 の暗黙transactionには依存せず、autocommit (isolation_level=None) で
BEGIN IMMEDIATE / COMMIT / ROLLBACK を明示する。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

MIN_SQLITE_VERSION = (3, 37, 0)  # STRICT table の最低版

PRAGMAS = {
    "foreign_keys": 1,
    "synchronous": 2,  # FULL
    "busy_timeout": 5000,
}


class DatabaseError(Exception):
    pass


def sqlite_runtime_version() -> tuple[int, ...]:
    return tuple(int(p) for p in sqlite3.sqlite_version.split("."))


def check_runtime() -> dict:
    """配布Pythonが実際に使うSQLite版を検査する (02章5節)。"""
    version = sqlite_runtime_version()
    return {
        "sqlite_version": sqlite3.sqlite_version,
        "strict_supported": version >= MIN_SQLITE_VERSION,
        "backup_api": hasattr(sqlite3.Connection, "backup"),
    }


def _apply_pragmas(conn: sqlite3.Connection, *, readonly: bool) -> None:
    if not readonly:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() not in ("wal", "memory"):
            raise DatabaseError(f"WAL を有効化できません (journal_mode={mode})")
    for name, value in PRAGMAS.items():
        conn.execute(f"PRAGMA {name}={value}")
    # 各connectionでPRAGMAを確認する
    for name, value in PRAGMAS.items():
        actual = conn.execute(f"PRAGMA {name}").fetchone()[0]
        if int(actual) != value:
            raise DatabaseError(f"PRAGMA {name} が {value} になっていません (実値 {actual})")


def connect(path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    if sqlite_runtime_version() < MIN_SQLITE_VERSION:
        raise DatabaseError(f"SQLite {sqlite3.sqlite_version} は STRICT table に未対応です")
    if str(path) == ":memory:":
        conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    else:
        p = Path(path)
        if readonly:
            if not p.exists():
                raise DatabaseError(f"DBがありません: {p}")
            uri = p.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
        else:
            conn = sqlite3.connect(str(p), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn, readonly=readonly or str(path) == ":memory:")
    return conn


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT。例外時は必ず ROLLBACK する。"""
    if conn.in_transaction:
        raise DatabaseError("入れ子のtransactionは使いません")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """帳票等で金額と件数を同じ read snapshot から読む (14章7節)。"""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN DEFERRED")
    try:
        yield conn
    finally:
        conn.execute("COMMIT")
