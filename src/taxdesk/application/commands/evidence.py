"""証憑 command (08・09・13章)。

原本の保存完了後、cloud upload と OCR を独立して起動する。片方の失敗で他方を取り消さない。
文書・物理object・取引索引を分け、索引の訂正は新versionとして旧値を残す。
"""

from __future__ import annotations

import json
from pathlib import Path

from ...agents import receipt
from ...domain.errors import TaxDeskError, invalid, not_found
from ...domain.values import canonical_json, content_hash, new_id, parse_date
from ...evidence.spool import read_input, store
from .. import config as app_config
from ..core import CommandContext, command
from ..lookups import get_entry

READABLE_MIME = ("image/jpeg", "image/png", "image/gif", "image/webp", "application/pdf")
RECEIPT_KINDS = ("paper_photo", "paper_scan", "electronic", "screenshot", "unknown")


def _cloud_configured(ctx: CommandContext) -> bool:
    return bool(app_config.load(ctx.paths).get("cloud", {}).get("provider"))


def _queue_ocr(ctx: CommandContext, object_id: str, provider: str = "claude_cli") -> str:
    job_id = new_id()
    ctx.db.execute("INSERT INTO ocr_jobs (id, org_id, object_id, provider, state, created_at, updated_at) "
                   "VALUES (?,?,?,?, 'queued', ?,?)", (job_id, ctx.org_id, object_id, provider, ctx.now, ctx.now))
    return job_id


def document_view(conn, doc_id: str) -> dict:
    doc = conn.execute("SELECT * FROM evidence_documents WHERE id=?", (doc_id,)).fetchone()
    if doc is None:
        raise not_found("evidence_document", doc_id)
    objects = [dict(o) for o in conn.execute("SELECT * FROM evidence_objects WHERE document_id=? ORDER BY created_at",
                                             (doc_id,))]
    ocr = [dict(j) | {"extraction": json.loads(j["extraction_json"]) if j["extraction_json"] else None,
                      "validation": json.loads(j["validation_json"]) if j["validation_json"] else None}
           for j in conn.execute("SELECT o.* FROM ocr_jobs o JOIN evidence_objects b ON b.id=o.object_id "
                                 "WHERE b.document_id=? ORDER BY o.created_at", (doc_id,))]
    for j in ocr:
        j.pop("extraction_json", None)
        j.pop("validation_json", None)
    index = []
    for row in conn.execute("SELECT * FROM evidence_index_rows WHERE document_id=?", (doc_id,)):
        versions = [dict(v) for v in conn.execute(
            "SELECT * FROM evidence_index_versions WHERE index_row_id=? ORDER BY version", (row["id"],))]
        index.append({"index_row_id": row["id"], "current": versions[-1] if versions else None,
                      "versions": versions})
    links = [dict(l) for l in conn.execute(
        "SELECT l.*, e.journal_date, e.number, e.status FROM evidence_links l JOIN journal_entries e "
        "ON e.id=l.journal_entry_id WHERE l.document_id=?", (doc_id,))]
    receipts = [dict(r) for r in conn.execute(
        "SELECT r.* FROM evidence_receipts r JOIN evidence_objects o ON o.id=r.object_id WHERE o.document_id=? "
        "ORDER BY r.received_at", (doc_id,))]
    return dict(doc) | {"objects": objects, "ocr_jobs": ocr, "index": index, "links": links, "receipts": receipts}


@command("evidence.add", min_profile="assistant", mutating=True)
def evidence_add(ctx: CommandContext, payload: dict) -> dict:
    """証憑原本を受け付ける。同じbyte列の再投入は原本を共用し取得履歴を追加する。"""
    conn = ctx.db
    kind = payload.get("receipt_kind", "paper_photo")
    if kind not in RECEIPT_KINDS:
        raise invalid("receipt_kind が不正です", allowed=RECEIPT_KINDS)
    path = Path(payload.get("path") or "")
    data = read_input(path)
    obj = store(ctx.paths.spool, "evidence", data, path.name, write=not ctx.dry_run)
    existing = conn.execute("SELECT * FROM evidence_objects WHERE sha256=? AND role='original'",
                            (obj.sha256,)).fetchone()
    if existing is not None:
        conn.execute("INSERT INTO evidence_receipts (id, object_id, original_name, received_at, actor) "
                     "VALUES (?,?,?,?,?)", (new_id(), existing["id"], path.name, ctx.now, ctx.actor))
        ctx.warn("SAME_ORIGINAL_REUSED", "同じ内容の原本は登録済みです。取得履歴だけを追加しました")
        ctx.audit(target_type="evidence_object", target_id=existing["id"], detail={"reused": True})
        return document_view(conn, existing["document_id"]) | {"duplicate": True}
    doc_id, object_id = new_id(), new_id()
    cloud = _cloud_configured(ctx)
    conn.execute("INSERT INTO evidence_documents (id, org_id, receipt_kind, acquired_from, created_at) "
                 "VALUES (?,?,?,?,?)", (doc_id, ctx.org_id, kind, payload.get("acquired_from"), ctx.now))
    conn.execute(
        "INSERT INTO evidence_objects (id, org_id, document_id, role, sha256, mime, size_bytes, local_ref, sync_state, "
        "created_at) VALUES (?,?,?, 'original', ?,?,?,?,?,?)",
        (object_id, ctx.org_id, doc_id, obj.sha256, obj.mime, obj.size_bytes, obj.local_ref,
         "pending" if cloud else "not_configured", ctx.now))
    conn.execute("INSERT INTO evidence_receipts (id, object_id, original_name, received_at, actor) VALUES (?,?,?,?,?)",
                 (new_id(), object_id, path.name, ctx.now, ctx.actor))
    if cloud:
        conn.execute("INSERT INTO sync_outbox (id, org_id, object_id, operation, idempotency_key, state, created_at, "
                     "updated_at) VALUES (?,?,?, 'upload', ?, 'queued', ?,?)",
                     (new_id(), ctx.org_id, object_id, f"upload:{object_id}", ctx.now, ctx.now))
    else:
        ctx.warn("CLOUD_NOT_CONFIGURED", "クラウド保管先が未設定です。原本はローカルだけに保存されています")
    auto_read = payload.get("auto_read")
    if auto_read is None:
        auto_read = app_config.load(ctx.paths)["ai"].get("auto_read_receipts", True)
    ocr_job = None
    if auto_read:
        if obj.mime in READABLE_MIME:
            ocr_job = _queue_ocr(ctx, object_id)
        elif obj.mime == "image/heic":
            ctx.warn("HEIC_NOT_READABLE", "HEICの変換decoderが未設定のため自動読取りしません。手入力できます")
        else:
            ctx.warn("NOT_READABLE", f"{obj.mime} は自動読取りの対象外です")
    ctx.audit(target_type="evidence_document", target_id=doc_id, input_value={"sha256": obj.sha256, "kind": kind})
    return document_view(conn, doc_id) | {"duplicate": False, "queued_ocr_job_id": ocr_job}


@command("evidence.list", min_profile="reader", mutating=False)
def evidence_list(ctx: CommandContext, payload: dict) -> list:
    """証憑一覧 (同期状態・読取状態・関連仕訳の件数)。"""
    rows = ctx.db.execute(
        "SELECT d.id, d.receipt_kind, d.created_at, o.id AS object_id, o.mime, o.size_bytes, o.sync_state, o.sha256, "
        "(SELECT state FROM ocr_jobs j WHERE j.object_id=o.id ORDER BY created_at DESC LIMIT 1) AS ocr_state, "
        "(SELECT COUNT(*) FROM evidence_links l WHERE l.document_id=d.id) AS link_count "
        "FROM evidence_documents d JOIN evidence_objects o ON o.document_id=d.id AND o.role='original' "
        "ORDER BY d.created_at DESC").fetchall()
    return [dict(r) for r in rows]


@command("evidence.show", min_profile="reader", mutating=False)
def evidence_show(ctx: CommandContext, payload: dict) -> dict:
    """証憑1件の原本・派生物・読取結果・索引版・関連仕訳。"""
    return document_view(ctx.db, payload.get("document_id"))


@command("evidence.read", min_profile="assistant", mutating=True)
def evidence_read(ctx: CommandContext, payload: dict) -> dict:
    """証憑の読取り (OCR) を待ち行列へ入れる。既に待機/実行中ならそれを返す。"""
    conn = ctx.db
    obj = conn.execute("SELECT * FROM evidence_objects WHERE document_id=? AND role='original'",
                       (payload.get("document_id"),)).fetchone()
    if obj is None:
        raise not_found("evidence_document", str(payload.get("document_id")))
    if obj["mime"] not in READABLE_MIME:
        raise TaxDeskError("NOT_SUPPORTED", f"{obj['mime']} は読取り対象外です")
    active = conn.execute("SELECT * FROM ocr_jobs WHERE object_id=? AND state IN ('queued','running','preprocessing')",
                          (obj["id"],)).fetchone()
    if active:
        return {"ocr_job_id": active["id"], "state": active["state"], "already_active": True}
    job = _queue_ocr(ctx, obj["id"], payload.get("provider", "claude_cli"))
    ctx.audit(target_type="ocr_job", target_id=job)
    return {"ocr_job_id": job, "state": "queued", "already_active": False}


@command("evidence.review", min_profile="assistant", mutating=True)
def evidence_review(ctx: CommandContext, payload: dict) -> dict:
    """利用者が確認した索引値を新versionとして保存する。人の確認値は再解析で上書きされない。"""
    conn = ctx.db
    doc = conn.execute("SELECT * FROM evidence_documents WHERE id=?", (payload.get("document_id"),)).fetchone()
    if doc is None:
        raise not_found("evidence_document", str(payload.get("document_id")))
    values = payload.get("values") or {}
    if values.get("business_date") is not None:
        parse_date(values["business_date"], "business_date")
    amount = values.get("amount_jpy")
    if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int)):
        raise invalid("amount_jpy は円整数です (返金は負数可)")
    decision = payload.get("decision", "confirm")
    if decision not in ("confirm", "withdraw"):
        raise invalid("decision は confirm/withdraw です")
    status = "withdrawn" if decision == "withdraw" else (
        "confirmed" if ctx.session.allows("bookkeeper_scoped") else "proposed")
    row_id = payload.get("index_row_id")
    if row_id is None:
        row_id = new_id()
        conn.execute("INSERT INTO evidence_index_rows (id, document_id, created_at) VALUES (?,?,?)",
                     (row_id, doc["id"], ctx.now))
        version = 1
    else:
        version = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM evidence_index_versions WHERE index_row_id=?",
                               (row_id,)).fetchone()[0]
    conn.execute(
        "INSERT INTO evidence_index_versions (id, index_row_id, version, business_date, amount_jpy, counterparty, "
        "transaction_kind, page_from, page_to, raw_text, status, reason, actor, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_id(), row_id, version, values.get("business_date"), amount, values.get("counterparty"),
         values.get("transaction_kind"), values.get("page_from"), values.get("page_to"), values.get("raw_text"),
         status, payload.get("reason"), ctx.actor, ctx.now))
    if payload.get("ocr_job_id"):
        conn.execute("UPDATE ocr_jobs SET state='reviewed', reviewed_json=?, reviewed_by=?, updated_at=? "
                     "WHERE id=? AND state IN ('extracted','needs_review')",
                     (canonical_json(values), ctx.actor, ctx.now, payload["ocr_job_id"]))
    ctx.audit(target_type="evidence_index", target_id=row_id, input_value=values,
              detail={"version": version, "status": status})
    return {"index_row_id": row_id, "version": version, "status": status}


@command("evidence.link", min_profile="assistant", mutating=True)
def evidence_link(ctx: CommandContext, payload: dict) -> dict:
    """証憑と仕訳を関連付ける (N:M)。証憑の追加で経費を再計上しない。"""
    conn = ctx.db
    doc = conn.execute("SELECT * FROM evidence_documents WHERE id=?", (payload.get("document_id"),)).fetchone()
    if doc is None:
        raise not_found("evidence_document", str(payload.get("document_id")))
    entry = get_entry(conn, payload.get("entry_id"))
    obj = conn.execute("SELECT id FROM evidence_objects WHERE document_id=? AND role='original'",
                       (doc["id"],)).fetchone()
    link_id = new_id()
    cur = conn.execute("INSERT OR IGNORE INTO evidence_links (id, document_id, object_id, index_row_id, "
                       "journal_entry_id, actor, created_at) VALUES (?,?,?,?,?,?,?)",
                       (link_id, doc["id"], obj["id"], payload.get("index_row_id"), entry["id"], ctx.actor, ctx.now))
    if cur.rowcount == 0:
        ctx.warn("ALREADY_LINKED", "既に関連付け済みです")
    ctx.audit(target_type="evidence_link", target_id=link_id, detail={"document_id": doc["id"], "entry_id": entry["id"]})
    return {"document_id": doc["id"], "entry_id": entry["id"], "linked": cur.rowcount == 1}


@command("evidence.match_candidates", min_profile="reader", mutating=False)
def evidence_match_candidates(ctx: CommandContext, payload: dict) -> list:
    """読取り/確認値と同額・近い日付の確定仕訳を候補として返す。金額一致は順位の材料に留める。"""
    amount = payload.get("amount_jpy")
    date = payload.get("business_date")
    if amount is None or date is None:
        raise invalid("amount_jpy と business_date が必要です")
    parse_date(date, "business_date")
    rows = ctx.db.execute(
        "SELECT DISTINCT e.id, e.journal_date, e.number, e.description, l.amount_jpy, a.name AS account_name "
        "FROM journal_entries e JOIN journal_lines l ON l.entry_id=e.id JOIN accounts a ON a.id=l.account_id "
        "WHERE e.status='posted' AND l.amount_jpy=? AND e.journal_date BETWEEN date(?, '-10 days') AND date(?, '+40 days') "
        "AND a.type IN ('expense','asset','liability') ORDER BY abs(julianday(e.journal_date)-julianday(?))",
        (abs(int(amount)), date, date, date)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- worker 用の内部 command

@command("worker.ocr_claim", min_profile="assistant", mutating=True, gate="ai")
def worker_ocr_claim(ctx: CommandContext, payload: dict) -> dict | None:
    """待機中のOCR jobを1件取り出し running にする (AI実行はtransaction外)。"""
    conn = ctx.db
    job = conn.execute("SELECT j.*, o.local_ref, o.mime, o.sha256 FROM ocr_jobs j JOIN evidence_objects o "
                       "ON o.id=j.object_id WHERE j.state='queued' ORDER BY j.created_at LIMIT 1").fetchone()
    if job is None:
        return None
    attempt = conn.execute("SELECT COUNT(*)+1 FROM agent_jobs a JOIN ocr_jobs j ON j.agent_job_id=a.id "
                           "WHERE j.object_id=?", (job["object_id"],)).fetchone()[0]
    agent_id = new_id()
    input_hash = content_hash({"sha256": job["sha256"], "prompt": receipt.PROMPT_VERSION,
                               "schema": receipt.SCHEMA_VERSION, "provider": job["provider"]})
    conn.execute(
        "INSERT INTO agent_jobs (id, org_id, provider, job_kind, prompt_version, schema_version, policy_version, "
        "input_hash, snapshot_revision, status, attempt, created_at, started_at) "
        "VALUES (?,?,?, 'receipt.extract', ?,?,?,?,?, 'running', ?,?,?)",
        (agent_id, ctx.org_id, job["provider"], receipt.PROMPT_VERSION, receipt.SCHEMA_VERSION,
         "agent_policy/1.0", input_hash, ctx.ledger_revision(), attempt, ctx.now, ctx.now))
    conn.execute("UPDATE ocr_jobs SET state='running', agent_job_id=?, updated_at=? WHERE id=?",
                 (agent_id, ctx.now, job["id"]))
    ctx.audit(target_type="agent_job", target_id=agent_id, job_id=agent_id)
    return {"ocr_job_id": job["id"], "agent_job_id": agent_id, "object_id": job["object_id"],
            "local_ref": job["local_ref"], "mime": job["mime"], "sha256": job["sha256"], "provider": job["provider"]}


_OCR_STATE = {
    "succeeded": None,  # 検証結果で決める
    "invalid_output": "retryable_failed",
    "failed": "retryable_failed",
    "auth_required": "retryable_failed",
    "quota_wait": "retryable_failed",
    "timed_out": "outcome_unknown",
    "outcome_unknown": "outcome_unknown",
    "cancelled": "cancelled",
}


@command("worker.ocr_complete", min_profile="assistant", mutating=True)
def worker_ocr_complete(ctx: CommandContext, payload: dict) -> dict:
    """AI実行結果を core が検証して採用する。schema不適合は invalid_output として候補にしない。"""
    conn = ctx.db
    job = conn.execute("SELECT * FROM ocr_jobs WHERE id=? AND agent_job_id=?",
                       (payload.get("ocr_job_id"), payload.get("agent_job_id"))).fetchone()
    if job is None or job["state"] != "running":
        raise TaxDeskError("STALE_RESULT", "対象jobは実行中ではありません。結果を採用しません")
    result = payload.get("result") or {}
    status = result.get("status", "outcome_unknown")
    if status not in _OCR_STATE:
        raise invalid("result.status が不正です")
    extraction = result.get("structured_output")
    issues: list[dict] = []
    ocr_state = _OCR_STATE[status]
    agent_status = status
    if status == "succeeded":
        schema_ok, issues = receipt.validate(extraction)
        if not schema_ok:
            agent_status, ocr_state = "invalid_output", "retryable_failed"
        else:
            ocr_state = "needs_review" if issues else "extracted"
    if ocr_state == "retryable_failed" and status in ("failed", "invalid_output"):
        attempts = conn.execute("SELECT COUNT(*) FROM agent_jobs a JOIN ocr_jobs j ON j.agent_job_id=a.id "
                                "WHERE j.object_id=?", (job["object_id"],)).fetchone()[0]
        if attempts >= 3:
            ocr_state = "permanent_failed"
    conn.execute(
        "UPDATE agent_jobs SET status=?, model=?, cli_version=?, usage_json=?, result_hash=?, result_json=?, "
        "error_code=?, error_message=?, process_identity=?, finished_at=? WHERE id=?",
        (agent_status, result.get("model"), result.get("cli_version"),
         None if result.get("usage") is None else canonical_json(result["usage"]),
         None if extraction is None else content_hash(extraction),
         None if extraction is None else canonical_json(extraction),
         result.get("error_code") or (None if agent_status == "succeeded" else agent_status.upper()),
         result.get("error_message"), result.get("process_identity"), ctx.now, job["agent_job_id"]))
    keep_extraction = agent_status == "succeeded"
    conn.execute("UPDATE ocr_jobs SET state=?, extraction_json=?, validation_json=?, updated_at=? WHERE id=?",
                 (ocr_state, canonical_json(extraction) if keep_extraction else None,
                  canonical_json(issues), ctx.now, job["id"]))
    ctx.audit(target_type="ocr_job", target_id=job["id"], job_id=job["agent_job_id"],
              result_value={"status": agent_status, "ocr_state": ocr_state})
    return {"ocr_job_id": job["id"], "agent_status": agent_status, "ocr_state": ocr_state, "issues": issues}


@command("worker.upload_claim", min_profile="assistant", mutating=True, gate="upload")
def worker_upload_claim(ctx: CommandContext, payload: dict) -> dict | None:
    """送信待ちの原本を1件 lease する。outbox は外部通信前に commit 済み。"""
    conn = ctx.db
    row = conn.execute(
        "SELECT x.*, o.local_ref, o.sha256, o.size_bytes FROM sync_outbox x JOIN evidence_objects o ON o.id=x.object_id "
        "WHERE (x.state='queued' OR (x.state='retry_wait' AND COALESCE(x.next_attempt_at,'') <= ?) "
        "OR (x.state='running' AND x.lease_until < ?)) ORDER BY x.created_at LIMIT 1", (ctx.now, ctx.now)).fetchone()
    if row is None:
        return None
    token = new_id()
    import datetime as dt
    lease_until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    conn.execute("UPDATE sync_outbox SET state='running', attempt=attempt+1, lease_token=?, lease_until=?, updated_at=? "
                 "WHERE id=?", (token, lease_until, ctx.now, row["id"]))
    conn.execute("UPDATE evidence_objects SET sync_state='uploading' WHERE id=?", (row["object_id"],))
    return {"outbox_id": row["id"], "lease_token": token, "object_id": row["object_id"], "local_ref": row["local_ref"],
            "sha256": row["sha256"], "key": f"orgs/{ctx.org_id}/objects/{row['object_id']}",
            "attempt": row["attempt"] + 1}


@command("worker.upload_complete", min_profile="assistant", mutating=True)
def worker_upload_complete(ctx: CommandContext, payload: dict) -> dict:
    """upload結果を保存する。hash と version を確認できたものだけ verified にする。"""
    conn = ctx.db
    row = conn.execute("SELECT * FROM sync_outbox WHERE id=?", (payload.get("outbox_id"),)).fetchone()
    if row is None or row["lease_token"] != payload.get("lease_token"):
        raise TaxDeskError("STALE_RESULT", "lease が一致しません。結果を採用しません")
    outcome = payload.get("outcome")
    obj = conn.execute("SELECT * FROM evidence_objects WHERE id=?", (row["object_id"],)).fetchone()
    if outcome == "verified":
        if payload.get("remote_sha256") != obj["sha256"]:
            raise TaxDeskError("INTEGRITY_ERROR", "保管先の hash が原本と一致しません")
        conn.execute("UPDATE sync_outbox SET state='succeeded', lease_token=NULL, lease_until=NULL, last_error=NULL, "
                     "updated_at=? WHERE id=?", (ctx.now, row["id"]))
        conn.execute("UPDATE evidence_objects SET sync_state='verified', cloud_key=?, cloud_version=? WHERE id=?",
                     (payload.get("key"), payload.get("remote_version"), obj["id"]))
    else:
        import datetime as dt
        delay = min(3600, 30 * (2 ** min(row["attempt"], 6)))
        next_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay)).isoformat().replace("+00:00", "Z")
        final = row["attempt"] >= 8
        conn.execute("UPDATE sync_outbox SET state=?, lease_token=NULL, lease_until=NULL, next_attempt_at=?, "
                     "last_error=?, updated_at=? WHERE id=?",
                     ("failed" if final else "retry_wait", next_at, str(payload.get("error"))[:500], ctx.now, row["id"]))
        conn.execute("UPDATE evidence_objects SET sync_state=? WHERE id=?",
                     ("failed" if final else "retry_wait", obj["id"]))
    ctx.audit(target_type="sync_outbox", target_id=row["id"], result_value={"outcome": outcome})
    return {"outbox_id": row["id"], "outcome": outcome}


@command("ai.jobs", min_profile="reader", mutating=False)
def ai_jobs(ctx: CommandContext, payload: dict) -> list:
    """AI job の実行履歴 (Provider・状態・利用量・error)。"""
    rows = ctx.db.execute("SELECT id, provider, job_kind, model, cli_version, status, attempt, error_code, "
                          "error_message, usage_json, created_at, finished_at FROM agent_jobs "
                          "ORDER BY created_at DESC LIMIT ?", (int(payload.get("limit") or 100),)).fetchall()
    return [dict(r) for r in rows]
