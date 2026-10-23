"""初期設定・マスター・締め・capability 発行。"""

from __future__ import annotations

import datetime as dt
import json
import secrets

from ...domain.chart import DEFAULT_ACCOUNTS, DEFAULT_SUBACCOUNTS
from ...domain.errors import AsuraBookError, invalid
from ...domain.values import content_hash, new_id, parse_date, utc_now
from ...infrastructure.db.connection import write_transaction
from ...infrastructure.paths import looks_like_sync_folder
from ..core import CommandContext, command
from ..lookups import resolve_account, resolve_year

YEAR_STATES = ("open", "preclose", "adjusted", "reviewed", "frozen")


def _insert_year(ctx: CommandContext, org_id: str, year: int) -> str:
    if not (1990 <= year <= 2200):
        raise invalid("year が範囲外です", year=year)
    year_id = new_id()
    ctx.db.execute(
        "INSERT INTO fiscal_years (id, org_id, year, start_date, end_date, workflow_state) VALUES (?,?,?,?,?, 'open')",
        (year_id, org_id, year, f"{year}-01-01", f"{year}-12-31"))
    return year_id


@command("org.init", min_profile="assistant", mutating=False, needs_db=False)
def org_init(ctx: CommandContext, payload: dict) -> dict:
    """DBを新規作成し、事業者・年度・初期科目を登録する。既存DBには使えない。"""
    core = ctx.core
    if core.db_exists():
        raise AsuraBookError("ALREADY_INITIALIZED", "既にDBがあります", {"path": str(core.paths.db)})
    name = payload.get("display_name")
    if not name or not isinstance(name, str):
        raise invalid("display_name は必須です")
    years = payload.get("years") or [dt.date.today().year]
    if not all(isinstance(y, int) for y in years):
        raise invalid("years は整数配列です")
    plan = {
        "db_path": str(core.paths.db),
        "display_name": name,
        "years": sorted(set(years)),
        "accounts": len(DEFAULT_ACCOUNTS),
    }
    if looks_like_sync_folder(core.paths.root):
        ctx.warn("SYNC_FOLDER", "同期フォルダ/ネットワーク上のpathです。DB正本はローカルNTFSに置いてください")
    if ctx.dry_run:
        ctx.warn("DRY_RUN", "dry-run: DBは作成していません")
        return {"plan": plan}
    conn = core.create_database()
    ctx.conn = conn
    with write_transaction(conn):
        org_id = new_id()
        conn.execute("INSERT INTO org (id, display_name, created_at) VALUES (?,?,?)", (org_id, name, ctx.now))
        for y in plan["years"]:
            _insert_year(ctx, org_id, y)
        codes = {}
        for a in DEFAULT_ACCOUNTS:
            acc_id = new_id()
            codes[a.code] = acc_id
            conn.execute(
                "INSERT INTO accounts (id, org_id, code, name, type, normal_side, open_item_type, role) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (acc_id, org_id, a.code, a.name, a.type, a.normal_side, a.open_item_type, a.role))
        for acc_code, sub_code, sub_name in DEFAULT_SUBACCOUNTS:
            conn.execute("INSERT INTO subaccounts (id, account_id, code, name) VALUES (?,?,?,?)",
                         (new_id(), codes[acc_code], sub_code, sub_name))
        ctx.audit(target_type="org", target_id=org_id, input_value=payload, detail=plan)
    core.runtime()  # restore_generation を初期化する
    return {"org_id": org_id, **plan}


@command("year.create", min_profile="owner", mutating=True)
def year_create(ctx: CommandContext, payload: dict) -> dict:
    """会計年度 (1/1〜12/31) を追加する。"""
    year = payload.get("year")
    if not isinstance(year, int):
        raise invalid("year は整数です")
    if ctx.db.execute("SELECT 1 FROM fiscal_years WHERE year=?", (year,)).fetchone():
        raise AsuraBookError("ALREADY_EXISTS", f"{year}年度は既にあります")
    year_id = _insert_year(ctx, ctx.org_id, year)
    ctx.audit(target_type="fiscal_year", target_id=year_id, input_value=payload)
    return {"fiscal_year_id": year_id, "year": year}


@command("year.list", min_profile="reader", mutating=False)
def year_list(ctx: CommandContext, payload: dict) -> list:
    """年度一覧と状態。"""
    return [dict(r) for r in ctx.db.execute("SELECT * FROM fiscal_years ORDER BY year").fetchall()]


@command("year.set_state", min_profile="owner", mutating=True)
def year_set_state(ctx: CommandContext, payload: dict) -> dict:
    """年度の workflow_state を進める/再開する。再開は理由必須で履歴を残す。"""
    year = resolve_year(ctx.db, payload.get("year"))
    state = payload.get("state")
    if state not in YEAR_STATES:
        raise invalid("state は open/preclose/adjusted/reviewed/frozen です")
    ctx.require_expected_revision(year["revision"], "年度")
    reopening = YEAR_STATES.index(state) < YEAR_STATES.index(year["workflow_state"])
    if reopening and not payload.get("reason"):
        raise invalid("年度の再開には reason が必要です")
    ctx.db.execute("UPDATE fiscal_years SET workflow_state=?, revision=revision+1 WHERE id=?", (state, year["id"]))
    ctx.audit(target_type="fiscal_year", target_id=year["id"], before_revision=year["revision"],
              after_revision=year["revision"] + 1, reason=payload.get("reason"),
              detail={"from": year["workflow_state"], "to": state, "reopen": reopening})
    return {"year": year["year"], "workflow_state": state, "reopened": reopening}


@command("period.set_state", min_profile="owner", mutating=True)
def period_set_state(ctx: CommandContext, payload: dict) -> dict:
    """月次締め (open/reviewing/soft_closed)。再開は理由必須。"""
    month = payload.get("month")  # YYYY-MM
    if not isinstance(month, str) or len(month) != 7:
        raise invalid("month は YYYY-MM です")
    start = parse_date(f"{month}-01", "month")
    end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    state = payload.get("state")
    if state not in ("open", "reviewing", "soft_closed"):
        raise invalid("state は open/reviewing/soft_closed です")
    year = resolve_year(ctx.db, start.year)
    current = ctx.db.execute("SELECT * FROM period_closes WHERE fiscal_year_id=? AND period_start=?",
                             (year["id"], start.isoformat())).fetchone()
    if current is not None and current["workflow_state"] == "soft_closed" and state != "soft_closed" \
            and not payload.get("reason"):
        raise invalid("締め解除には reason が必要です")
    if current is None:
        ctx.db.execute(
            "INSERT INTO period_closes (id, fiscal_year_id, period_start, period_end, workflow_state, reason, actor, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (new_id(), year["id"], start.isoformat(), end.isoformat(), state, payload.get("reason"), ctx.actor, ctx.now))
    else:
        ctx.db.execute("UPDATE period_closes SET workflow_state=?, reason=?, actor=?, revision=revision+1, "
                       "updated_at=? WHERE id=?", (state, payload.get("reason"), ctx.actor, ctx.now, current["id"]))
    ctx.audit(target_type="period", target_id=month, reason=payload.get("reason"),
              detail={"from": None if current is None else current["workflow_state"], "to": state})
    return {"month": month, "workflow_state": state}


@command("accounts.list", min_profile="reader", mutating=False)
def accounts_list(ctx: CommandContext, payload: dict) -> list:
    """勘定科目と補助科目の一覧。"""
    out = []
    for a in ctx.db.execute("SELECT * FROM accounts ORDER BY code").fetchall():
        d = dict(a)
        d["subaccounts"] = [dict(s) for s in ctx.db.execute(
            "SELECT id, code, name, active FROM subaccounts WHERE account_id=? ORDER BY code", (a["id"],))]
        out.append(d)
    return out


@command("accounts.create", min_profile="owner", mutating=True)
def accounts_create(ctx: CommandContext, payload: dict) -> dict:
    """勘定科目を追加する。"""
    t = payload.get("type")
    if t not in ("asset", "liability", "equity", "revenue", "expense"):
        raise invalid("type が不正です")
    side = payload.get("normal_side") or ("debit" if t in ("asset", "expense") else "credit")
    if not payload.get("code") or not payload.get("name"):
        raise invalid("code と name は必須です")
    if ctx.db.execute("SELECT 1 FROM accounts WHERE code=?", (payload["code"],)).fetchone():
        raise AsuraBookError("ALREADY_EXISTS", "同じ科目コードがあります")
    acc_id = new_id()
    ctx.db.execute(
        "INSERT INTO accounts (id, org_id, code, name, type, normal_side, open_item_type) VALUES (?,?,?,?,?,?,?)",
        (acc_id, ctx.org_id, payload["code"], payload["name"], t, side, payload.get("open_item_type")))
    ctx.audit(target_type="account", target_id=acc_id, input_value=payload)
    return dict(ctx.db.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone())


@command("accounts.set_active", min_profile="owner", mutating=True)
def accounts_set_active(ctx: CommandContext, payload: dict) -> dict:
    """科目の利用終了/再開。使用済み科目は削除しない。"""
    acc = resolve_account(ctx.db, payload.get("account"))
    active = 1 if payload.get("active") else 0
    ctx.db.execute("UPDATE accounts SET active=?, revision=revision+1 WHERE id=?", (active, acc["id"]))
    ctx.audit(target_type="account", target_id=acc["id"], detail={"active": active})
    return {"account_id": acc["id"], "active": bool(active)}


@command("subaccounts.create", min_profile="owner", mutating=True)
def subaccounts_create(ctx: CommandContext, payload: dict) -> dict:
    """補助科目を追加する (銀行・カード・サービス・取引先など)。"""
    acc = resolve_account(ctx.db, payload.get("account"))
    if not payload.get("code") or not payload.get("name"):
        raise invalid("code と name は必須です")
    sub_id = new_id()
    ctx.db.execute("INSERT INTO subaccounts (id, account_id, code, name) VALUES (?,?,?,?)",
                   (sub_id, acc["id"], payload["code"], payload["name"]))
    ctx.audit(target_type="subaccount", target_id=sub_id, input_value=payload)
    return {"subaccount_id": sub_id, "account_code": acc["code"], "code": payload["code"]}


@command("counterparties.create", min_profile="assistant", mutating=True)
def counterparties_create(ctx: CommandContext, payload: dict) -> dict:
    """取引先を追加する。登録番号は形式だけを検査し、公表情報の確認とは区別する。"""
    if not payload.get("display_name"):
        raise invalid("display_name は必須です")
    cp_id = new_id()
    ctx.db.execute("INSERT INTO counterparties (id, org_id, display_name, registration_number) VALUES (?,?,?,?)",
                   (cp_id, ctx.org_id, payload["display_name"], payload.get("registration_number")))
    ctx.audit(target_type="counterparty", target_id=cp_id, input_value=payload)
    return {"counterparty_id": cp_id}


@command("counterparties.list", min_profile="reader", mutating=False)
def counterparties_list(ctx: CommandContext, payload: dict) -> list:
    """取引先一覧。reader には名称を既定maskする。"""
    rows = [dict(r) for r in ctx.db.execute("SELECT * FROM counterparties ORDER BY display_name")]
    if ctx.session.profile == "reader":
        for r in rows:
            r["display_name"] = r["display_name"][:1] + "***"
    return rows


@command("source_accounts.create", min_profile="owner", mutating=True)
def source_accounts_create(ctx: CommandContext, payload: dict) -> dict:
    """銀行/カード/サービスの本人アカウントを登録し、帳簿科目と結び付ける。"""
    kind = payload.get("kind")
    if kind not in ("bank", "card", "platform", "other"):
        raise invalid("kind は bank/card/platform/other です")
    for key in ("code", "provider", "display_name"):
        if not payload.get(key):
            raise invalid(f"{key} は必須です")
    acc = resolve_account(ctx.db, payload["account"]) if payload.get("account") else None
    sub_id = None
    if payload.get("subaccount"):
        from ..lookups import resolve_subaccount
        sub_id = resolve_subaccount(ctx.db, acc["id"], payload["subaccount"])["id"]
    sa_id = new_id()
    ctx.db.execute(
        "INSERT INTO source_accounts (id, org_id, code, provider, kind, display_name, account_id, subaccount_id, purpose) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (sa_id, ctx.org_id, payload["code"], payload["provider"], kind, payload["display_name"],
         None if acc is None else acc["id"], sub_id, payload.get("purpose")))
    ctx.audit(target_type="source_account", target_id=sa_id, input_value=payload)
    return {"source_account_id": sa_id, "code": payload["code"]}


@command("source_accounts.list", min_profile="reader", mutating=False)
def source_accounts_list(ctx: CommandContext, payload: dict) -> list:
    """取込元アカウント一覧。"""
    return [dict(r) for r in ctx.db.execute("SELECT * FROM source_accounts ORDER BY code")]


@command("capability.issue", min_profile="owner", mutating=True, store_result=False)
def capability_issue(ctx: CommandContext, payload: dict) -> dict:
    """GUIで人が承認したplanに限り一回用capabilityを発行する。token本文はDBに残さない。"""
    if ctx.session.entry_point != "gui":
        raise AsuraBookError("PERMISSION_DENIED", "capability は製品GUIの承認画面からだけ発行できます")
    scope = payload.get("scope")
    if scope not in ("journal.post", "journal.reverse", "reconcile.apply", "reconcile.cancel", "restore.apply"):
        raise invalid("scope が不正です", scope=scope)
    targets = payload.get("targets")
    if not isinstance(targets, list) or not targets:
        raise invalid("targets は1件以上必要です")
    principal = payload.get("principal")
    if not principal:
        raise invalid("principal (委任先) は必須です")
    ttl = int(payload.get("ttl_seconds") or 600)
    if not (30 <= ttl <= 3600):
        raise invalid("ttl_seconds は30〜3600です")
    token = secrets.token_urlsafe(32)  # 256bit
    from ...domain.values import sha256_hex
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=ttl)).isoformat(
        timespec="microseconds").replace("+00:00", "Z")
    cap_id = new_id()
    ctx.db.execute(
        "INSERT INTO approval_capabilities (id, token_hash, principal, approval_session, org_id, scope, targets_json, "
        "plan_hash, expires_at, restore_generation, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (cap_id, sha256_hex(token), principal, ctx.session.session_id, ctx.org_id, scope, json.dumps(targets),
         content_hash(targets), expires, ctx.core.runtime().restore_generation, utc_now()))
    ctx.audit(target_type="capability", target_id=cap_id, detail={"scope": scope, "targets": targets,
                                                                  "principal": principal, "expires_at": expires})
    return {"capability_id": cap_id, "token": token, "expires_at": expires, "scope": scope}


@command("capability.revoke", min_profile="owner", mutating=True)
def capability_revoke(ctx: CommandContext, payload: dict) -> dict:
    """未使用のcapabilityを取り消す。"""
    cur = ctx.db.execute("UPDATE approval_capabilities SET revoked_at=? WHERE id=? AND consumed_at IS NULL "
                         "AND revoked_at IS NULL", (ctx.now, payload.get("capability_id")))
    if cur.rowcount != 1:
        raise AsuraBookError("NOT_FOUND", "取り消せる capability がありません")
    ctx.audit(target_type="capability", target_id=payload.get("capability_id"))
    return {"revoked": payload.get("capability_id")}
