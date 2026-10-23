"""帳票の集計 (04章3節、14章)。posted 仕訳だけを、同じ read snapshot から集計する。"""

from __future__ import annotations

import sqlite3

from ..domain.carryforward import AccountBalance


def account_balances(conn: sqlite3.Connection, fiscal_year_id: str, *, date_from: str | None = None,
                     date_to: str | None = None, entry_ids: list[str] | None = None) -> list[AccountBalance]:
    cond = ["e.status='posted'", "e.fiscal_year_id=:year"]
    params: dict = {"year": fiscal_year_id}
    if date_from:
        cond.append("e.journal_date >= :from")
        params["from"] = date_from
    if date_to:
        cond.append("e.journal_date <= :to")
        params["to"] = date_to
    if entry_ids is not None:
        cond.append("e.id IN (SELECT value FROM json_each(:ids))")
        import json
        params["ids"] = json.dumps(entry_ids)
    # 仕訳側を先に科目別集計し、科目マスターと結合する (大量データで LEFT JOIN 全体を集計しない)
    totals = {r["account_id"]: (int(r["debit_total"]), int(r["credit_total"])) for r in conn.execute(
        f"""SELECT l.account_id,
                   COALESCE(SUM(CASE WHEN l.side='debit' THEN l.amount_jpy END),0) AS debit_total,
                   COALESCE(SUM(CASE WHEN l.side='credit' THEN l.amount_jpy END),0) AS credit_total
            FROM journal_entries e JOIN journal_lines l ON l.entry_id = e.id
            WHERE {' AND '.join(cond)} GROUP BY l.account_id""", params)}
    return [AccountBalance(r["id"], r["code"], r["name"], r["type"], r["role"], *totals.get(r["id"], (0, 0)))
            for r in conn.execute("SELECT id, code, name, type, role FROM accounts ORDER BY code")]


def trial_balance(balances: list[AccountBalance]) -> dict:
    rows = []
    for b in balances:
        if b.debit_total == 0 and b.credit_total == 0:
            continue
        rows.append({
            "account_id": b.account_id, "code": b.code, "name": b.name, "type": b.type,
            "debit_total_jpy": b.debit_total, "credit_total_jpy": b.credit_total,
            "debit_balance_jpy": max(b.debit_balance, 0), "credit_balance_jpy": max(-b.debit_balance, 0),
        })
    debit = sum(r["debit_total_jpy"] for r in rows)
    credit = sum(r["credit_total_jpy"] for r in rows)
    return {"rows": rows, "debit_total_jpy": debit, "credit_total_jpy": credit, "balanced": debit == credit}


def profit_loss(balances: list[AccountBalance]) -> dict:
    revenue = [{"code": b.code, "name": b.name, "amount_jpy": -b.debit_balance}
               for b in balances if b.type == "revenue" and b.debit_balance != 0]
    expense = [{"code": b.code, "name": b.name, "amount_jpy": b.debit_balance}
               for b in balances if b.type == "expense" and b.debit_balance != 0]
    total_revenue = sum(r["amount_jpy"] for r in revenue)
    total_expense = sum(r["amount_jpy"] for r in expense)
    return {"revenue": revenue, "expense": expense, "total_revenue_jpy": total_revenue,
            "total_expense_jpy": total_expense, "book_profit_jpy": total_revenue - total_expense,
            "note": "帳簿利益は青色申告特別控除前の値です"}


def balance_sheet(balances: list[AccountBalance]) -> dict:
    assets = [{"code": b.code, "name": b.name, "amount_jpy": b.debit_balance}
              for b in balances if b.type == "asset" and b.debit_balance != 0]
    liabilities = [{"code": b.code, "name": b.name, "amount_jpy": -b.debit_balance}
                   for b in balances if b.type == "liability" and b.debit_balance != 0]
    equity = [{"code": b.code, "name": b.name, "role": b.role, "amount_jpy": -b.debit_balance}
              for b in balances if b.type == "equity" and b.debit_balance != 0]
    profit = profit_loss(balances)["book_profit_jpy"]
    total_assets = sum(a["amount_jpy"] for a in assets)
    total_liab = sum(a["amount_jpy"] for a in liabilities)
    total_equity = sum(a["amount_jpy"] for a in equity)
    return {
        "assets": assets, "liabilities": liabilities, "equity": equity,
        "total_assets_jpy": total_assets, "total_liabilities_jpy": total_liab,
        "total_equity_jpy": total_equity, "book_profit_jpy": profit,
        "balanced": total_assets == total_liab + total_equity + profit,
        "note": "事業主貸は資本の控除 (負の資本) として equity に含めます",
    }


def general_ledger(conn: sqlite3.Connection, fiscal_year_id: str, account_id: str, *, date_from: str | None = None,
                   date_to: str | None = None, subaccount_id: str | None = None) -> dict:
    cond = ["e.status='posted'", "e.fiscal_year_id=?", "l.account_id=?"]
    params: list = [fiscal_year_id, account_id]
    if subaccount_id:
        cond.append("l.subaccount_id=?")
        params.append(subaccount_id)
    if date_from:
        cond.append("e.journal_date >= ?")
        params.append(date_from)
    if date_to:
        cond.append("e.journal_date <= ?")
        params.append(date_to)
    acc = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    sign = 1 if acc["normal_side"] == "debit" else -1
    rows = conn.execute(
        f"""SELECT e.id AS entry_id, e.journal_date, e.number, e.kind, e.description, l.line_no, l.side,
                   l.amount_jpy, l.description AS line_description,
                   (SELECT group_concat(a2.name, '/') FROM journal_lines l2 JOIN accounts a2 ON a2.id=l2.account_id
                     WHERE l2.entry_id=e.id AND l2.side <> l.side) AS counter_accounts
            FROM journal_lines l JOIN journal_entries e ON e.id=l.entry_id
            WHERE {' AND '.join(cond)} ORDER BY e.journal_date, e.number, l.line_no""", params).fetchall()
    running = 0
    out = []
    for r in rows:
        delta = r["amount_jpy"] if r["side"] == "debit" else -r["amount_jpy"]
        running += delta * sign
        out.append(dict(r) | {"balance_jpy": running})
    return {"account": {"code": acc["code"], "name": acc["name"], "normal_side": acc["normal_side"]},
            "rows": out, "closing_balance_jpy": running}
