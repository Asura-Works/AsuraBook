"""参照の解決と計上gateの共通処理。表示aliasは内部IDへ一意に解決し、曖昧ならerror。"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..domain.errors import AsuraBookError, invalid, not_found
from ..domain.values import parse_date


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def resolve_account(conn: sqlite3.Connection, ref: Any) -> sqlite3.Row:
    if not isinstance(ref, str) or not ref:
        raise invalid("account は科目コードまたはIDで指定してください", value=ref)
    row = conn.execute("SELECT * FROM accounts WHERE id=?", (ref,)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM accounts WHERE code=?", (ref,)).fetchone()
    if row is None:
        rows = conn.execute("SELECT * FROM accounts WHERE name=?", (ref,)).fetchall()
        if len(rows) > 1:
            raise AsuraBookError("AMBIGUOUS_REFERENCE", f"科目名 {ref} が複数あります。コードで指定してください")
        row = rows[0] if rows else None
    if row is None:
        raise not_found("account", ref)
    return row


def resolve_subaccount(conn: sqlite3.Connection, account_id: str, ref: Any) -> sqlite3.Row | None:
    if ref is None:
        return None
    row = conn.execute("SELECT * FROM subaccounts WHERE account_id=? AND (id=? OR code=?)",
                       (account_id, ref, ref)).fetchone()
    if row is None:
        raise not_found("subaccount", str(ref))
    return row


def resolve_year(conn: sqlite3.Connection, year: Any) -> sqlite3.Row:
    if isinstance(year, bool) or not isinstance(year, int):
        if isinstance(year, str) and year.isdigit():
            year = int(year)
        else:
            raise invalid("year は西暦の整数です", value=year)
    row = conn.execute("SELECT * FROM fiscal_years WHERE year=?", (year,)).fetchone()
    if row is None:
        raise not_found("fiscal_year", str(year))
    return row


def year_for_date(conn: sqlite3.Connection, journal_date: str, requested_year: Any = None) -> sqlite3.Row:
    parse_date(journal_date, "journal_date")
    row = conn.execute("SELECT * FROM fiscal_years WHERE start_date <= ? AND end_date >= ?",
                       (journal_date, journal_date)).fetchone()
    if row is None:
        raise AsuraBookError("YEAR_NOT_FOUND", f"{journal_date} を含む年度がありません。年度を作成してください",
                           {"journal_date": journal_date})
    if requested_year is not None and int(requested_year) != row["year"]:
        raise AsuraBookError("YEAR_MISMATCH", "指定年度と日付の年度が異なります。暗黙に別年度へ切り替えません",
                           {"requested_year": requested_year, "date_year": row["year"]})
    return row


def assert_postable(conn: sqlite3.Connection, fiscal_year: sqlite3.Row, journal_date: str) -> None:
    """年度凍結・月締めの計上gate (03章2節)。"""
    if fiscal_year["workflow_state"] == "frozen":
        raise AsuraBookError("PERIOD_CLOSED", f"{fiscal_year['year']}年度は凍結済みです。ownerが理由付きで再開してください",
                           {"year": fiscal_year["year"]})
    close = conn.execute(
        "SELECT * FROM period_closes WHERE fiscal_year_id=? AND period_start <= ? AND period_end >= ? "
        "AND workflow_state='soft_closed'",
        (fiscal_year["id"], journal_date, journal_date),
    ).fetchone()
    if close is not None:
        raise AsuraBookError("PERIOD_CLOSED", f"{close['period_start']}〜{close['period_end']} は月締め済みです",
                           {"period_start": close["period_start"], "period_end": close["period_end"]})


def get_entry(conn: sqlite3.Connection, entry_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM journal_entries WHERE id=?", (entry_id,)).fetchone()
    if row is None:
        rows = conn.execute("SELECT * FROM journal_entries WHERE id LIKE ?", (f"{entry_id}%",)).fetchall() \
            if isinstance(entry_id, str) and len(entry_id) >= 8 else []
        if len(rows) > 1:
            raise AsuraBookError("AMBIGUOUS_REFERENCE", "仕訳IDの前方一致が複数あります", {"ref": entry_id})
        row = rows[0] if rows else None
    if row is None:
        raise not_found("journal_entry", str(entry_id))
    return row
