"""取込 command (05章)。

inspect は純粋preview。stage は原本保全→観測→経済イベント→利用枠までを登録し、仕訳は作らない。
取込成功は「資料と確認待ち候補を保存した状態」であり、posted への確定を意味しない。
"""

from __future__ import annotations

import json
from pathlib import Path

from ...domain.errors import TaxDeskError, invalid
from ...domain.values import canonical_json, content_hash, new_id
from ...evidence.spool import read_input, store
from ...importers import generic_csv
from ..core import CommandContext, command

PROFILES = {"generic-v1": generic_csv}


def _profile_module(name: str | None):
    if name not in PROFILES:
        raise TaxDeskError("PROFILE_NOT_AVAILABLE", "未対応の取込profileです。実サンプルで検証したprofileだけを使えます",
                           {"profile": name, "available": sorted(PROFILES)})
    return PROFILES[name]


def _ensure_profile_row(ctx: CommandContext, module) -> str:
    p = module.PROFILE
    row = ctx.db.execute("SELECT id FROM provider_profiles WHERE provider=? AND data_kind=? AND version=?",
                         (p["provider"], p["data_kind"], p["version"])).fetchone()
    if row:
        return row["id"]
    pid = new_id()
    ctx.db.execute("INSERT INTO provider_profiles (id, provider, data_kind, version, payload_json, payload_hash, "
                   "created_at) VALUES (?,?,?,?,?,?,?)",
                   (pid, p["provider"], p["data_kind"], p["version"], canonical_json(p), content_hash(p), ctx.now))
    return pid


@command("import.inspect", min_profile="reader", mutating=False, needs_db=False)
def import_inspect(ctx: CommandContext, payload: dict) -> dict:
    """副作用なしでファイルを解析し、正規化結果と issue を返す。"""
    module = _profile_module(payload.get("profile"))
    path = Path(payload.get("path") or "")
    data = read_input(path)
    result = module.parse(data, expected_account=payload.get("account"))
    return {
        "summary": result.summary(),
        "header": result.header,
        "rows": [{"locator": r.locator, "row_id": r.row_id, "event_type": r.event_type, "date": r.primary_date,
                  "amount_jpy": r.amount_jpy, "components": dict(r.components), "description": r.description,
                  "held": r.held} for r in result.rows[: int(payload.get("limit") or 200)]],
        "issues": [i.to_dict() for i in result.issues],
    }


@command("import.stage", min_profile="assistant", mutating=True)
def import_stage(ctx: CommandContext, payload: dict) -> dict:
    """原本を保全し、観測と確認待ちの経済イベントを登録する。"""
    conn = ctx.db
    module = _profile_module(payload.get("profile"))
    code = payload.get("account")
    sa = conn.execute("SELECT * FROM source_accounts WHERE code=?", (code,)).fetchone()
    if sa is None:
        raise TaxDeskError("NOT_FOUND", "取込元アカウントがありません。source_accounts.create で登録してください",
                           {"account": code})
    path = Path(payload.get("path") or "")
    data = read_input(path)
    result = module.parse(data, expected_account=code)
    if result.fatal:
        raise TaxDeskError(result.issues[0].code, result.issues[0].message,
                           {"issues": [i.to_dict() for i in result.issues]})
    # 原本の永続化を先に完了する (DB登録前に停止した原本は doctor で回収)
    obj = store(ctx.paths.spool, "sources", data, path.name, write=not ctx.dry_run)
    file_row = conn.execute("SELECT * FROM source_files WHERE sha256=?", (obj.sha256,)).fetchone()
    if file_row is None:
        file_id = new_id()
        conn.execute("INSERT INTO source_files (id, org_id, sha256, size_bytes, mime, local_ref, created_at) "
                     "VALUES (?,?,?,?,?,?,?)", (file_id, ctx.org_id, obj.sha256, obj.size_bytes, obj.mime,
                                                obj.local_ref, ctx.now))
    else:
        file_id = file_row["id"]
    conn.execute("INSERT INTO source_file_receipts (id, source_file_id, original_name, received_at, actor) "
                 "VALUES (?,?,?,?,?)", (new_id(), file_id, path.name, ctx.now, ctx.actor))
    profile_id = _ensure_profile_row(ctx, module)
    acceptance_key = f"{sa['id']}|{profile_id}|{obj.sha256}"
    existing = conn.execute("SELECT * FROM import_batches WHERE acceptance_key=?", (acceptance_key,)).fetchone()
    if existing is not None:
        ctx.warn("SAME_FILE_REUSED", "同じ内容の原本は取込済みです。取得履歴だけを追加しました")
        return {"batch_id": existing["id"], "duplicate_file": True, "summary": json.loads(existing["coverage_json"])}

    batch_id = new_id()
    summary = result.summary()
    coverage = summary | {"complete": "unconfirmed",
                          "note": "ファイル最初/最後の日付だけで全期間取得済みとは判定しません"}
    attempt_id = new_id()
    conn.execute(
        "INSERT INTO import_batches (id, org_id, source_account_id, source_file_id, profile_id, acceptance_key, status, "
        "coverage_json, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (batch_id, ctx.org_id, sa["id"], file_id, profile_id, acceptance_key,
         "needs_review" if result.issues else "staged", canonical_json(coverage), ctx.actor, ctx.now))
    conn.execute(
        "INSERT INTO import_attempts (id, batch_id, parser_version, profile_version, status, row_count, new_events, "
        "known_events, held_rows, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (attempt_id, batch_id, module.PARSER_VERSION, module.PROFILE["version"], "succeeded", result.total_rows,
         0, 0, 0, ctx.now))

    observation_ids: dict[str, str] = {}
    for locator, raw in result.raw_rows:
        obs_id = new_id()
        observation_ids[locator] = obs_id
        conn.execute(
            "INSERT INTO source_observations (id, attempt_id, source_file_id, locator, raw_json, raw_external_id, "
            "observed_at) VALUES (?,?,?,?,?,?,?)",
            (obs_id, attempt_id, file_id, locator, json.dumps(raw, ensure_ascii=False),
             raw.get("external_id") if isinstance(raw, dict) else None, ctx.now))

    new_events = known = held = 0
    events = []
    for row in result.rows:
        obs_id = observation_ids[row.locator]
        if row.held:
            held += 1
            continue
        identity = f"{module.PROFILE['provider']}|{sa['code']}|{row.row_id}"
        current = conn.execute("SELECT * FROM source_transactions WHERE normalized_identity_key=?",
                               (identity,)).fetchone()
        if current is not None:
            same = (current["amount_jpy"] == row.amount_jpy and current["event_type"] == row.event_type
                    and current["transaction_date"] == row.transaction_date)
            conn.execute("INSERT OR IGNORE INTO event_observation_links (event_id, observation_id, basis) "
                         "VALUES (?,?,?)", (current["id"], obs_id, "same_identity" if same else "changed_observation"))
            if same:
                known += 1
                events.append({"event_id": current["id"], "row_id": row.row_id, "state": "known"})
            else:
                held += 1
                result.issues.append(generic_csv.RowIssue(
                    "SOURCE_REVISION_CONFLICT", "needs_review", row.locator, "amount_jpy", None,
                    "同じ row_id で内容が変わっています。観測差分を確認してください (既存イベントは変更していません)"))
                events.append({"event_id": current["id"], "row_id": row.row_id, "state": "conflict"})
            continue
        event_id = new_id()
        amount = row.amount_jpy
        if row.event_type == "sale":
            amount = dict(row.components).get("gross")
        conn.execute(
            "INSERT INTO source_transactions (id, org_id, source_account_id, event_type, raw_provider_id, "
            "normalized_identity_key, transaction_date, recognition_date, settlement_date, period_start, period_end, "
            "grain, currency, amount_jpy, description, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, ctx.org_id, sa["id"], row.event_type, row.external_id or row.row_id, identity,
             row.transaction_date, row.recognition_date, row.settlement_date, row.period_start, row.period_end,
             row.grain, row.currency, amount, row.description, ctx.now))
        conn.execute("INSERT INTO event_observation_links (event_id, observation_id, basis) VALUES (?,?,?)",
                     (event_id, obs_id, "created"))
        if row.amount_jpy is not None and row.event_type != "sale":
            conn.execute("INSERT INTO source_components (id, event_id, kind, amount_jpy, observation_id) "
                         "VALUES (?,?,?,?,?)", (new_id(), event_id, "amount", row.amount_jpy, obs_id))
        for kind, value in row.components:
            conn.execute("INSERT INTO source_components (id, event_id, kind, amount_jpy, observation_id) "
                         "VALUES (?,?,?,?,?)", (new_id(), event_id, kind, value, obs_id))
        capacity = row.capacity_jpy
        if capacity is not None and row.grain != "net_only":
            conn.execute("INSERT INTO source_usage_slots (id, event_id, role, capacity_jpy) VALUES (?,?,?,?)",
                         (new_id(), event_id, row.role, capacity))
        new_events += 1
        events.append({"event_id": event_id, "row_id": row.row_id, "state": "new"})

    for issue in result.issues:
        conn.execute(
            "INSERT INTO import_issues (id, attempt_id, code, severity, locator, field, raw_masked, message, remedy) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (new_id(), attempt_id, issue.code, issue.severity, issue.locator, issue.field, issue.raw_masked,
             issue.message, issue.remedy))
    status = "partial" if held else "succeeded"
    conn.execute("UPDATE import_attempts SET status=?, new_events=?, known_events=?, held_rows=? WHERE id=?",
                 (status, new_events, known, held + (result.total_rows - len(result.rows)), attempt_id))
    if any(i.severity in ("needs_review", "error") for i in result.issues):
        conn.execute("UPDATE import_batches SET status='needs_review' WHERE id=?", (batch_id,))
    ctx.audit(target_type="import_batch", target_id=batch_id, input_value={"sha256": obj.sha256, "account": code},
              result_value={"new": new_events, "known": known, "held": held})
    return {
        "batch_id": batch_id, "duplicate_file": False, "source_file_sha256": obj.sha256,
        "summary": coverage, "new_events": new_events, "known_events": known,
        "held_rows": held + (result.total_rows - len(result.rows)),
        "accounted_rows": new_events + known + held + (result.total_rows - len(result.rows)),
        "events": events, "issues": [i.to_dict() for i in result.issues],
    }


@command("import.review", min_profile="reader", mutating=False)
def import_review(ctx: CommandContext, payload: dict) -> dict:
    """取込batchの原本・観測・イベント・issue を確認する。"""
    conn = ctx.db
    batch = conn.execute("SELECT * FROM import_batches WHERE id=?", (payload.get("batch_id"),)).fetchone()
    if batch is None:
        raise invalid("batch_id が見つかりません")
    attempts = [dict(a) for a in conn.execute("SELECT * FROM import_attempts WHERE batch_id=? ORDER BY created_at",
                                              (batch["id"],))]
    events = [dict(r) for r in conn.execute(
        "SELECT DISTINCT t.*, o.locator, l.basis, "
        "(SELECT COALESCE(SUM(s.signed_effect_jpy),0) FROM source_links s JOIN journal_lines jl ON jl.id=s.journal_line_id "
        " JOIN journal_entries e ON e.id=jl.entry_id WHERE s.event_id=t.id AND e.status='posted') AS used_jpy "
        "FROM source_transactions t JOIN event_observation_links l ON l.event_id=t.id "
        "JOIN source_observations o ON o.id=l.observation_id JOIN import_attempts a ON a.id=o.attempt_id "
        "WHERE a.batch_id=? ORDER BY o.locator", (batch["id"],))]
    issues = [dict(r) for r in conn.execute(
        "SELECT i.* FROM import_issues i JOIN import_attempts a ON a.id=i.attempt_id WHERE a.batch_id=? "
        "ORDER BY i.locator", (batch["id"],))]
    return {"batch": dict(batch) | {"coverage": json.loads(batch["coverage_json"])}, "attempts": attempts,
            "events": events, "issues": issues}


@command("import.events", min_profile="reader", mutating=False)
def import_events(ctx: CommandContext, payload: dict) -> list:
    """未記帳の経済イベント (利用枠に残りがあるもの) を返す。"""
    rows = ctx.db.execute(
        "SELECT t.*, sa.code AS source_account_code, u.role, u.capacity_jpy, "
        "(SELECT COALESCE(SUM(s.signed_effect_jpy),0) FROM source_links s JOIN journal_lines jl ON jl.id=s.journal_line_id "
        " JOIN journal_entries e ON e.id=jl.entry_id WHERE s.usage_slot_id=u.id AND e.status='posted') AS used_jpy "
        "FROM source_transactions t JOIN source_accounts sa ON sa.id=t.source_account_id "
        "LEFT JOIN source_usage_slots u ON u.event_id=t.id ORDER BY t.transaction_date, t.id").fetchall()
    out = [dict(r) for r in rows]
    if not payload.get("include_used"):
        out = [r for r in out if r["capacity_jpy"] is None or r["used_jpy"] < r["capacity_jpy"]]
    return out
