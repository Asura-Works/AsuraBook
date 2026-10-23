"""消込 command (04章7節、05章7節)。初期の許容差は0円。差額を資料なしに手数料へ逃がさない。"""

from __future__ import annotations

from ...domain.errors import AsuraBookError, invalid
from ...domain.values import parse_date
from ..core import CommandContext, command
from ..ledger import (ORIGIN_SIDE, active_allocations, allocate, cancel_allocation, open_item_summary,
                      settlement_allocated)
from ..lookups import resolve_account


@command("openitems.list", min_profile="reader", mutating=False)
def openitems_list(ctx: CommandContext, payload: dict) -> dict:
    """as_of 時点の未回収/未払。年度をまたいでも同じ open item ID を使う。"""
    conn = ctx.db
    as_of = payload.get("as_of")
    if as_of is not None:
        parse_date(as_of, "as_of")
    cond, params = ["1=1"], []
    if payload.get("type"):
        cond.append("type=?")
        params.append(payload["type"])
    if payload.get("account"):
        cond.append("account_id=?")
        params.append(resolve_account(conn, payload["account"])["id"])
    if as_of:
        cond.append("created_business_date <= ?")
        params.append(as_of)
    items = conn.execute(f"SELECT * FROM open_items WHERE {' AND '.join(cond)} ORDER BY created_business_date, id",
                         params).fetchall()
    out = [open_item_summary(conn, it, as_of) for it in items]
    if not payload.get("include_settled"):
        out = [o for o in out if o["balance_jpy"] != 0]
    return {"as_of": as_of, "items": out, "total_jpy": sum(o["balance_jpy"] for o in out)}


@command("reconcile.suggest", min_profile="reader", mutating=False)
def reconcile_suggest(ctx: CommandContext, payload: dict) -> dict:
    """未割当の決済行と、同じ科目の未決済 open item の候補を返す。金額一致は順位の材料に留める。"""
    conn = ctx.db
    candidates = []
    lines = conn.execute(
        "SELECT l.*, e.journal_date, e.number, e.description AS entry_description, a.open_item_type "
        "FROM journal_lines l JOIN journal_entries e ON e.id=l.entry_id JOIN accounts a ON a.id=l.account_id "
        "WHERE e.status='posted' AND e.kind NOT IN ('reversal','opening','opening_adjustment') "
        "AND a.open_item_type IS NOT NULL ORDER BY e.journal_date, e.number").fetchall()
    for line in lines:
        if line["side"] == ORIGIN_SIDE[line["open_item_type"]]:
            continue
        if conn.execute("SELECT 1 FROM correction_groups WHERE original_entry_id=?", (line["entry_id"],)).fetchone():
            continue
        left = line["amount_jpy"] - settlement_allocated(conn, line["id"])
        if left <= 0:
            continue
        items = conn.execute(
            "SELECT * FROM open_items WHERE account_id=? AND COALESCE(subaccount_id,'')=COALESCE(?, '') "
            "AND created_business_date <= ?", (line["account_id"], line["subaccount_id"], line["journal_date"])).fetchall()
        options = []
        for it in items:
            s = open_item_summary(conn, it, None)
            if s["balance_jpy"] <= 0:
                continue
            s["exact_match"] = s["balance_jpy"] == left
            options.append(s)
        options.sort(key=lambda o: (not o["exact_match"], o["created_business_date"]))
        candidates.append({
            "settlement_line_id": line["id"], "journal_date": line["journal_date"], "entry_number": line["number"],
            "description": line["entry_description"], "unallocated_jpy": left, "open_items": options,
        })
    return {"candidates": candidates}


@command("reconcile.apply", min_profile="bookkeeper_scoped", mutating=True, capability_scope="reconcile.apply")
def reconcile_apply(ctx: CommandContext, payload: dict) -> dict:
    """消込を適用する。全割当を同じtransactionで検証し、一部だけ成功させない。"""
    allocations = payload.get("allocations")
    if not isinstance(allocations, list) or not allocations:
        raise invalid("allocations は1件以上必要です")
    if ctx.expected_revision is not None and ctx.expected_revision != ctx.ledger_revision():
        raise AsuraBookError("REVISION_CONFLICT", "帳簿が更新されています。候補を再取得してください",
                           {"expected_revision": ctx.expected_revision, "actual_revision": ctx.ledger_revision()})
    seq = ctx.audit(target_type="reconciliation", input_value=payload)
    created = []
    for a in allocations:
        if ctx.capability is not None:
            ctx.capability_target(a.get("settlement_line_id"))
        created.append(allocate(ctx, open_item_id=a.get("open_item_id"), settlement_line_id=a.get("settlement_line_id"),
                                amount_jpy=a.get("amount_jpy"), effective_date=a.get("effective_date"),
                                audit_seq=seq))
    ctx.bump_ledger_revision()
    return {"allocation_ids": created}


@command("reconcile.cancel", min_profile="bookkeeper_scoped", mutating=True, capability_scope="reconcile.cancel")
def reconcile_cancel(ctx: CommandContext, payload: dict) -> dict:
    """割当を取り消す。取消は効力日付きの新eventで、元の割当は残る。"""
    if not payload.get("reason"):
        raise invalid("reason は必須です")
    allocation_id = payload.get("allocation_id")
    if ctx.capability is not None:
        ctx.capability_target(allocation_id)
    effective = payload.get("effective_date")
    if not effective:
        raise invalid("effective_date は必須です (取消の効力日)")
    seq = ctx.audit(target_type="allocation", target_id=allocation_id, reason=payload["reason"])
    cancel_id = cancel_allocation(ctx, allocation_id, effective, seq)
    ctx.bump_ledger_revision()
    return {"cancel_id": cancel_id, "allocation_id": allocation_id}


@command("reconcile.list", min_profile="reader", mutating=False)
def reconcile_list(ctx: CommandContext, payload: dict) -> list:
    """open item に対する割当/取消の履歴。"""
    item_id = payload.get("open_item_id")
    rows = ctx.db.execute("SELECT * FROM reconciliation_allocations WHERE open_item_id=? "
                          "ORDER BY recorded_audit_seq", (item_id,)).fetchall()
    active = {r["id"] for r in active_allocations(ctx.db, open_item_id=item_id)}
    return [dict(r) | {"active": r["id"] in active} for r in rows]
