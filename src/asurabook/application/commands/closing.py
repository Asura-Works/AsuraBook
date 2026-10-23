"""決算整理: 家事按分 (04章6節) と固定資産台帳・減価償却 (11章5節)。

整理仕訳は kind=adjustment として確定し、元の仕訳は変更しない。やり直しは整理仕訳の訂正 (取消) で行う。
"""

from __future__ import annotations

import datetime as dt
import json

from ...domain.assets import business_split, depreciation_for_year
from ...domain.errors import AsuraBookError, invalid, not_found
from ...domain.values import new_id, parse_amount_jpy, parse_date, parse_ratio
from ..core import CommandContext, command
from ..lookups import resolve_account, resolve_year
from .journal import _validate_and_post, create_draft, entry_view


def _active(conn, entry_id: str) -> bool:
    """整理仕訳が取り消されていなければ有効。"""
    return conn.execute("SELECT 1 FROM correction_groups WHERE original_entry_id=?", (entry_id,)).fetchone() is None


def _owner_draw(conn):
    row = conn.execute("SELECT * FROM accounts WHERE role='owner_draw'").fetchone()
    if row is None:
        raise AsuraBookError("ACCOUNT_MISSING", "事業主貸 (role=owner_draw) の科目がありません")
    return row


# ------------------------------------------------------------------ 家事按分

@command("apportionment.rule_create", min_profile="owner", mutating=True)
def apportionment_rule_create(ctx: CommandContext, payload: dict) -> dict:
    """年末一括の家事按分ルール (対象科目・期間・事業割合・根拠) を登録する。同じ科目・期間の重複は拒否。"""
    conn = ctx.db
    year = resolve_year(conn, payload.get("year"))
    acc = resolve_account(conn, payload.get("account"))
    if acc["type"] != "expense":
        raise invalid("家事按分の対象は費用科目です")
    ratio = parse_ratio(payload.get("business_ratio"))
    if ratio is None or ratio >= 1:
        raise invalid("business_ratio は 0 以上 1 未満の Decimal 文字列です (全額事業なら按分不要)")
    start = payload.get("period_start") or year["start_date"]
    end = payload.get("period_end") or year["end_date"]
    parse_date(start, "period_start")
    parse_date(end, "period_end")
    if not (year["start_date"] <= start <= end <= year["end_date"]):
        raise invalid("期間は年度内で指定してください")
    if not payload.get("basis"):
        raise invalid("按分の根拠 (basis) は必須です (例: 使用時間の記録、床面積の割合)")
    overlap = conn.execute(
        "SELECT id FROM apportionment_rules WHERE account_id=? AND status='active' AND NOT (period_end < ? OR period_start > ?)",
        (acc["id"], start, end)).fetchone()
    if overlap:
        raise AsuraBookError("APPORTIONMENT_OVERLAP", "同じ科目・重なる期間の按分ルールがあります", {"rule_id": overlap["id"]})
    rule_id = new_id()
    conn.execute(
        "INSERT INTO apportionment_rules (id, org_id, fiscal_year_id, account_id, period_start, period_end, business_ratio, "
        "method, rounding, basis, status, created_by, created_at) VALUES (?,?,?,?,?,?,?, 'year_end', 'floor_business', "
        "?, 'active', ?,?)", (rule_id, ctx.org_id, year["id"], acc["id"], start, end, str(ratio), payload["basis"],
                              ctx.actor, ctx.now))
    ctx.audit(target_type="apportionment_rule", target_id=rule_id, input_value=payload)
    return {"rule_id": rule_id}


def _apportionment_base(conn, rule) -> tuple[int, list[str], list[str]]:
    """対象科目の期間内の確定額。按分済み (行の business_ratio あり) と整理仕訳は除外する。"""
    rows = conn.execute(
        "SELECT e.id, e.kind, l.side, l.amount_jpy, l.business_ratio FROM journal_lines l "
        "JOIN journal_entries e ON e.id=l.entry_id WHERE e.status='posted' AND l.account_id=? "
        "AND e.journal_date BETWEEN ? AND ? AND e.kind NOT IN ('opening','opening_adjustment')",
        (rule["account_id"], rule["period_start"], rule["period_end"])).fetchall()
    # 按分の整理仕訳とその取消仕訳は対象額に含めない
    applied_entries = {r["entry_id"] for r in conn.execute("SELECT entry_id FROM apportionment_applications")}
    applied_entries |= {r["reversal_entry_id"] for r in conn.execute(
        "SELECT g.reversal_entry_id FROM correction_groups g JOIN apportionment_applications a "
        "ON a.entry_id=g.original_entry_id")}
    base, used, skipped = 0, set(), set()
    for r in rows:
        if r["id"] in applied_entries:
            continue
        if r["business_ratio"] is not None:
            skipped.add(r["id"])  # 計上時に按分済みの行へ年末按分を重ねない
            continue
        base += r["amount_jpy"] if r["side"] == "debit" else -r["amount_jpy"]
        used.add(r["id"])
    return base, sorted(used), sorted(skipped)


def _rule_view(conn, rule) -> dict:
    acc = conn.execute("SELECT code, name FROM accounts WHERE id=?", (rule["account_id"],)).fetchone()
    apps = [dict(a) for a in conn.execute("SELECT * FROM apportionment_applications WHERE rule_id=?", (rule["id"],))]
    active = [a for a in apps if _active(conn, a["entry_id"])]
    base, used, skipped = _apportionment_base(conn, rule)
    ratio = parse_ratio(rule["business_ratio"])
    business, private = business_split(base, ratio) if base > 0 else (0, 0)
    return dict(rule) | {"account_code": acc["code"], "account_name": acc["name"], "applied": bool(active),
                         "applications": apps, "preview": {"base_jpy": base, "business_jpy": business,
                                                           "private_jpy": private, "entries": len(used),
                                                           "skipped_already_apportioned": len(skipped)}}


@command("apportionment.list", min_profile="reader", mutating=False)
def apportionment_list(ctx: CommandContext, payload: dict) -> list:
    """家事按分ルールと適用状況・計算プレビュー。"""
    year = resolve_year(ctx.db, payload.get("year"))
    rules = ctx.db.execute("SELECT * FROM apportionment_rules WHERE fiscal_year_id=? AND status='active' "
                           "ORDER BY created_at", (year["id"],)).fetchall()
    return [_rule_view(ctx.db, r) for r in rules]


@command("apportionment.apply", min_profile="owner", mutating=True)
def apportionment_apply(ctx: CommandContext, payload: dict) -> dict:
    """按分の整理仕訳 (事業主貸 / 対象費用) を期間末日付で確定する。適用済みなら拒否。"""
    conn = ctx.db
    rule = conn.execute("SELECT * FROM apportionment_rules WHERE id=? AND status='active'",
                        (payload.get("rule_id"),)).fetchone()
    if rule is None:
        raise not_found("apportionment_rule", str(payload.get("rule_id")))
    view = _rule_view(conn, rule)
    if view["applied"]:
        raise AsuraBookError("ALREADY_APPLIED", "この按分は適用済みです。やり直す場合は整理仕訳を訂正してください")
    preview = view["preview"]
    if preview["private_jpy"] <= 0:
        raise AsuraBookError("NOTHING_TO_APPORTION", "按分する私用分がありません", preview)
    draw = _owner_draw(conn)
    entry = create_draft(ctx, {
        "journal_date": rule["period_end"],
        "description": f"家事按分 {view['account_name']} 事業割合 {rule['business_ratio']} ({rule['basis']})",
        "lines": [{"account": draw["id"], "side": "debit", "amount_jpy": preview["private_jpy"],
                   "tax_amount_status": "not_applicable", "description": "私用分"},
                  {"account": rule["account_id"], "side": "credit", "amount_jpy": preview["private_jpy"],
                   "tax_amount_status": "not_applicable", "description": "家事分の振替"}]}, kind="adjustment")
    posted = _validate_and_post(ctx, entry)
    base, used, skipped = _apportionment_base(conn, rule)
    conn.execute("INSERT INTO apportionment_applications (id, rule_id, entry_id, base_amount_jpy, private_amount_jpy, "
                 "input_manifest, created_at) VALUES (?,?,?,?,?,?,?)",
                 (new_id(), rule["id"], posted["id"], preview["base_jpy"], preview["private_jpy"],
                  json.dumps({"entry_ids": used, "skipped_already_apportioned": skipped, "ratio": rule["business_ratio"],
                              "rounding": "事業分を円未満切捨て"}), ctx.now))
    if skipped:
        ctx.warn("SKIPPED_APPORTIONED_LINES", f"計上時に按分済みの仕訳 {len(skipped)} 件は対象外にしました")
    return {"entry": posted, "preview": preview}


# ------------------------------------------------------------------ 固定資産

METHODS = ("straight_line", "lump_sum_3y", "immediate_small")


@command("asset.register", min_profile="owner", mutating=True)
def asset_register(ctx: CommandContext, payload: dict) -> dict:
    """固定資産を台帳に登録する。耐用年数・償却率・方法は利用者が公式資料から選び、根拠を残す。"""
    conn = ctx.db
    method = payload.get("method")
    if method not in METHODS:
        raise invalid("method は straight_line/lump_sum_3y/immediate_small です")
    cost = parse_amount_jpy(payload.get("acquisition_cost_jpy"), "acquisition_cost_jpy")
    acquired = payload.get("acquired_on")
    parse_date(acquired, "acquired_on")
    in_service = payload.get("in_service_on") or acquired
    parse_date(in_service, "in_service_on")
    if in_service < acquired:
        raise invalid("供用日が取得日より前です")
    rate = parse_ratio(payload.get("rate_decimal"), "rate_decimal")
    life = payload.get("useful_life_years")
    if method == "straight_line" and (rate is None or not isinstance(life, int) or life <= 0):
        raise invalid("定額法では useful_life_years (整数) と rate_decimal (公式の償却率) が必要です")
    ratio = parse_ratio(payload.get("business_ratio", "1"))
    if not payload.get("basis"):
        raise invalid("根拠 (basis) は必須です (例: 耐用年数表の該当区分、特例の適用確認)")
    asset_acc = resolve_account(conn, payload.get("asset_account") or ("151" if method == "lump_sum_3y" else "150"))
    expense_acc = resolve_account(conn, payload.get("expense_account") or "520")
    asset_id = new_id()
    conn.execute(
        "INSERT INTO fixed_assets (id, org_id, name, asset_account_id, expense_account_id, acquired_on, in_service_on, "
        "acquisition_cost_jpy, quantity, method, useful_life_years, rate_decimal, business_ratio, basis, source_entry_id, "
        "created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (asset_id, ctx.org_id, payload.get("name") or "名称未設定", asset_acc["id"], expense_acc["id"], acquired,
         in_service, cost, int(payload.get("quantity") or 1), method, life, None if rate is None else str(rate),
         str(ratio), payload["basis"], payload.get("source_entry_id"), ctx.actor, ctx.now))
    ctx.audit(target_type="fixed_asset", target_id=asset_id, input_value=payload)
    ctx.warn("TAX_RULE_NOT_VERIFIED", "耐用年数・償却率・特例の適用可否はアプリでは判定していません。根拠を確認してください")
    return {"asset_id": asset_id}


def _asset_schedule(conn, asset, year) -> dict:
    deps = [d for d in conn.execute("SELECT d.*, y.year FROM asset_depreciations d JOIN fiscal_years y "
                                    "ON y.id=d.fiscal_year_id WHERE d.asset_id=? ORDER BY y.year", (asset["id"],))
            if _active(conn, d["entry_id"])]
    before = [d for d in deps if d["year"] < year["year"]]
    this_year = [d for d in deps if d["year"] == year["year"]]
    calc = depreciation_for_year(
        method=asset["method"], cost=asset["acquisition_cost_jpy"],
        rate=None if asset["rate_decimal"] is None else parse_ratio(asset["rate_decimal"], "rate_decimal"),
        in_service=dt.date.fromisoformat(asset["in_service_on"] or asset["acquired_on"]),
        year_start=dt.date.fromisoformat(year["start_date"]), year_end=dt.date.fromisoformat(year["end_date"]),
        accumulated_before=sum(d["depreciation_jpy"] for d in before), years_already=len(before),
        disposed=dt.date.fromisoformat(asset["disposed_on"]) if asset["disposed_on"] else None)
    business, private = business_split(calc["depreciation_jpy"], parse_ratio(asset["business_ratio"]))
    return dict(asset) | {"year": year["year"], "calculation": calc | {"business_jpy": business, "private_jpy": private},
                          "posted_this_year": bool(this_year)}


@command("asset.list", min_profile="reader", mutating=False)
def asset_list(ctx: CommandContext, payload: dict) -> list:
    """固定資産台帳と、指定年度の償却計算 (候補)。"""
    year = resolve_year(ctx.db, payload.get("year"))
    assets = ctx.db.execute("SELECT * FROM fixed_assets WHERE acquired_on <= ? ORDER BY acquired_on",
                            (year["end_date"],)).fetchall()
    return [_asset_schedule(ctx.db, a, year) for a in assets]


@command("asset.depreciate", min_profile="owner", mutating=True)
def asset_depreciate(ctx: CommandContext, payload: dict) -> dict:
    """年度の減価償却の整理仕訳を確定する (減価償却費 + 事業主貸 / 資産)。計上済みの資産は除外する。"""
    conn = ctx.db
    year = resolve_year(conn, payload.get("year"))
    wanted = set(payload.get("asset_ids") or [])
    draw = _owner_draw(conn)
    done, skipped = [], []
    for asset in conn.execute("SELECT * FROM fixed_assets WHERE acquired_on <= ? ORDER BY acquired_on",
                              (year["end_date"],)).fetchall():
        if wanted and asset["id"] not in wanted:
            continue
        sched = _asset_schedule(conn, asset, year)
        calc = sched["calculation"]
        if sched["posted_this_year"]:
            skipped.append({"asset_id": asset["id"], "reason": "計上済み"})
            continue
        if calc["depreciation_jpy"] <= 0:
            skipped.append({"asset_id": asset["id"], "reason": "償却額 0"})
            continue
        lines = []
        if calc["business_jpy"]:
            lines.append({"account": asset["expense_account_id"], "side": "debit", "amount_jpy": calc["business_jpy"],
                          "tax_amount_status": "not_applicable"})
        if calc["private_jpy"]:
            lines.append({"account": draw["id"], "side": "debit", "amount_jpy": calc["private_jpy"],
                          "tax_amount_status": "not_applicable", "description": "私用分"})
        lines.append({"account": asset["asset_account_id"], "side": "credit", "amount_jpy": calc["depreciation_jpy"],
                      "tax_amount_status": "not_applicable"})
        entry = create_draft(ctx, {"journal_date": year["end_date"],
                                   "description": f"減価償却 {asset['name']} ({calc['months']}か月)",
                                   "lines": lines}, kind="adjustment")
        posted = _validate_and_post(ctx, entry)
        conn.execute(
            "INSERT INTO asset_depreciations (id, asset_id, fiscal_year_id, entry_id, months, depreciation_jpy, "
            "business_jpy, book_value_end_jpy, calculation_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (new_id(), asset["id"], year["id"], posted["id"], calc["months"], calc["depreciation_jpy"],
             calc["business_jpy"], calc["book_value_end_jpy"], json.dumps(calc, ensure_ascii=False), ctx.now))
        done.append({"asset_id": asset["id"], "entry_id": posted["id"], **calc})
    return {"year": year["year"], "posted": done, "skipped": skipped}
