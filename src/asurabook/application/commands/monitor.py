"""申告モニタリングの決定的検査 (21章9節、14章5節)。

帳簿データだけから、取得範囲・記帳・残高・証憑・決算・申告情報の不足を根拠付きの finding にする。
決定的検査の結果は再点検でだけ解消され、既読 (acknowledged) にしても解消扱いにしない。
帳簿 revision が変わると前回の点検結果は stale になる。
"""

from __future__ import annotations

import datetime as dt
import json

from ...domain.errors import AsuraBookError, invalid
from ...domain.values import new_id, today_tokyo
from .. import config as app_config
from ..core import CommandContext, command
from ..ledger import ORIGIN_SIDE, open_item_balance, settlement_allocated
from ..lookups import resolve_year
from ..reports import account_balances, trial_balance
from .tax import latest_profile, unknown_fields

STEPS = ("資料取得", "記帳", "残高照合", "証憑", "決算整理", "他所得・控除", "年度rule", "申告書", "e-Tax署名・送信", "受付", "納付")
UNSUPPORTED_STEPS = {
    "年度rule": "対象年の税務ruleは未登録です (公式資料の確認後に版として登録)",
    "申告書": "申告書・決算書の作成は未対応です",
    "e-Tax署名・送信": "e-Tax連携は未対応です (公式ソフトでの手続)",
    "受付": "受付記録の管理は未対応です",
    "納付": "納付記録の管理は未対応です",
}


def _months(start: dt.date, end: dt.date) -> list[str]:
    out, cur = [], start.replace(day=1)
    while cur <= end:
        out.append(cur.strftime("%Y-%m"))
        cur = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return out


def _finding(step: str, code: str, key: str, severity: str, message: str, evidence: dict, action: str) -> dict:
    return {"step": step, "code": code, "key": key, "severity": severity, "message": message,
            "evidence": evidence, "next_action": action}


def run_checks(ctx: CommandContext, year) -> tuple[list[dict], dict]:
    conn = ctx.db
    start = dt.date.fromisoformat(year["start_date"])
    end = min(dt.date.fromisoformat(year["end_date"]), today_tokyo())
    found: list[dict] = []
    facts: dict = {}

    # 資料取得: 取込元ごとの月別 coverage
    accounts = conn.execute("SELECT * FROM source_accounts WHERE active=1 AND kind IN ('bank','card','platform') "
                            "ORDER BY code").fetchall()
    facts["source_accounts"] = len(accounts)
    for sa in accounts:
        covered: set[str] = set()
        for b in conn.execute("SELECT coverage_json FROM import_batches WHERE source_account_id=?", (sa["id"],)):
            cov = json.loads(b["coverage_json"])
            if cov.get("period_start") and cov.get("period_end"):
                covered.update(_months(dt.date.fromisoformat(cov["period_start"]),
                                       dt.date.fromisoformat(cov["period_end"])))
        missing = [m for m in _months(start, end) if m not in covered] if start <= end else []
        if missing:
            found.append(_finding(
                "資料取得", "COVERAGE_GAP", f"coverage:{sa['code']}", "warning",
                f"{sa['display_name']} ({sa['code']}) の取込がない月: {', '.join(missing)}",
                {"source_account": sa["code"], "months": missing},
                "該当月の明細を取込んでください。取引がなかった月も明細0件の資料で確認します"))

    # 記帳
    unposted = conn.execute("SELECT status, COUNT(*) AS n FROM journal_entries WHERE fiscal_year_id=? "
                            "AND status<>'posted' GROUP BY status", (year["id"],)).fetchall()
    facts["posted_entries"] = conn.execute("SELECT COUNT(*) FROM journal_entries WHERE fiscal_year_id=? "
                                           "AND status='posted'", (year["id"],)).fetchone()[0]
    if unposted:
        counts = {r["status"]: r["n"] for r in unposted}
        found.append(_finding("記帳", "UNPOSTED_ENTRIES", "unposted", "warning",
                              f"未確定の仕訳があります: {counts}", counts, "仕訳画面で検証・確定または破棄してください"))
    unlinked = conn.execute(
        "SELECT t.id, t.transaction_date, t.amount_jpy, t.description, u.capacity_jpy, "
        "(SELECT COALESCE(SUM(s.signed_effect_jpy),0) FROM source_links s JOIN journal_lines jl ON jl.id=s.journal_line_id "
        " JOIN journal_entries e ON e.id=jl.entry_id WHERE s.usage_slot_id=u.id AND e.status='posted') AS used "
        "FROM source_transactions t JOIN source_usage_slots u ON u.event_id=t.id "
        "WHERE COALESCE(t.transaction_date, t.recognition_date, t.settlement_date) BETWEEN ? AND ?",
        (year["start_date"], year["end_date"])).fetchall()
    pending = [dict(r) for r in unlinked if r["used"] < r["capacity_jpy"]]
    if pending:
        found.append(_finding("記帳", "UNLINKED_SOURCE_EVENTS", "unlinked_source_events", "warning",
                              f"記帳されていない取込明細が {len(pending)} 件あります",
                              {"count": len(pending), "examples": pending[:10]},
                              "取込画面の明細から仕訳案を作るか、私用等の理由を確認してください"))
    review = conn.execute("SELECT COUNT(*) FROM import_batches WHERE status='needs_review'").fetchone()[0]
    if review:
        found.append(_finding("資料取得", "IMPORT_NEEDS_REVIEW", "import_needs_review", "warning",
                              f"要確認の取込が {review} 件あります", {"batches": review},
                              "取込の issue を確認し、保留行を修正して再取込してください"))

    # 残高照合
    tb = trial_balance(account_balances(conn, year["id"]))
    if not tb["balanced"]:
        found.append(_finding("残高照合", "TRIAL_BALANCE_UNBALANCED", "trial_balance", "error",
                              "試算表の貸借が一致しません (DB整合の異常)", {"debit": tb["debit_total_jpy"],
                                                                           "credit": tb["credit_total_jpy"]},
                              "backup から復元できるか確認し、開発者へ連絡してください"))
    unallocated = []
    for line in conn.execute(
            "SELECT l.id, l.amount_jpy, l.side, e.journal_date, e.number, e.description, a.open_item_type, a.name "
            "FROM journal_lines l JOIN journal_entries e ON e.id=l.entry_id JOIN accounts a ON a.id=l.account_id "
            "WHERE e.status='posted' AND e.fiscal_year_id=? AND a.open_item_type IS NOT NULL "
            "AND e.kind NOT IN ('reversal','opening','opening_adjustment') "
            "AND NOT EXISTS (SELECT 1 FROM correction_groups g WHERE g.original_entry_id=e.id)", (year["id"],)):
        if line["side"] == ORIGIN_SIDE[line["open_item_type"]]:
            continue
        left = line["amount_jpy"] - settlement_allocated(conn, line["id"])
        if left > 0:
            unallocated.append({"journal_date": line["journal_date"], "number": line["number"], "account": line["name"],
                                "description": line["description"], "unallocated_jpy": left})
    if unallocated:
        found.append(_finding("残高照合", "UNALLOCATED_SETTLEMENTS", "unallocated_settlements", "warning",
                              f"消込されていない決済が {len(unallocated)} 件 (計 {sum(u['unallocated_jpy'] for u in unallocated):,} 円) あります",
                              {"items": unallocated[:20]}, "消込画面で対応する債権・債務へ割り当ててください"))
    cash = conn.execute("SELECT id, name FROM accounts WHERE code='100'").fetchone()
    if cash:
        running, first_negative = 0, None
        for r in conn.execute(
                "SELECT e.journal_date, SUM(CASE WHEN l.side='debit' THEN l.amount_jpy ELSE -l.amount_jpy END) AS d "
                "FROM journal_lines l JOIN journal_entries e ON e.id=l.entry_id WHERE e.status='posted' "
                "AND e.fiscal_year_id=? AND l.account_id=? GROUP BY e.journal_date ORDER BY e.journal_date",
                (year["id"], cash["id"])):
            running += r["d"]
            if running < 0 and first_negative is None:
                first_negative = {"date": r["journal_date"], "balance_jpy": running}
        if first_negative:
            found.append(_finding("残高照合", "NEGATIVE_CASH", "negative_cash", "warning",
                                  f"{cash['name']}の帳簿残高が {first_negative['date']} にマイナス ({first_negative['balance_jpy']:,} 円) になっています",
                                  first_negative, "記帳漏れ・事業主借の計上漏れ・日付誤りを確認してください"))
    open_items = []
    for item in conn.execute("SELECT * FROM open_items WHERE created_business_date <= ?", (year["end_date"],)):
        bal = open_item_balance(conn, item["id"], year["end_date"])
        if bal > 0:
            open_items.append({"open_item_id": item["id"], "type": item["type"], "created": item["created_business_date"],
                               "balance_jpy": bal})
    if open_items:
        found.append(_finding("決算整理", "OPEN_ITEMS_AT_YEAR_END", "open_items_year_end", "info",
                              f"年度末時点で未決済の債権債務が {len(open_items)} 件あります",
                              {"items": open_items[:20]}, "回収・支払の予定と、貸倒れ等の要否を確認してください"))

    # 証憑
    expense_rows = conn.execute(
        "SELECT e.id, e.journal_date, e.number, e.description, SUM(l.amount_jpy) AS amount FROM journal_entries e "
        "JOIN journal_lines l ON l.entry_id=e.id JOIN accounts a ON a.id=l.account_id "
        "WHERE e.status='posted' AND e.fiscal_year_id=? AND a.type='expense' AND l.side='debit' "
        "AND e.kind IN ('normal','replacement') "
        "AND NOT EXISTS (SELECT 1 FROM evidence_links x WHERE x.journal_entry_id=e.id) "
        "AND NOT EXISTS (SELECT 1 FROM correction_groups g WHERE g.original_entry_id=e.id) "
        "GROUP BY e.id ORDER BY e.journal_date", (year["id"],)).fetchall()
    if expense_rows:
        total = sum(r["amount"] for r in expense_rows)
        found.append(_finding("証憑", "EXPENSE_WITHOUT_EVIDENCE", "expense_without_evidence", "warning",
                              f"証憑が関連付いていない経費の仕訳が {len(expense_rows)} 件 (計 {total:,} 円) あります",
                              {"count": len(expense_rows), "examples": [dict(r) for r in expense_rows[:20]]},
                              "証憑画面でレシート・請求書を関連付けてください (仕訳の追加は不要)"))
    cloud = bool(app_config.load(ctx.paths).get("cloud", {}).get("provider"))
    unsynced = conn.execute("SELECT sync_state, COUNT(*) AS n FROM evidence_objects WHERE role='original' "
                            "AND sync_state<>'verified' GROUP BY sync_state").fetchall()
    if unsynced:
        counts = {r["sync_state"]: r["n"] for r in unsynced}
        if not cloud:
            found.append(_finding("証憑", "CLOUD_NOT_CONFIGURED", "cloud_not_configured", "warning",
                                  f"証憑原本 {sum(counts.values())} 件がローカルだけに保存されています (保管先未設定)",
                                  counts, "保管先を設定するまで、backup を定期的に作成してください"))
        else:
            found.append(_finding("証憑", "EVIDENCE_UNSYNCED", "evidence_unsynced", "warning",
                                  f"保管先で確認できていない原本があります: {counts}", counts,
                                  "AI・実行履歴の「待機中の処理を実行」で upload を再試行してください"))
    ocr = conn.execute("SELECT COUNT(*) FROM ocr_jobs WHERE state IN ('extracted','needs_review')").fetchone()[0]
    if ocr:
        found.append(_finding("証憑", "OCR_REVIEW_PENDING", "ocr_review", "info",
                              f"読取り結果の確認待ちが {ocr} 件あります", {"count": ocr},
                              "証憑画面で読取り値を確認して保存してください"))

    # 決算整理
    next_year = conn.execute("SELECT * FROM fiscal_years WHERE year=?", (year["year"] + 1,)).fetchone()
    if next_year is not None:
        from .yearend import _latest_cumulative
        from ...domain.carryforward import CarryforwardError, diff_balances, project_opening
        latest, applied = _latest_cumulative(conn, year["id"], next_year["id"])
        if latest is not None:
            try:
                diff = diff_balances(applied, project_opening(account_balances(conn, year["id"])).balances)
            except CarryforwardError:
                diff = {"error": "projection"}
            if diff:
                found.append(_finding("決算整理", "CARRYFORWARD_STALE", "carryforward_stale", "error",
                                      f"{year['year'] + 1}年度の期首残高が {year['year']}年度の現在の帳簿と一致しません",
                                      {"diff": diff}, "決算・繰越画面で差額調整を実行してください"))
    prev_year = conn.execute("SELECT * FROM fiscal_years WHERE year=?", (year["year"] - 1,)).fetchone()
    if prev_year is not None:
        prev_posted = conn.execute("SELECT COUNT(*) FROM journal_entries WHERE fiscal_year_id=? AND status='posted'",
                                   (prev_year["id"],)).fetchone()[0]
        has_opening = conn.execute("SELECT 1 FROM carryforward_revisions WHERE source_year_id=? AND target_year_id=?",
                                   (prev_year["id"], year["id"])).fetchone()
        if prev_posted and not has_opening:
            found.append(_finding("決算整理", "OPENING_MISSING", "opening_missing", "warning",
                                  f"{prev_year['year']}年度からの期首繰越がまだありません", {},
                                  f"{prev_year['year']}年度の決算後に繰越を実行してください"))

    # 他所得・控除 / 申告情報
    profile_row = latest_profile(conn, year["id"])
    if profile_row is None:
        found.append(_finding("他所得・控除", "TAX_PROFILE_MISSING", "tax_profile", "error",
                              "税務設定 (青色・消費税・インボイス・他所得) が未登録です", {},
                              "税務設定画面で年度の状況を登録してください"))
    else:
        missing = unknown_fields(json.loads(profile_row["payload_json"]))
        if missing or profile_row["status"] != "confirmed":
            found.append(_finding("他所得・控除", "TAX_PROFILE_UNCONFIRMED", "tax_profile", "error",
                                  "税務設定に未確認の項目があります: " + "、".join(m["label"] for m in missing),
                                  {"unknown_fields": missing, "version": profile_row["version"]},
                                  "税務設定画面で確認してください"))

    # 実行状態
    unknown_jobs = conn.execute("SELECT COUNT(*) FROM agent_jobs WHERE status='outcome_unknown'").fetchone()[0]
    if unknown_jobs:
        found.append(_finding("証憑", "AI_OUTCOME_UNKNOWN", "ai_outcome_unknown", "warning",
                              f"結果が不明な AI job が {unknown_jobs} 件あります (利用量が発生した可能性があります)",
                              {"count": unknown_jobs}, "必要な証憑だけ読取りを再実行してください"))
    rc = ctx.core.runtime()
    if rc.recovery_hold:
        found.append(_finding("資料取得", "RECOVERY_HOLD", "recovery_hold", "warning",
                              f"復元後の確認待ちで外部処理を保留しています: {', '.join(rc.held_scopes)}",
                              {"held_scopes": rc.held_scopes}, "保全・復元画面で状態を確認し、確認できた範囲を解除してください"))
    return found, facts


def readiness(ctx: CommandContext, year, found: list[dict], facts: dict) -> list[dict]:
    by_step: dict[str, list[dict]] = {}
    for f in found:
        by_step.setdefault(f["step"], []).append(f)
    out = []
    for step in STEPS:
        items = [f for f in by_step.get(step, []) if f["severity"] in ("warning", "error")]
        if step in UNSUPPORTED_STEPS:
            out.append({"step": step, "state": "未対応", "basis": UNSUPPORTED_STEPS[step]})
        elif items:
            out.append({"step": step, "state": "要確認", "basis": "; ".join(f["message"] for f in items)[:300]})
        elif step == "資料取得" and not facts.get("source_accounts"):
            out.append({"step": step, "state": "未着手", "basis": "取込元アカウントが未登録です"})
        elif step in ("記帳", "残高照合", "証憑") and not facts.get("posted_entries"):
            out.append({"step": step, "state": "未着手", "basis": "確定仕訳がありません"})
        elif step == "決算整理" and year["workflow_state"] == "open":
            out.append({"step": step, "state": "未着手", "basis": "年度は open です (決算整理を開始していません)"})
        elif step == "他所得・控除":
            out.append({"step": step, "state": "未対応", "basis": "所得控除・他所得の入力画面は未対応です (有無の確認だけ記録)"})
        else:
            note = " (銀行の外部残高との照合は未対応)" if step == "残高照合" else ""
            out.append({"step": step, "state": "完了", "basis": "決定的検査で問題は検出されませんでした" + note})
    return out


def _persist(ctx: CommandContext, year, found: list[dict]) -> dict:
    conn = ctx.db
    revision = ctx.ledger_revision()
    seen = set()
    created = updated = 0
    for f in found:
        seen.add(f["key"])
        row = conn.execute("SELECT id FROM findings WHERE fiscal_year_id=? AND origin='deterministic' AND finding_key=? "
                           "AND status<>'resolved'", (year["id"], f["key"])).fetchone()
        evidence = json.dumps({"evidence": f["evidence"], "next_action": f["next_action"], "step": f["step"]},
                              ensure_ascii=False, default=str)
        if row is None:
            conn.execute(
                "INSERT INTO findings (id, org_id, fiscal_year_id, origin, topic, code, severity, status, snapshot_revision, "
                "message, evidence_json, created_at, updated_at, finding_key) VALUES (?,?,?, 'deterministic', ?,?,?, "
                "'open', ?,?,?,?,?,?)",
                (new_id(), ctx.org_id, year["id"], f["step"], f["code"], f["severity"], revision, f["message"], evidence,
                 ctx.now, ctx.now, f["key"]))
            created += 1
        else:
            conn.execute("UPDATE findings SET code=?, severity=?, snapshot_revision=?, message=?, evidence_json=?, "
                         "updated_at=? WHERE id=?",
                         (f["code"], f["severity"], revision, f["message"], evidence, ctx.now, row["id"]))
            updated += 1
    resolved = 0
    for row in conn.execute("SELECT id, finding_key FROM findings WHERE fiscal_year_id=? AND origin='deterministic' "
                            "AND status<>'resolved'", (year["id"],)).fetchall():
        if row["finding_key"] not in seen:
            conn.execute("UPDATE findings SET status='resolved', resolved_reason=?, snapshot_revision=?, updated_at=? "
                         "WHERE id=?", ("再点検で検出されなくなりました", revision, ctx.now, row["id"]))
            resolved += 1
    return {"created": created, "updated": updated, "resolved": resolved}


def _findings_view(ctx: CommandContext, year, status: str | None = None) -> list[dict]:
    revision = ctx.ledger_revision()
    cond, params = ["fiscal_year_id=?"], [year["id"]]
    if status:
        cond.append("status=?")
        params.append(status)
    else:
        cond.append("status<>'resolved'")
    rows = ctx.db.execute(f"SELECT * FROM findings WHERE {' AND '.join(cond)} ORDER BY "
                          "CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, topic, created_at",
                          params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        extra = json.loads(d.pop("evidence_json"))
        d.update(extra)
        d["stale"] = r["snapshot_revision"] != revision
        out.append(d)
    return out


@command("monitor.check", min_profile="assistant", mutating=True)
def monitor_check(ctx: CommandContext, payload: dict) -> dict:
    """決定的検査を実行し、finding と申告準備の進捗を更新する。"""
    year = resolve_year(ctx.db, payload.get("year"))
    found, facts = run_checks(ctx, year)
    stats = _persist(ctx, year, found)
    ctx.audit(target_type="findings", target_id=str(year["year"]), result_value=stats)
    steps = readiness(ctx, year, found, facts)
    return {"year": year["year"], "ledger_revision": ctx.ledger_revision(), "stats": stats,
            "readiness": steps, "ready_for_filing": all(s["state"] == "完了" for s in steps),
            "findings": _findings_view(ctx, year)}


@command("monitor.findings", min_profile="reader", mutating=False)
def monitor_findings(ctx: CommandContext, payload: dict) -> dict:
    """保存済みの finding。帳簿 revision が変わったものは stale として示す。"""
    year = resolve_year(ctx.db, payload.get("year"))
    status = payload.get("status")
    if status not in (None, "open", "acknowledged", "resolved"):
        raise invalid("status は open/acknowledged/resolved です")
    return {"year": year["year"], "ledger_revision": ctx.ledger_revision(), "findings": _findings_view(ctx, year, status)}


@command("monitor.ack", min_profile="assistant", mutating=True)
def monitor_ack(ctx: CommandContext, payload: dict) -> dict:
    """finding を既読にする。既読は解消ではなく、再点検で検出される間は残る。"""
    cur = ctx.db.execute("UPDATE findings SET status='acknowledged', updated_at=? WHERE id=? AND status='open'",
                         (ctx.now, payload.get("id")))
    if cur.rowcount != 1:
        raise AsuraBookError("NOT_FOUND", "既読にできる finding がありません", {"id": payload.get("id")})
    ctx.audit(target_type="finding", target_id=payload.get("id"))
    return {"id": payload.get("id"), "status": "acknowledged"}
