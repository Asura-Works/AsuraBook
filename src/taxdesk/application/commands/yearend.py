"""決算・繰越・税務snapshot (11章)。

snapshot は仕訳ID集合・audit cutoff・movement/allocation ID集合を固定する。
繰越は opening_projection 方式。前期訂正は翌期を stale にし、承認後に差額調整を追加する。
"""

from __future__ import annotations

import json

from ...domain.carryforward import CarryforwardError, diff_balances, project_opening
from ...domain.errors import TaxDeskError, invalid
from ...domain.values import content_hash, new_id
from ..core import CommandContext, command
from ..lookups import resolve_year
from ..reports import account_balances, profit_loss, trial_balance
from .journal import _validate_and_post, create_draft, entry_view


def build_snapshot(ctx: CommandContext, year) -> dict:
    conn = ctx.db
    cutoff = conn.execute("SELECT COALESCE(MAX(sequence_no),0) FROM audit_events").fetchone()[0]
    entry_ids = [r[0] for r in conn.execute(
        "SELECT id FROM journal_entries WHERE fiscal_year_id=? AND status='posted' ORDER BY id", (year["id"],))]
    movement_ids = [r[0] for r in conn.execute(
        "SELECT m.id FROM open_item_movements m WHERE m.recorded_audit_seq <= ? AND m.effective_business_date <= ? "
        "ORDER BY m.id", (cutoff, year["end_date"]))]
    allocation_ids = [r[0] for r in conn.execute(
        "SELECT id FROM reconciliation_allocations WHERE recorded_audit_seq <= ? AND effective_business_date <= ? "
        "ORDER BY id", (cutoff, year["end_date"]))]
    balances = account_balances(conn, year["id"])
    manifest = {
        "audit_cutoff_seq": cutoff,
        "journal_entry_ids": entry_ids,
        "open_item_movement_ids": movement_ids,
        "allocation_ids": allocation_ids,
        "algorithm_version": "taxdesk.snapshot/1",
    }
    result = {"trial_balance": trial_balance(balances), "profit_loss": profit_loss(balances)}
    try:
        result["carryforward_projection"] = project_opening(balances).to_dict()
    except CarryforwardError as exc:
        result["carryforward_projection_error"] = str(exc)
    return {"manifest": manifest, "result": result, "balances": balances}


def _insert_snapshot(ctx: CommandContext, year, snap: dict) -> str:
    snap_id = new_id()
    rules: list = []  # 年版ruleは未登録 (11章4節)。登録後にここへ版を固定する
    body = {"manifest": snap["manifest"], "result": snap["result"], "rules": rules}
    ctx.db.execute(
        "INSERT INTO tax_snapshots (id, fiscal_year_id, ledger_revision, audit_cutoff_seq, rule_versions_json, "
        "input_manifest_json, result_json, content_hash, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (snap_id, year["id"], ctx.ledger_revision(), snap["manifest"]["audit_cutoff_seq"], json.dumps(rules),
         json.dumps(snap["manifest"], ensure_ascii=False), json.dumps(snap["result"], ensure_ascii=False),
         content_hash(body), ctx.actor, ctx.now))
    return snap_id


@command("tax.snapshot_create", min_profile="assistant", mutating=True)
def tax_snapshot_create(ctx: CommandContext, payload: dict) -> dict:
    """年度の帳簿集計を不変snapshotとして保存する (年版ruleは未適用の集計snapshot)。"""
    year = resolve_year(ctx.db, payload.get("year"))
    snap = build_snapshot(ctx, year)
    snap_id = _insert_snapshot(ctx, year, snap)
    ctx.audit(target_type="tax_snapshot", target_id=snap_id, result_value=snap["result"])
    ctx.warn("RULES_NOT_APPLIED", "年版の税務ruleは未登録のため、税額計算は含みません")
    return {"snapshot_id": snap_id, "manifest": snap["manifest"], "result": snap["result"]}


def _lines_from_balances(balances: dict[str, int]) -> list[dict]:
    lines = []
    for account_id, amount in sorted(balances.items()):
        lines.append({"account": account_id, "side": "debit" if amount > 0 else "credit", "amount_jpy": abs(amount),
                      "tax_amount_status": "not_applicable"})
    return lines


def _latest_cumulative(conn, source_id: str, target_id: str) -> tuple[dict | None, dict[str, int]]:
    rows = conn.execute("SELECT * FROM carryforward_revisions WHERE source_year_id=? AND target_year_id=? "
                        "ORDER BY revision", (source_id, target_id)).fetchall()
    if not rows:
        return None, {}
    latest = dict(rows[-1])
    return latest, json.loads(latest["projection_json"])["balances"]


@command("year.carryforward_status", min_profile="reader", mutating=False)
def carryforward_status(ctx: CommandContext, payload: dict) -> dict:
    """翌期繰越が前期の現在の帳簿と一致しているか (opening_stale) を返す。"""
    source = resolve_year(ctx.db, payload.get("from_year"))
    target_year = payload.get("to_year") or source["year"] + 1
    target = ctx.db.execute("SELECT * FROM fiscal_years WHERE year=?", (target_year,)).fetchone()
    projection = project_opening(account_balances(ctx.db, source["id"]))
    if target is None:
        return {"from_year": source["year"], "to_year": target_year, "state": "not_started",
                "projection": projection.to_dict()}
    latest, applied = _latest_cumulative(ctx.db, source["id"], target["id"])
    diff = diff_balances(applied, projection.balances)
    return {"from_year": source["year"], "to_year": target_year,
            "state": "not_started" if latest is None else ("opening_stale" if diff else "current"),
            "latest_revision": None if latest is None else latest["revision"],
            "pending_diff": diff, "projection": projection.to_dict()}


@command("year.carryforward", min_profile="owner", mutating=True)
def year_carryforward(ctx: CommandContext, payload: dict) -> dict:
    """前期の帳簿から翌期首の opening を作る。2回目以降は差額調整だけを追加する。"""
    conn = ctx.db
    source = resolve_year(conn, payload.get("from_year"))
    target_year = payload.get("to_year") or source["year"] + 1
    if target_year != source["year"] + 1:
        raise invalid("繰越先は翌年度だけです")
    target = conn.execute("SELECT * FROM fiscal_years WHERE year=?", (target_year,)).fetchone()
    if target is None:
        from .setup import _insert_year
        _insert_year(ctx, ctx.org_id, target_year)
        target = conn.execute("SELECT * FROM fiscal_years WHERE year=?", (target_year,)).fetchone()
    if conn.execute("SELECT 1 FROM journal_entries WHERE fiscal_year_id=? AND status<>'posted' "
                    "AND journal_date <= ?", (source["id"], source["end_date"])).fetchone():
        ctx.warn("UNPOSTED_IN_SOURCE", "前期に未確定の仕訳があります。繰越には含めていません")
    snap = build_snapshot(ctx, source)
    try:
        projection = project_opening(snap["balances"])
    except CarryforwardError as exc:
        raise TaxDeskError("CARRYFORWARD_FAILED", str(exc)) from None
    latest, applied = _latest_cumulative(conn, source["id"], target["id"])
    diff = diff_balances(applied, projection.balances)
    if latest is not None and not diff:
        return {"state": "current", "revision": latest["revision"], "projection": projection.to_dict()}
    snap_id = _insert_snapshot(ctx, source, snap)
    kind = "opening" if latest is None else "opening_adjustment"
    lines = _lines_from_balances(projection.balances if latest is None else diff)
    if len(lines) < 2:
        raise TaxDeskError("CARRYFORWARD_EMPTY", "繰越する残高がありません")
    desc = f"{source['year']}年度からの期首繰越" if latest is None else \
        f"{source['year']}年度訂正による期首差額調整 (rev {latest['revision'] + 1})"
    draft = create_draft(ctx, {"journal_date": target["start_date"], "description": desc, "lines": lines}, kind=kind)
    posted = _validate_and_post(ctx, draft)
    rev_id = new_id()
    conn.execute(
        "INSERT INTO carryforward_revisions (id, source_year_id, target_year_id, source_snapshot_id, entry_id, kind, "
        "revision, previous_id, projection_json, diff_json, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (rev_id, source["id"], target["id"], snap_id, posted["id"], kind,
         1 if latest is None else latest["revision"] + 1, None if latest is None else latest["id"],
         json.dumps(projection.to_dict()), None if latest is None else json.dumps(diff), ctx.actor, ctx.now))
    ctx.audit(target_type="carryforward", target_id=rev_id, detail={"kind": kind, "diff": diff})
    return {"state": "applied", "kind": kind, "revision": 1 if latest is None else latest["revision"] + 1,
            "entry": posted, "projection": projection.to_dict(), "diff": diff if latest else None}
