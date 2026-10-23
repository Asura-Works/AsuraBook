"""backup / restore (16章3〜7節)。

- 稼働中DBの本体をコピーせず、SQLite online backup API で一貫snapshotを作る。
- 未同期原本は必ず含める。完全backupは全原本を含める。
- 復元は: 検証 → runtime_control に新 restore_generation と recovery_hold を保存 → 保守mutex →
  現データ退避 → 置換 → read-only 検証 → 外部jobを保留状態へ、の順に行う。
- 暗号化は未実装 (認証付き暗号の実績ある形式と鍵管理を選定してから実装する)。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from ..domain.errors import TaxDeskError, invalid
from ..domain.values import new_id, utc_now
from ..infrastructure import runtime_control
from ..infrastructure.db.connection import connect
from ..infrastructure.db.migrate import current_version, latest_version
from ..infrastructure.locks import FileLock, LockBusy
from .core import APP_VERSION, Core

MANIFEST = "manifest.json"
DB_NAME = "taxdesk.db"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_db(path: Path) -> dict:
    conn = connect(path, readonly=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        version = current_version(conn)
        org = conn.execute("SELECT id, display_name, ledger_revision FROM org").fetchone()
        audit = conn.execute("SELECT COALESCE(MAX(sequence_no),0) FROM audit_events").fetchone()[0]
        tb = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN side='debit' THEN amount_jpy ELSE -amount_jpy END),0) FROM journal_lines l "
            "JOIN journal_entries e ON e.id=l.entry_id WHERE e.status='posted'").fetchone()[0]
        return {"integrity": integrity, "foreign_key_violations": len(fk), "schema_version": version,
                "org_id": org["id"] if org else None, "display_name": org["display_name"] if org else None,
                "ledger_revision": org["ledger_revision"] if org else None, "audit_cutoff_seq": audit,
                "posted_balance_jpy": tb}
    finally:
        conn.close()


def create(core: Core, dest_dir: Path, *, full: bool = True) -> dict:
    if not core.db_exists():
        raise TaxDeskError("NOT_INITIALIZED", "DBがありません")
    dest_dir = dest_dir.resolve()
    data_root = core.paths.root.resolve()
    if dest_dir == data_root or data_root in dest_dir.parents:
        raise invalid("backup先は利用者データフォルダの外にしてください", dest=str(dest_dir))
    dest_dir.mkdir(parents=True, exist_ok=True)
    backup_id = new_id()
    stamp = utc_now().replace(":", "").replace("-", "").split(".")[0]
    out = dest_dir / f"taxdesk-backup-{stamp}-{backup_id[:8]}"
    out.mkdir()
    db_copy = out / DB_NAME
    src = connect(core.paths.db, readonly=True)
    try:
        target = sqlite3.connect(str(db_copy))
        src.backup(target)
        target.close()
        objects = [dict(r) for r in src.execute(
            "SELECT 'evidence' AS kind, id, local_ref, sha256, size_bytes, sync_state, cloud_key, cloud_version "
            "FROM evidence_objects UNION ALL SELECT 'sources', id, local_ref, sha256, size_bytes, 'not_configured', "
            "NULL, NULL FROM source_files")]
    finally:
        src.close()
    check = _check_db(db_copy)
    if check["integrity"] != "ok" or check["foreign_key_violations"]:
        shutil.rmtree(out)
        raise TaxDeskError("INTEGRITY_ERROR", "backup の整合検査に失敗しました", check)
    entries = []
    unsynced = 0
    for o in objects:
        local_ref = o["local_ref"]
        include = full or o["sync_state"] != "verified"
        if o["sync_state"] != "verified":
            unsynced += 1
        if include:
            src_file = core.paths.spool / local_ref
            if not src_file.exists():
                raise TaxDeskError("INTEGRITY_ERROR", "原本が spool にありません", {"local_ref": local_ref})
            dst = out / "spool" / local_ref
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_file, dst)
            if _sha256_file(dst) != o["sha256"]:
                raise TaxDeskError("INTEGRITY_ERROR", "原本コピーの hash が一致しません", {"local_ref": local_ref})
        entries.append({"kind": o["kind"], "local_ref": local_ref, "sha256": o["sha256"], "size_bytes": o["size_bytes"],
                        "included": include, "sync_state": o["sync_state"], "cloud_key": o["cloud_key"],
                        "cloud_version": o["cloud_version"]})
    manifest = {
        "backup_id": backup_id, "created_at": utc_now(), "app_version": APP_VERSION,
        "schema_version": check["schema_version"], "org_id": check["org_id"], "display_name": check["display_name"],
        "ledger_revision": check["ledger_revision"], "audit_cutoff_seq": check["audit_cutoff_seq"],
        "db_sha256": _sha256_file(db_copy), "full": full, "objects": entries, "unsynced_objects": unsynced,
        "encryption": "none",
        "excluded": ["runtime_control.json", "config.json", "認証token/PIN/password"],
        "restore_requirements": "復元後は recovery_hold が有効になり、外部処理は確認後に scope ごとに解除する",
    }
    (out / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (core.paths.root / "last_backup.json").write_text(json.dumps(
        {"backup_id": backup_id, "path": str(out), "created_at": manifest["created_at"], "full": full}), encoding="utf-8")
    warnings = ["backup は暗号化されていません。安全な保管先に置いてください"]
    if not full:
        warnings.append("軽量backupは cloud verified 原本を参照だけで保持します。保管先 account 喪失には耐えません")
    return {"path": str(out), "manifest": {k: v for k, v in manifest.items() if k != "objects"},
            "objects": len(entries), "warnings": warnings}


def verify(path: Path) -> dict:
    manifest_path = path / MANIFEST
    if not manifest_path.exists():
        raise TaxDeskError("BACKUP_INVALID", "manifest.json がありません", {"path": str(path)})
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    db = path / DB_NAME
    problems = []
    if not db.exists():
        problems.append("DBがありません")
    elif _sha256_file(db) != manifest["db_sha256"]:
        problems.append("DBの hash が manifest と一致しません")
    check = _check_db(db) if db.exists() else {}
    if check and (check["integrity"] != "ok" or check["foreign_key_violations"]):
        problems.append("DBの整合検査に失敗しました")
    if check and check.get("schema_version", 0) > latest_version():
        problems.append("このアプリより新しい schema のbackupです")
    missing = []
    for o in manifest["objects"]:
        if not o["included"]:
            continue
        f = path / "spool" / o["local_ref"]
        if not f.exists() or _sha256_file(f) != o["sha256"]:
            missing.append(o["local_ref"])
    if missing:
        problems.append(f"原本 {len(missing)} 件が欠落または hash 不一致です")
    return {"ok": not problems, "problems": problems, "manifest": {k: v for k, v in manifest.items() if k != "objects"},
            "db_check": check, "referenced_only_objects": sum(1 for o in manifest["objects"] if not o["included"])}


def preview(core: Core, path: Path) -> dict:
    result = verify(path)
    current = _check_db(core.paths.db) if core.db_exists() else None
    m = result["manifest"]
    lost = None
    if current and current["org_id"] == m["org_id"]:
        lost = {"audit_events_after_backup": max(0, current["audit_cutoff_seq"] - m["audit_cutoff_seq"]),
                "current_ledger_revision": current["ledger_revision"], "backup_ledger_revision": m["ledger_revision"]}
    return result | {"current": current, "changes_since_backup": lost,
                     "notice": "現在のDBへ merge せず置換します。backup後の変更は失われる可能性があります"}


def apply(core: Core, path: Path, *, actor: str) -> dict:
    check = verify(path)
    if not check["ok"]:
        raise TaxDeskError("BACKUP_INVALID", "backup の検証に失敗しました", {"problems": check["problems"]})
    manifest = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    lock = FileLock(core.paths.maintenance_lock)
    try:
        lock.acquire()
    except LockBusy:
        raise TaxDeskError("MAINTENANCE", "別の保守処理が実行中です") from None
    try:
        # DB置換前に新 restore_generation と recovery_hold を durable 保存する
        rc = runtime_control.begin_restore(core.paths.runtime_control, actor=actor, backup_id=manifest["backup_id"])
        core.close()
        aside = core.paths.root / "restore-aside" / utc_now().replace(":", "").replace("-", "").split(".")[0]
        aside.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(core.paths.db) + suffix)
            if p.exists():
                shutil.move(str(p), aside / p.name)
        shutil.copyfile(path / DB_NAME, core.paths.db)
        restored_objects = 0
        for o in manifest["objects"]:
            if not o["included"]:
                continue
            dst = core.paths.spool / o["local_ref"]
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path / "spool" / o["local_ref"], dst)
                restored_objects += 1
        post = _check_db(core.paths.db)
        if post["integrity"] != "ok" or post["foreign_key_violations"]:
            raise TaxDeskError("INTEGRITY_ERROR", "復元後の検査に失敗しました。退避した旧DBは残っています",
                               {"aside": str(aside)})
        held = _hold_external_work(core, actor, manifest["backup_id"], rc.restore_generation)
    finally:
        lock.release()
    return {"restored_backup_id": manifest["backup_id"], "restore_generation": rc.restore_generation,
            "recovery_hold": True, "held_scopes": rc.held_scopes, "aside": str(aside),
            "restored_objects": restored_objects, "held_items": held, "db_check": post,
            "next_steps": ["recovery status で保留中の外部処理を確認する",
                           "cloud upload は保管先の登録ID/hash/versionを照会してから recovery release upload",
                           "AI job は結果回収または outcome_unknown として扱い、必要なら利用者が再実行する",
                           "申告は公式の受付結果を確認するまで再送しない"]}


def _hold_external_work(core: Core, actor: str, backup_id: str, generation: str) -> dict:
    """旧 lease・capability を失効させ、外部処理jobを一律 queued に戻さず保留する。"""
    conn = connect(core.paths.db)
    try:
        now = utc_now()
        conn.execute("BEGIN IMMEDIATE")
        caps = conn.execute("UPDATE approval_capabilities SET revoked_at=? WHERE consumed_at IS NULL "
                            "AND revoked_at IS NULL", (now,)).rowcount
        outbox = conn.execute("UPDATE sync_outbox SET state='held', lease_token=NULL, lease_until=NULL, updated_at=? "
                              "WHERE state IN ('queued','running','retry_wait')", (now,)).rowcount
        agents = conn.execute("UPDATE agent_jobs SET status='outcome_unknown', error_code='RESTORED', finished_at=? "
                              "WHERE status IN ('queued','preparing','running','validating')", (now,)).rowcount
        ocr = conn.execute("UPDATE ocr_jobs SET state='held', updated_at=? WHERE state IN ('queued','running',"
                           "'preprocessing')", (now,)).rowcount
        uploading = conn.execute("UPDATE evidence_objects SET sync_state='uploaded_unverified' "
                                 "WHERE sync_state='uploading'").rowcount
        prev = conn.execute("SELECT hash FROM audit_events ORDER BY sequence_no DESC LIMIT 1").fetchone()
        from ..domain.values import canonical_json, sha256_hex
        event = {"id": new_id(), "org_id": conn.execute("SELECT id FROM org").fetchone()[0], "actor": actor,
                 "request_id": None, "command": "restore.apply", "job_id": None, "target_type": "backup",
                 "target_id": backup_id, "before_revision": None, "after_revision": None, "input_hash": None,
                 "result_hash": None, "reason": None,
                 "detail_json": canonical_json({"restore_generation": generation, "revoked_capabilities": caps,
                                                "held_outbox": outbox, "unknown_agent_jobs": agents,
                                                "held_ocr_jobs": ocr}),
                 "created_at": now}
        prev_hash = prev[0] if prev else "0" * 64
        conn.execute("INSERT INTO audit_events (id, org_id, actor, request_id, command, job_id, target_type, target_id, "
                     "before_revision, after_revision, input_hash, result_hash, reason, detail_json, prev_hash, hash, "
                     "created_at) VALUES (:id,:org_id,:actor,:request_id,:command,:job_id,:target_type,:target_id,"
                     ":before_revision,:after_revision,:input_hash,:result_hash,:reason,:detail_json,:prev_hash,:hash,"
                     ":created_at)", event | {"prev_hash": prev_hash, "hash": sha256_hex(prev_hash + canonical_json(event))})
        conn.execute("COMMIT")
        return {"revoked_capabilities": caps, "held_outbox": outbox, "unknown_agent_jobs": agents, "held_ocr_jobs": ocr,
                "uploaded_unverified": uploading}
    finally:
        conn.close()
