"""open item と日付付き消込 (03章5節、04章7・9節)。

as_of残高 = 対象日以前に効く元本増減 − 有効割当 (取消は取消効力日から効く)。
凍結snapshotでは audit_cutoff_seq も適用する。現在の完済フラグから過去残を求めない。
"""

from __future__ import annotations

import sqlite3

from ..domain.errors import TaxDeskError, invalid, not_found
from ..domain.values import new_id, parse_amount_jpy, parse_date
from .core import CommandContext

# open item の発生側: 債権は借方、債務は貸方
ORIGIN_SIDE = {"receivable": "debit", "payable": "credit"}


def create_open_items_for_entry(ctx: CommandContext, entry: sqlite3.Row, audit_seq: int) -> list[str]:
    """posted 仕訳の債権債務科目の発生行から open item を作る。opening/取消では作らない。"""
    if entry["kind"] in ("opening", "opening_adjustment", "reversal"):
        return []
    created = []
    lines = ctx.db.execute(
        "SELECT l.*, a.open_item_type FROM journal_lines l JOIN accounts a ON a.id = l.account_id "
        "WHERE l.entry_id=? AND a.open_item_type IS NOT NULL ORDER BY l.line_no",
        (entry["id"],),
    ).fetchall()
    for line in lines:
        if line["side"] != ORIGIN_SIDE[line["open_item_type"]]:
            continue
        item_id = new_id()
        ctx.db.execute(
            "INSERT INTO open_items (id, org_id, origin_line_id, type, account_id, subaccount_id, counterparty_id, "
            "currency, created_business_date) VALUES (?,?,?,?,?,?,?,?,?)",
            (item_id, entry["org_id"], line["id"], line["open_item_type"], line["account_id"],
             line["subaccount_id"], line["counterparty_id"] or entry["counterparty_id"], "JPY",
             entry["journal_date"]),
        )
        ctx.db.execute(
            "INSERT INTO open_item_movements (id, open_item_id, kind, signed_amount_jpy, effective_business_date, "
            "source_journal_line_id, recorded_audit_seq) VALUES (?,?,?,?,?,?,?)",
            (new_id(), item_id, "origin", line["amount_jpy"], entry["journal_date"], line["id"], audit_seq),
        )
        created.append(item_id)
    return created


def open_item_balance(conn: sqlite3.Connection, item_id: str, as_of: str | None = None,
                      cutoff_seq: int | None = None) -> int:
    date_cond = "" if as_of is None else " AND effective_business_date <= :as_of"
    seq_cond = "" if cutoff_seq is None else " AND recorded_audit_seq <= :cutoff"
    params = {"item": item_id, "as_of": as_of, "cutoff": cutoff_seq}
    principal = conn.execute(
        f"SELECT COALESCE(SUM(signed_amount_jpy),0) FROM open_item_movements WHERE open_item_id=:item"
        f"{date_cond}{seq_cond}", params).fetchone()[0]
    allocated = conn.execute(
        f"SELECT COALESCE(SUM(CASE effect WHEN 'allocate' THEN amount_jpy ELSE -amount_jpy END),0) "
        f"FROM reconciliation_allocations WHERE open_item_id=:item{date_cond}{seq_cond}", params).fetchone()[0]
    return int(principal) - int(allocated)


def settlement_allocated(conn: sqlite3.Connection, line_id: str, as_of: str | None = None) -> int:
    date_cond = "" if as_of is None else " AND effective_business_date <= ?"
    params: tuple = (line_id,) if as_of is None else (line_id, as_of)
    return int(conn.execute(
        "SELECT COALESCE(SUM(CASE effect WHEN 'allocate' THEN amount_jpy ELSE -amount_jpy END),0) "
        f"FROM reconciliation_allocations WHERE settlement_line_id=?{date_cond}", params).fetchone()[0])


def active_allocations(conn: sqlite3.Connection, *, open_item_id: str | None = None,
                       settlement_line_id: str | None = None) -> list[sqlite3.Row]:
    cond, params = [], []
    if open_item_id:
        cond.append("a.open_item_id=?")
        params.append(open_item_id)
    if settlement_line_id:
        cond.append("a.settlement_line_id=?")
        params.append(settlement_line_id)
    where = " AND ".join(cond) if cond else "1=1"
    return conn.execute(
        f"SELECT a.* FROM reconciliation_allocations a WHERE {where} AND a.effect='allocate' "
        "AND NOT EXISTS (SELECT 1 FROM reconciliation_allocations c WHERE c.reverses_allocation_id = a.id) "
        "ORDER BY a.recorded_audit_seq", params).fetchall()


def allocate(ctx: CommandContext, *, open_item_id: str, settlement_line_id: str, amount_jpy: int,
             effective_date: str | None, audit_seq: int) -> str:
    """決済行を open item へ割り当てる。過剰充当・科目違い・未確定行を拒否する。"""
    conn = ctx.db
    amount = parse_amount_jpy(amount_jpy, "amount_jpy")
    item = conn.execute("SELECT * FROM open_items WHERE id=?", (open_item_id,)).fetchone()
    if item is None:
        raise not_found("open_item", open_item_id)
    line = conn.execute(
        "SELECT l.*, e.status, e.journal_date, e.kind AS entry_kind FROM journal_lines l "
        "JOIN journal_entries e ON e.id = l.entry_id WHERE l.id=?", (settlement_line_id,)).fetchone()
    if line is None:
        raise not_found("journal_line", settlement_line_id)
    if line["status"] != "posted":
        raise TaxDeskError("SETTLEMENT_NOT_POSTED", "確定済み仕訳の行だけを消込に使えます")
    if line["account_id"] != item["account_id"]:
        raise TaxDeskError("ACCOUNT_MISMATCH", "open item と決済行の科目が異なります")
    if (item["subaccount_id"] or None) != (line["subaccount_id"] or None):
        raise TaxDeskError("ACCOUNT_MISMATCH", "open item と決済行の補助科目が異なります")
    if line["side"] == ORIGIN_SIDE[item["type"]]:
        raise TaxDeskError("SIDE_MISMATCH", "決済行は open item と逆の貸借である必要があります")
    if line["entry_kind"] == "reversal":
        raise TaxDeskError("SIDE_MISMATCH", "取消仕訳の行は消込に使えません")
    eff = effective_date or line["journal_date"]
    parse_date(eff, "effective_date")
    if eff < item["created_business_date"]:
        raise invalid("割当の効力日が債権債務の発生日より前です", effective_date=eff)

    remaining_item = open_item_balance(conn, open_item_id)
    remaining_item_at = open_item_balance(conn, open_item_id, as_of=eff)
    remaining_line = line["amount_jpy"] - settlement_allocated(conn, settlement_line_id)
    if amount > remaining_item or amount > remaining_item_at:
        raise TaxDeskError("ALLOCATION_OVERFLOW", "open item の残高を超える割当です",
                           {"open_item_id": open_item_id, "remaining_jpy": min(remaining_item, remaining_item_at),
                            "requested_jpy": amount})
    if amount > remaining_line:
        raise TaxDeskError("ALLOCATION_OVERFLOW", "決済行の未割当額を超える割当です",
                           {"settlement_line_id": settlement_line_id, "remaining_jpy": remaining_line,
                            "requested_jpy": amount})
    alloc_id = new_id()
    conn.execute(
        "INSERT INTO reconciliation_allocations (id, open_item_id, settlement_line_id, amount_jpy, effect, "
        "reverses_allocation_id, effective_business_date, recorded_audit_seq, actor, created_at) "
        "VALUES (?,?,?,?, 'allocate', NULL, ?,?,?,?)",
        (alloc_id, open_item_id, settlement_line_id, amount, eff, audit_seq, ctx.actor, ctx.now),
    )
    return alloc_id


def cancel_allocation(ctx: CommandContext, allocation_id: str, effective_date: str, audit_seq: int) -> str:
    """割当取消は元割当を参照する新event。取消効力日前の残高を変えない。"""
    conn = ctx.db
    parse_date(effective_date, "effective_date")
    alloc = conn.execute("SELECT * FROM reconciliation_allocations WHERE id=?", (allocation_id,)).fetchone()
    if alloc is None:
        raise not_found("allocation", allocation_id)
    if alloc["effect"] != "allocate":
        raise invalid("取消イベント自体は取り消せません")
    if conn.execute("SELECT 1 FROM reconciliation_allocations WHERE reverses_allocation_id=?",
                    (allocation_id,)).fetchone():
        raise TaxDeskError("ALREADY_CANCELLED", "この割当は取消済みです", {"allocation_id": allocation_id})
    if effective_date < alloc["effective_business_date"]:
        raise invalid("取消の効力日が元の割当より前です", effective_date=effective_date)
    cancel_id = new_id()
    conn.execute(
        "INSERT INTO reconciliation_allocations (id, open_item_id, settlement_line_id, amount_jpy, effect, "
        "reverses_allocation_id, effective_business_date, recorded_audit_seq, actor, created_at) "
        "VALUES (?,?,?,?, 'cancel', ?,?,?,?,?)",
        (cancel_id, alloc["open_item_id"], alloc["settlement_line_id"], alloc["amount_jpy"], allocation_id,
         effective_date, audit_seq, ctx.actor, ctx.now),
    )
    return cancel_id


def open_item_summary(conn: sqlite3.Connection, item: sqlite3.Row, as_of: str | None,
                      cutoff_seq: int | None = None) -> dict:
    origin = conn.execute(
        "SELECT e.journal_date, e.number, e.description, l.amount_jpy FROM journal_lines l "
        "JOIN journal_entries e ON e.id=l.entry_id WHERE l.id=?", (item["origin_line_id"],)).fetchone()
    account = conn.execute("SELECT code, name FROM accounts WHERE id=?", (item["account_id"],)).fetchone()
    return {
        "open_item_id": item["id"],
        "type": item["type"],
        "account_code": account["code"],
        "account_name": account["name"],
        "subaccount_id": item["subaccount_id"],
        "counterparty_id": item["counterparty_id"],
        "created_business_date": item["created_business_date"],
        "origin_amount_jpy": origin["amount_jpy"],
        "origin_entry_number": origin["number"],
        "description": origin["description"],
        "balance_jpy": open_item_balance(conn, item["id"], as_of, cutoff_seq),
    }
