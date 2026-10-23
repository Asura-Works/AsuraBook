"""schema migration。正本は migrations/*.sql の1系統 (20章3節)。

schema_version は単調に上げ、未知の新版を旧appで書かない (03章10節)。
"""

from __future__ import annotations

import re
import sqlite3
from importlib import resources

from .connection import DatabaseError

_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def available_migrations() -> list[tuple[int, str, str]]:
    root = resources.files("asurabook.infrastructure.db") / "migrations"
    found = []
    for entry in root.iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            found.append((int(m.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    return sorted(found)


def latest_version() -> int:
    migrations = available_migrations()
    return migrations[-1][0] if migrations else 0


def current_version(conn: sqlite3.Connection) -> int:
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if not has_meta:
        return 0
    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    return int(row[0]) if row else 0


def ensure_compatible(conn: sqlite3.Connection) -> int:
    version = current_version(conn)
    if version > latest_version():
        raise DatabaseError(
            f"DB schema_version {version} はこのアプリ (対応 {latest_version()}) より新しいため書き込みません"
        )
    return version


def migrate(conn: sqlite3.Connection) -> int:
    """未適用のmigrationを排他transactionで順に適用する。"""
    version = ensure_compatible(conn)
    for number, name, sql in available_migrations():
        if number <= version:
            continue
        # executescript は暗黙COMMITするため、文ごとに分割せず BEGIN を含めて渡す
        script = f"BEGIN IMMEDIATE;\n{sql}\n" + (
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '{n}') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value;\nCOMMIT;"
        ).format(n=number)
        try:
            conn.executescript(script)
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise DatabaseError(f"migration {name} に失敗しました: {exc}") from exc
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise DatabaseError(f"migration {name} 後に外部キー違反があります: {len(violations)}件")
        version = number
    return version
