"""帳票・ダッシュボード command (14章)。"""

from __future__ import annotations

import json
from pathlib import Path

from ...domain.errors import invalid
from ...domain.values import parse_date
from ..core import CommandContext, command
from ..exports import resolve_export_path, rows_to_csv, write_atomic
from ..ledger import open_item_balance
from ..lookups import resolve_account, resolve_subaccount, resolve_year
from ..reports import account_balances, balance_sheet, general_ledger, profit_loss, trial_balance


def _meta(ctx: CommandContext, year, payload: dict) -> dict:
    unposted = ctx.db.execute("SELECT COUNT(*) FROM journal_entries WHERE fiscal_year_id=? AND status<>'posted'",
                              (year["id"],)).fetchone()[0]
    return {
        "year": year["year"], "workflow_state": year["workflow_state"],
        "date_from": payload.get("date_from"), "date_to": payload.get("date_to"),
        "ledger_revision": ctx.ledger_revision(), "generated_at": ctx.now,
        "includes_unposted": False, "unposted_entries_excluded": unposted,
    }


def _balances(ctx: CommandContext, payload: dict):
    year = resolve_year(ctx.db, payload.get("year"))
    for key in ("date_from", "date_to"):
        if payload.get(key):
            parse_date(payload[key], key)
    return year, account_balances(ctx.db, year["id"], date_from=payload.get("date_from"),
                                  date_to=payload.get("date_to"))


@command("report.trial_balance", min_profile="reader", mutating=False)
def report_trial_balance(ctx: CommandContext, payload: dict) -> dict:
    """残高試算表 (posted のみ)。"""
    year, balances = _balances(ctx, payload)
    return {"meta": _meta(ctx, year, payload), **trial_balance(balances)}


@command("report.profit_loss", min_profile="reader", mutating=False)
def report_profit_loss(ctx: CommandContext, payload: dict) -> dict:
    """損益計算書 (帳簿利益は青色控除前)。"""
    year, balances = _balances(ctx, payload)
    return {"meta": _meta(ctx, year, payload), **profit_loss(balances)}


@command("report.balance_sheet", min_profile="reader", mutating=False)
def report_balance_sheet(ctx: CommandContext, payload: dict) -> dict:
    """貸借対照表。"""
    year, balances = _balances(ctx, payload)
    return {"meta": _meta(ctx, year, payload), **balance_sheet(balances)}


@command("report.general_ledger", min_profile="reader", mutating=False)
def report_general_ledger(ctx: CommandContext, payload: dict) -> dict:
    """総勘定元帳/補助元帳。"""
    year = resolve_year(ctx.db, payload.get("year"))
    acc = resolve_account(ctx.db, payload.get("account"))
    sub = resolve_subaccount(ctx.db, acc["id"], payload.get("subaccount"))
    data = general_ledger(ctx.db, year["id"], acc["id"], date_from=payload.get("date_from"),
                          date_to=payload.get("date_to"), subaccount_id=None if sub is None else sub["id"])
    return {"meta": _meta(ctx, year, payload), **data}


@command("dashboard.summary", min_profile="reader", mutating=False)
def dashboard_summary(ctx: CommandContext, payload: dict) -> dict:
    """ホーム画面: 売上・利益・現預金・未回収・未払・未処理を別指標で返す。"""
    conn = ctx.db
    year, balances = _balances(ctx, payload)
    pl = profit_loss(balances)
    cash = sum(b.debit_balance for b in balances if b.code in ("100", "110", "111"))
    as_of = payload.get("as_of") or year["end_date"]
    receivable = payable = 0
    for item in conn.execute("SELECT id, type FROM open_items WHERE created_business_date <= ?", (as_of,)):
        bal = open_item_balance(conn, item["id"], as_of)
        if item["type"] == "receivable":
            receivable += bal
        else:
            payable += bal
    counts = {
        "draft_entries": conn.execute("SELECT COUNT(*) FROM journal_entries WHERE status='draft' AND fiscal_year_id=?",
                                      (year["id"],)).fetchone()[0],
        "validated_entries": conn.execute("SELECT COUNT(*) FROM journal_entries WHERE status='validated' "
                                          "AND fiscal_year_id=?", (year["id"],)).fetchone()[0],
        "evidence_unsynced": conn.execute("SELECT COUNT(*) FROM evidence_objects WHERE role='original' "
                                          "AND sync_state <> 'verified'").fetchone()[0],
        "ocr_needs_review": conn.execute("SELECT COUNT(*) FROM ocr_jobs WHERE state IN ('extracted','needs_review')"
                                         ).fetchone()[0],
        "import_issues": conn.execute("SELECT COUNT(*) FROM import_issues").fetchone()[0],
        "unlinked_source_events": conn.execute(
            "SELECT COUNT(*) FROM source_transactions t WHERE NOT EXISTS (SELECT 1 FROM source_links s "
            "JOIN journal_lines l ON l.id=s.journal_line_id JOIN journal_entries e ON e.id=l.entry_id "
            "WHERE s.event_id=t.id AND e.status='posted')").fetchone()[0],
        "open_findings": conn.execute("SELECT COUNT(*) FROM findings WHERE status='open'").fetchone()[0],
    }
    return {
        "meta": _meta(ctx, year, payload) | {"as_of": as_of},
        "revenue_jpy": pl["total_revenue_jpy"],
        "expense_jpy": pl["total_expense_jpy"],
        "book_profit_jpy": pl["book_profit_jpy"],
        "cash_and_deposits_jpy": cash,
        "receivable_open_jpy": receivable,
        "payable_open_jpy": payable,
        "counts": counts,
        "notes": ["現預金は帳簿残高です。銀行の外部残高とは別に照合してください",
                  "帳簿利益から所得税を定率で求めることはしません"],
    }


@command("report.export", min_profile="reader", mutating=False)
def report_export(ctx: CommandContext, payload: dict) -> dict:
    """帳票をCSVで export root 配下へ出力する (no-clobber)。"""
    kind = payload.get("report")
    path = payload.get("path")
    if not path:
        raise invalid("path は必須です")
    year, balances = _balances(ctx, payload)
    meta = _meta(ctx, year, payload)
    if kind == "trial_balance":
        tb = trial_balance(balances)
        header = ["科目コード", "科目名", "借方合計", "貸方合計", "借方残高", "貸方残高"]
        rows = [[r["code"], r["name"], r["debit_total_jpy"], r["credit_total_jpy"], r["debit_balance_jpy"],
                 r["credit_balance_jpy"]] for r in tb["rows"]]
    elif kind == "profit_loss":
        pl = profit_loss(balances)
        header = ["区分", "科目コード", "科目名", "金額"]
        rows = [["収益", r["code"], r["name"], r["amount_jpy"]] for r in pl["revenue"]] + \
               [["費用", r["code"], r["name"], r["amount_jpy"]] for r in pl["expense"]] + \
               [["帳簿利益", "", "", pl["book_profit_jpy"]]]
    elif kind == "journal":
        header = ["日付", "伝票番号", "行", "借貸", "科目コード", "科目名", "金額", "摘要", "種別"]
        rows = [list(r) for r in ctx.db.execute(
            "SELECT e.journal_date, e.number, l.line_no, l.side, a.code, a.name, l.amount_jpy, "
            "COALESCE(NULLIF(l.description,''), e.description), e.kind FROM journal_entries e "
            "JOIN journal_lines l ON l.entry_id=e.id JOIN accounts a ON a.id=l.account_id "
            "WHERE e.status='posted' AND e.fiscal_year_id=? ORDER BY e.journal_date, e.number, l.line_no",
            (year["id"],)).fetchall()]
    else:
        raise invalid("report は trial_balance/profit_loss/journal です")
    footer = [["# meta", json.dumps(meta, ensure_ascii=False)]]
    export_root = Path(payload.get("export_root_override") or ctx.paths.exports)
    if payload.get("export_root_override") and ctx.session.entry_point != "gui":
        raise invalid("export root の変更は GUI の保存先選択でだけ許可されます")
    target = resolve_export_path(ctx.paths, export_root, path)
    data = rows_to_csv(header, rows + footer, bom=payload.get("bom", True))
    if ctx.dry_run:
        return {"would_write": str(target), "rows": len(rows), "meta": meta}
    write_atomic(target, data)
    return {"written": str(target), "rows": len(rows), "meta": meta}
