"""保全・復旧・診断 command (16章)。"""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

from ...domain.errors import AsuraBookError, invalid
from ...infrastructure import runtime_control
from ...infrastructure.db.connection import check_runtime
from ...infrastructure.db.migrate import current_version, latest_version
from ...infrastructure.paths import looks_like_sync_folder
from .. import backup as backup_ops
from .. import config as app_config
from ..core import APP_VERSION, PROTOCOL_VERSION, REGISTRY, CommandContext, command


@command("backup.create", min_profile="assistant", mutating=False)
def backup_create(ctx: CommandContext, payload: dict) -> dict:
    """一貫snapshotのbackupを作る。未同期原本は常に含める。"""
    dest = payload.get("dest")
    if not dest:
        raise invalid("dest (保存先フォルダ) は必須です")
    if ctx.dry_run:
        return {"would_create_in": str(Path(dest).resolve()), "full": payload.get("full", True)}
    result = backup_ops.create(ctx.core, Path(dest), full=bool(payload.get("full", True)))
    for w in result.pop("warnings"):
        ctx.warn("BACKUP_NOTICE", w)
    return result


@command("backup.verify", min_profile="reader", mutating=False, needs_db=False)
def backup_verify(ctx: CommandContext, payload: dict) -> dict:
    """backupの manifest・hash・整合を検査する。"""
    return backup_ops.verify(Path(payload.get("path") or ""))


@command("restore.preview", min_profile="reader", mutating=False, needs_db=False)
def restore_preview(ctx: CommandContext, payload: dict) -> dict:
    """復元した場合の影響を表示する (現在DBは変更しない)。"""
    return backup_ops.preview(ctx.core, Path(payload.get("path") or ""))


@command("restore.apply", min_profile="owner", mutating=False, needs_db=False)
def restore_apply(ctx: CommandContext, payload: dict) -> dict:
    """backupでDBを置換する。recovery_hold を先に保存し、外部処理を保留する。"""
    if payload.get("confirm") is not True:
        raise invalid("confirm=true が必要です (現在のDBは退避され、backup後の変更は失われる可能性があります)")
    if ctx.dry_run:
        return backup_ops.preview(ctx.core, Path(payload.get("path") or ""))
    return backup_ops.apply(ctx.core, Path(payload.get("path") or ""), actor=ctx.actor)


@command("recovery.status", min_profile="reader", mutating=False)
def recovery_status(ctx: CommandContext, payload: dict) -> dict:
    """復元gateと保留中の外部処理。"""
    rc = ctx.core.runtime()
    conn = ctx.db
    return {
        "restore_generation": rc.restore_generation, "recovery_hold": rc.recovery_hold,
        "held_scopes": rc.held_scopes, "corrupt": rc.corrupt, "history": rc.history[-10:],
        "held": {
            "sync_outbox": conn.execute("SELECT COUNT(*) FROM sync_outbox WHERE state='held'").fetchone()[0],
            "ocr_jobs": conn.execute("SELECT COUNT(*) FROM ocr_jobs WHERE state='held'").fetchone()[0],
            "agent_jobs_outcome_unknown": conn.execute(
                "SELECT COUNT(*) FROM agent_jobs WHERE status='outcome_unknown'").fetchone()[0],
            "uploaded_unverified": conn.execute(
                "SELECT COUNT(*) FROM evidence_objects WHERE sync_state='uploaded_unverified'").fetchone()[0],
        },
    }


@command("recovery.release", min_profile="owner", mutating=True)
def recovery_release(ctx: CommandContext, payload: dict) -> dict:
    """確認済みの scope だけ保留を解除する。個別の成否不明jobは保留のまま。"""
    scope = payload.get("scope")
    if scope not in runtime_control.EXTERNAL_SCOPES:
        raise invalid("scope が不正です", allowed=runtime_control.EXTERNAL_SCOPES)
    if not payload.get("reason"):
        raise invalid("確認内容 (reason) は必須です")
    requeued = 0
    if scope == "upload":
        # worker は put の前に必ず保管先を照会するため、再開しても同じ原本を盲目的に再送しない
        requeued = ctx.db.execute("UPDATE sync_outbox SET state='queued', updated_at=? WHERE state='held'",
                                  (ctx.now,)).rowcount
    if scope == "ai":
        ctx.db.execute("UPDATE ocr_jobs SET state='outcome_unknown', updated_at=? WHERE state='held'", (ctx.now,))
    try:
        rc = runtime_control.release_scope(ctx.paths.runtime_control, scope, actor=ctx.actor, reason=payload["reason"])
    except ValueError as exc:
        raise AsuraBookError("RECOVERY_GATE_CORRUPT", str(exc)) from None
    ctx.audit(target_type="recovery", target_id=scope, reason=payload["reason"], detail={"requeued": requeued})
    return {"released": scope, "remaining_scopes": rc.held_scopes, "recovery_hold": rc.recovery_hold,
            "requeued_uploads": requeued}


@command("doctor", min_profile="reader", mutating=False, needs_db=False)
def doctor(ctx: CommandContext, payload: dict) -> dict:
    """runtime・DB・配置・復元gateの診断。"""
    paths = ctx.paths
    rt = check_runtime()
    info = {
        "app_version": APP_VERSION, "protocol_version": PROTOCOL_VERSION,
        "python": sys.version.split()[0], "platform": platform.platform(),
        "sqlite": rt, "data_dir": str(paths.root), "db_exists": ctx.core.db_exists(),
        "sync_folder_warning": looks_like_sync_folder(paths.root),
        "windows_validated": False,
    }
    if sys.version_info[:2] != (3, 13):
        info["python_warning"] = "設計の対象は CPython 3.13 です"
    if ctx.core.db_exists():
        conn = ctx.core.writer()
        info["schema_version"] = current_version(conn)
        info["schema_latest"] = latest_version()
        info["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
        refs = {r[0] for r in conn.execute("SELECT local_ref FROM evidence_objects UNION SELECT local_ref FROM source_files")}
        orphans = []
        for kind in ("evidence", "sources"):
            base = paths.spool / kind
            if base.exists():
                for f in base.rglob("*"):
                    if f.is_file() and not f.name.startswith(".") and f.relative_to(paths.spool).as_posix() not in refs:
                        orphans.append(f.relative_to(paths.spool).as_posix())
        info["spool_orphans"] = orphans[:50]
        info["runtime_control"] = ctx.core.runtime().to_dict()
        last = paths.root / "last_backup.json"
        info["last_backup"] = json.loads(last.read_text(encoding="utf-8")) if last.exists() else None
    info["config"] = app_config.load(paths)
    return info


@command("settings.show", min_profile="reader", mutating=False, needs_db=False)
def settings_show(ctx: CommandContext, payload: dict) -> dict:
    """利用者設定 (AI・cloud)。"""
    return app_config.load(ctx.paths)


@command("settings.set", min_profile="owner", mutating=False, needs_db=False)
def settings_set(ctx: CommandContext, payload: dict) -> dict:
    """設定を変更する (owner)。"""
    return app_config.set_value(ctx.paths, payload.get("key", ""), payload.get("value"))


@command("capabilities", min_profile="reader", mutating=False, needs_db=False)
def capabilities(ctx: CommandContext, payload: dict) -> list:
    """利用できる command と必要な権限。"""
    return [{"command": s.name, "min_profile": s.min_profile, "mutating": s.mutating,
             "capability_scope": s.capability_scope, "summary": s.summary,
             "allowed_for_session": ctx.session.allows(s.min_profile) or bool(s.capability_scope)}
            for s in sorted(REGISTRY.values(), key=lambda x: x.name) if not s.name.startswith("worker.")]
