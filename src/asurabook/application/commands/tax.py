"""年度ごとの税務profile (11章1節)。

青色承認・消費税・インボイス・経理方式・他所得を年度ごとの版で保存する。
未確認は unknown のまま保存でき、利用者条件を未確認のまま有利な方式へ自動選択しない。
現在の設定を過去年度へ遡及上書きしない (版は不変)。
"""

from __future__ import annotations

import json
import re

from ...domain.errors import AsuraBookError, invalid
from ...domain.values import new_id, parse_date
from ..core import CommandContext, command
from ..lookups import resolve_year

# field path -> 許可値。None は日付/文字列の自由入力
FIELDS: dict[str, tuple[str, ...] | None] = {
    "filing_type": ("blue", "white", "unknown"),
    "blue_approval": ("approved", "applied", "not_applied", "not_applicable", "unknown"),
    "bookkeeping": ("double_entry", "simple", "unknown"),
    "income_category": ("business", "miscellaneous", "unknown"),
    "consumption_tax.status": ("taxable", "exempt", "unknown"),
    "consumption_tax.method": ("general", "simplified", "two_tenths", "not_applicable", "unknown"),
    "invoice.registered": ("yes", "no", "unknown"),
    "invoice.registration_number": None,
    "invoice.registered_from": None,
    "invoice.registered_until": None,
    "accounting_method": ("tax_inclusive", "tax_exclusive", "unknown"),
    "other_income.salary": ("yes", "no", "unknown"),
    "other_income.pension": ("yes", "no", "unknown"),
    "other_income.other": ("yes", "no", "unknown"),
    "business_start_date": None,
    "notes": None,
}
LABELS = {
    "filing_type": "申告区分 (青色/白色)",
    "blue_approval": "青色申告の承認状況",
    "bookkeeping": "記帳方式",
    "income_category": "所得区分",
    "consumption_tax.status": "消費税の課税/免税",
    "consumption_tax.method": "消費税の計算方式",
    "invoice.registered": "インボイス登録",
    "accounting_method": "税込/税抜経理",
    "other_income.salary": "給与所得の有無",
    "other_income.pension": "年金等の有無",
    "other_income.other": "その他の所得の有無",
}
REQUIRED = tuple(LABELS)
_T_NUMBER = re.compile(r"^T\d{13}$")


def _get(profile: dict, path: str):
    cur = profile
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _set(profile: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = profile
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def default_profile() -> dict:
    out: dict = {}
    for path, allowed in FIELDS.items():
        _set(out, path, "unknown" if allowed else None)
    return out


def normalize(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise invalid("profile は object です")
    out = default_profile()
    for path, allowed in FIELDS.items():
        value = _get(raw, path)
        if value is None:
            continue
        if allowed is not None and value not in allowed:
            raise invalid(f"{path} の値が不正です", field=path, allowed=list(allowed), value=value)
        if path in ("invoice.registered_from", "invoice.registered_until", "business_start_date"):
            parse_date(value, path)
        if path == "invoice.registration_number" and not _T_NUMBER.match(str(value)):
            raise invalid("登録番号は T + 13桁 です (形式だけを検査し、公表情報との照合は別に行います)")
        _set(out, path, value)
    # 矛盾の検査 (どちらかを unknown にすれば保存できる)
    if out["filing_type"] == "white" and out["blue_approval"] == "approved":
        raise invalid("白色申告と青色承認済みが同時に指定されています")
    if out["consumption_tax"]["status"] == "exempt" and out["consumption_tax"]["method"] not in ("not_applicable",
                                                                                                  "unknown"):
        raise invalid("免税事業者に消費税の計算方式は指定できません")
    if out["invoice"]["registered"] == "yes" and out["consumption_tax"]["status"] == "exempt":
        raise invalid("インボイス登録事業者は免税事業者になりません。どちらかを確認してください")
    return out


def unknown_fields(profile: dict) -> list[dict]:
    missing = []
    for path in REQUIRED:
        if _get(profile, path) == "unknown":
            if path == "blue_approval" and profile["filing_type"] == "white":
                continue
            if path == "consumption_tax.method" and profile["consumption_tax"]["status"] == "exempt":
                continue
            missing.append({"field": path, "label": LABELS[path]})
    if profile["invoice"]["registered"] == "yes" and not profile["invoice"]["registration_number"]:
        missing.append({"field": "invoice.registration_number", "label": "インボイス登録番号"})
    return missing


def latest_profile(conn, fiscal_year_id: str):
    return conn.execute("SELECT * FROM tax_profiles WHERE fiscal_year_id=? ORDER BY version DESC LIMIT 1",
                        (fiscal_year_id,)).fetchone()


def _view(row) -> dict:
    profile = json.loads(row["payload_json"])
    return {"version": row["version"], "status": row["status"], "profile": profile,
            "unknown_fields": unknown_fields(profile), "created_by": row["created_by"], "created_at": row["created_at"]}


@command("tax.profile_show", min_profile="reader", mutating=False)
def tax_profile_show(ctx: CommandContext, payload: dict) -> dict:
    """年度の税務profile (最新版と履歴)。未設定なら全項目 unknown の雛形を返す。"""
    year = resolve_year(ctx.db, payload.get("year"))
    rows = ctx.db.execute("SELECT * FROM tax_profiles WHERE fiscal_year_id=? ORDER BY version",
                          (year["id"],)).fetchall()
    if not rows:
        template = default_profile()
        return {"year": year["year"], "current": None, "template": template,
                "unknown_fields": unknown_fields(template), "history": [], "labels": LABELS,
                "allowed": {k: list(v) if v else None for k, v in FIELDS.items()}}
    return {"year": year["year"], "current": _view(rows[-1]), "history": [_view(r) for r in rows[:-1]],
            "unknown_fields": _view(rows[-1])["unknown_fields"], "labels": LABELS,
            "allowed": {k: list(v) if v else None for k, v in FIELDS.items()}}


@command("tax.profile_set", min_profile="owner", mutating=True)
def tax_profile_set(ctx: CommandContext, payload: dict) -> dict:
    """税務profileの新しい版を保存する。confirm=true でも unknown が残れば未確認のまま。"""
    conn = ctx.db
    year = resolve_year(conn, payload.get("year"))
    if payload.get("copy_from_year") is not None:
        source = resolve_year(conn, payload["copy_from_year"])
        prev = latest_profile(conn, source["id"])
        if prev is None:
            raise AsuraBookError("NOT_FOUND", f"{source['year']}年度の税務profileがありません")
        base = json.loads(prev["payload_json"])
        ctx.warn("COPIED_PROFILE", f"{source['year']}年度の設定を複写しました。年度ごとに状況を確認してください")
    else:
        base = {}
    merged = json.loads(json.dumps(base))
    for path in FIELDS:
        value = _get(payload.get("profile") or {}, path)
        if value is not None:
            _set(merged, path, value)
    profile = normalize(merged)
    current = latest_profile(conn, year["id"])
    if ctx.expected_revision is not None and ctx.expected_revision != (current["version"] if current else 0):
        raise AsuraBookError("REVISION_CONFLICT", "税務profileが更新されています",
                             {"expected_revision": ctx.expected_revision,
                              "actual_revision": current["version"] if current else 0})
    version = (current["version"] if current else 0) + 1
    missing = unknown_fields(profile)
    status = "confirmed" if payload.get("confirm") and not missing else "unconfirmed"
    if payload.get("confirm") and missing:
        ctx.warn("PROFILE_HAS_UNKNOWN", "未確認の項目があるため「確認済み」にしていません")
    conn.execute("INSERT INTO tax_profiles (id, fiscal_year_id, version, payload_json, status, created_by, created_at) "
                 "VALUES (?,?,?,?,?,?,?)", (new_id(), year["id"], version, json.dumps(profile, ensure_ascii=False),
                                            status, ctx.actor, ctx.now))
    ctx.audit(target_type="tax_profile", target_id=f"{year['year']}:v{version}", input_value=profile,
              detail={"status": status})
    return {"year": year["year"], "version": version, "status": status, "profile": profile,
            "unknown_fields": missing}
