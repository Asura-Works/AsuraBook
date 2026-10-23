"""AsuraBook core: Application Service の入口 (02章4節)。

GUI・CLI・IPC はすべて Core.execute を通す。command transaction の順序:
認証主体を解決 → 同じidempotency結果を確認 → 入力とrevisionを検証 → BEGIN IMMEDIATE →
最新の締め/権限を再検証 → 業務変更 → 監査 → outbox/task → capability消費 → command結果保存 → COMMIT。
外部送信は commit 前に行わない。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..domain.errors import AsuraBookError, invalid
from ..domain.values import canonical_json, content_hash, new_id, sha256_hex, utc_now
from ..infrastructure import runtime_control
from ..infrastructure.db.connection import connect, read_snapshot, write_transaction
from ..infrastructure.db.migrate import ensure_compatible, migrate
from ..infrastructure.locks import FileLock, LockBusy
from ..infrastructure.paths import AppPaths
from .session import Session

log = logging.getLogger("asurabook.core")

PROTOCOL_VERSION = "1.0"
APP_VERSION = "0.1.0"


@dataclass
class CommandSpec:
    name: str
    handler: Callable[["CommandContext", dict], Any]
    min_profile: str
    mutating: bool
    capability_scope: str | None = None
    needs_db: bool = True
    gate: str | None = None  # recovery_hold の対象scope
    store_result: bool = True
    summary: str = ""


REGISTRY: dict[str, CommandSpec] = {}


def command(name: str, *, min_profile: str, mutating: bool, capability_scope: str | None = None,
            needs_db: bool = True, gate: str | None = None, store_result: bool = True,
            summary: str = "") -> Callable:
    def deco(fn: Callable) -> Callable:
        if name in REGISTRY:
            raise RuntimeError(f"duplicate command {name}")
        doc_lines = (fn.__doc__ or "").strip().splitlines()
        REGISTRY[name] = CommandSpec(name, fn, min_profile, mutating, capability_scope, needs_db, gate,
                                     store_result, summary or (doc_lines[0] if doc_lines else ""))
        return fn
    return deco


@dataclass
class CommandContext:
    core: "Core"
    conn: sqlite3.Connection | None
    session: Session
    command: str
    request_id: str
    dry_run: bool
    expected_revision: int | None
    capability: dict | None = None
    capability_checked: bool = False
    warnings: list[dict] = field(default_factory=list)
    now: str = field(default_factory=utc_now)

    @property
    def paths(self) -> AppPaths:
        return self.core.paths

    @property
    def actor(self) -> str:
        return self.session.principal

    @property
    def db(self) -> sqlite3.Connection:
        if self.conn is None:
            raise AsuraBookError("NOT_INITIALIZED", "AsuraBook のDBが初期化されていません。asurabook init を実行してください")
        return self.conn

    def warn(self, code: str, message: str) -> None:
        self.warnings.append({"code": code, "message": message})

    @property
    def org_id(self) -> str:
        row = self.db.execute("SELECT id FROM org").fetchone()
        if row is None:
            raise AsuraBookError("NOT_INITIALIZED", "事業者が未設定です")
        return row["id"]

    def ledger_revision(self) -> int:
        return int(self.db.execute("SELECT ledger_revision FROM org").fetchone()[0])

    def bump_ledger_revision(self) -> int:
        self.db.execute("UPDATE org SET ledger_revision = ledger_revision + 1")
        return self.ledger_revision()

    def audit(self, *, target_type: str | None = None, target_id: str | None = None,
              before_revision: int | None = None, after_revision: int | None = None,
              input_value: Any = None, result_value: Any = None, reason: str | None = None,
              detail: Any = None, job_id: str | None = None, command: str | None = None) -> int:
        """append-only の監査イベントを書き、sequence_no を返す。hash chain は改変検知の補助。"""
        prev = self.db.execute("SELECT hash FROM audit_events ORDER BY sequence_no DESC LIMIT 1").fetchone()
        prev_hash = prev["hash"] if prev else "0" * 64
        event = {
            "id": new_id(),
            "org_id": self._org_id_or_none(),
            "actor": self.actor,
            "request_id": self.request_id,
            "command": command or self.command,
            "job_id": job_id,
            "target_type": target_type,
            "target_id": target_id,
            "before_revision": before_revision,
            "after_revision": after_revision,
            "input_hash": None if input_value is None else content_hash(input_value),
            "result_hash": None if result_value is None else content_hash(result_value),
            "reason": reason,
            "detail_json": None if detail is None else canonical_json(detail),
            "created_at": self.now,
        }
        digest = sha256_hex(prev_hash + canonical_json(event))
        cur = self.db.execute(
            """INSERT INTO audit_events (id, org_id, actor, request_id, command, job_id, target_type, target_id,
                   before_revision, after_revision, input_hash, result_hash, reason, detail_json, prev_hash, hash,
                   created_at)
               VALUES (:id, :org_id, :actor, :request_id, :command, :job_id, :target_type, :target_id,
                   :before_revision, :after_revision, :input_hash, :result_hash, :reason, :detail_json,
                   :prev_hash, :hash, :created_at)""",
            {**event, "prev_hash": prev_hash, "hash": digest},
        )
        return int(cur.lastrowid)

    def _org_id_or_none(self) -> str | None:
        row = self.db.execute("SELECT id FROM org").fetchone()
        return row["id"] if row else None

    def capability_target(self, target_id: str) -> dict:
        """capability で許可された対象を返す。対象外なら拒否する。"""
        if self.capability is None:
            raise AsuraBookError("INTERNAL_ERROR", "capability がありません")
        for t in json.loads(self.capability["targets_json"]):
            if t.get("id") == target_id:
                self.capability_checked = True
                return t
        raise AsuraBookError("CAPABILITY_INVALID", "承認された対象に含まれていません", {"target_id": target_id})

    def require_expected_revision(self, actual: int, target: str) -> None:
        if self.expected_revision is not None and self.expected_revision != actual:
            raise AsuraBookError("REVISION_CONFLICT", f"{target} の revision が変わっています",
                               {"expected_revision": self.expected_revision, "actual_revision": actual})


class Core:
    """1 data_dir (=1事業者) の core。writer connection はこの object だけが持つ。"""

    def __init__(self, data_dir: Path | str):
        self.paths = AppPaths(Path(data_dir))
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------ connection

    def db_exists(self) -> bool:
        return self.paths.db.exists()

    def writer(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = connect(self.paths.db)
            ensure_compatible(self._conn)
        return self._conn

    def create_database(self) -> sqlite3.Connection:
        self.paths.ensure()
        conn = connect(self.paths.db)
        migrate(conn)
        self._conn = conn
        return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _dry_run_connection(self) -> sqlite3.Connection:
        """業務DBの read-only snapshot を memory へ写し、同じ処理を実行して捨てる。"""
        src = connect(self.paths.db, readonly=True)
        try:
            mem = connect(":memory:")
            src.backup(mem)
        finally:
            src.close()
        mem.execute("PRAGMA foreign_keys=ON")
        return mem

    def runtime(self) -> runtime_control.RuntimeControl:
        return runtime_control.load(self.paths.runtime_control)

    # ------------------------------------------------------------ execute

    def execute(self, name: str, payload: dict | None, session: Session, *, request_id: str | None = None,
                idempotency_key: str | None = None, expected_revision: int | None = None,
                dry_run: bool = False, capability_token: str | None = None) -> dict:
        request_id = request_id or new_id()
        payload = payload or {}
        with self._lock:
            ctx: CommandContext | None = None
            try:
                spec = REGISTRY.get(name)
                if spec is None:
                    raise AsuraBookError("UNKNOWN_COMMAND", f"未知のcommandです: {name}")
                if not session.allows("reader"):
                    raise AsuraBookError("PERMISSION_DENIED", "権限がありません")
                if not isinstance(payload, dict):
                    raise invalid("payload は JSON object です")
                self._check_maintenance(spec)
                if not spec.needs_db:
                    ctx = CommandContext(self, None, session, name, request_id, dry_run, expected_revision)
                    self._authorize_without_db(spec, session)
                    data = spec.handler(ctx, payload)
                    return self._envelope(request_id, True, data, ctx.warnings, None, None)
                if not self.db_exists():
                    raise AsuraBookError("NOT_INITIALIZED", "AsuraBook のDBが初期化されていません。asurabook init を実行してください")
                conn = self._dry_run_connection() if dry_run else self.writer()
                try:
                    ctx = CommandContext(self, conn, session, name, request_id, dry_run, expected_revision)
                    if dry_run:
                        ctx.warn("DRY_RUN", "dry-run: 業務DB・原本・outbox・command結果は変更していません")
                    if spec.mutating:
                        return self._execute_mutating(spec, ctx, payload, idempotency_key, capability_token)
                    self._authorize_without_db(spec, session)
                    with read_snapshot(conn):
                        data = spec.handler(ctx, payload)
                        observed = ctx.ledger_revision() if self._has_org(conn) else None
                    return self._envelope(request_id, True, data, ctx.warnings, None, observed)
                finally:
                    if dry_run:
                        conn.close()
            except AsuraBookError as exc:
                return self._envelope(request_id, False, None, ctx.warnings if ctx else [], exc, None)
            except sqlite3.IntegrityError as exc:
                return self._envelope(request_id, False, None, ctx.warnings if ctx else [],
                                      _map_integrity(exc), None)
            except Exception as exc:  # 内部stack traceを利用者応答へ出さない
                log.exception("internal error in %s", name)
                return self._envelope(request_id, False, None, ctx.warnings if ctx else [],
                                      AsuraBookError("INTERNAL_ERROR", "内部エラーが発生しました",
                                                   {"type": type(exc).__name__}), None)

    def _has_org(self, conn: sqlite3.Connection) -> bool:
        try:
            return conn.execute("SELECT 1 FROM org").fetchone() is not None
        except sqlite3.OperationalError:
            return False

    def _check_maintenance(self, spec: CommandSpec) -> None:
        if spec.name.startswith("restore.") or not self.paths.root.exists():
            return
        if FileLock(self.paths.maintenance_lock).is_locked_by_other():
            raise AsuraBookError("MAINTENANCE", "復元・移行の保守中です。完了まで待ってください")

    def _authorize_without_db(self, spec: CommandSpec, session: Session) -> None:
        if not session.allows(spec.min_profile):
            raise AsuraBookError("PERMISSION_DENIED", f"{spec.name} には {spec.min_profile} 権限が必要です",
                               {"required": spec.min_profile, "current": session.profile})

    def _execute_mutating(self, spec: CommandSpec, ctx: CommandContext, payload: dict,
                          idempotency_key: str | None, capability_token: str | None) -> dict:
        conn = ctx.db
        if idempotency_key is not None and not spec.store_result:
            raise invalid(f"{spec.name} は idempotency key で結果を保存しません")
        payload_hash = content_hash({"payload": payload, "expected_revision": ctx.expected_revision})
        actor_scope = ctx.session.principal
        org_id = ctx.org_id if self._has_org(conn) else "-"

        replay = self._lookup_result(conn, org_id, actor_scope, spec.name, idempotency_key, payload_hash)
        if replay is not None:
            return replay

        with write_transaction(conn):
            replay = self._lookup_result(conn, org_id, actor_scope, spec.name, idempotency_key, payload_hash)
            if replay is not None:
                return replay
            if spec.gate and self.runtime().is_held(spec.gate):
                raise AsuraBookError("RECOVERY_HOLD", f"復元後の確認待ちのため {spec.gate} 処理を保留しています",
                                   {"scope": spec.gate})
            if not ctx.session.allows(spec.min_profile):
                if capability_token and spec.capability_scope:
                    ctx.capability = self._verify_capability(conn, capability_token, ctx.session,
                                                             spec.capability_scope)
                else:
                    raise AsuraBookError("PERMISSION_DENIED", f"{spec.name} には {spec.min_profile} 権限または"
                                       " GUIで承認された capability が必要です",
                                       {"required": spec.min_profile, "current": ctx.session.profile})
            data = spec.handler(ctx, payload)
            if ctx.capability is not None:
                if not ctx.capability_checked:
                    raise AsuraBookError("INTERNAL_ERROR", "capability の対象検証が行われていません")
                cur = conn.execute(
                    "UPDATE approval_capabilities SET consumed_request_id=?, consumed_at=? "
                    "WHERE id=? AND consumed_at IS NULL",
                    (ctx.request_id, ctx.now, ctx.capability["id"]),
                )
                if cur.rowcount != 1:
                    raise AsuraBookError("CAPABILITY_INVALID", "capability は使用済みです")
            observed = ctx.ledger_revision() if self._has_org(conn) else None
            response = self._envelope(ctx.request_id, True, data, ctx.warnings, None, observed)
            if idempotency_key is not None:
                conn.execute(
                    "INSERT INTO command_results (org_id, actor_scope, command, idempotency_key, payload_hash, "
                    "request_id, response_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (ctx.org_id, actor_scope, spec.name, idempotency_key, payload_hash, ctx.request_id,
                     canonical_json(response), ctx.now),
                )
        return response

    def _lookup_result(self, conn: sqlite3.Connection, org_id: str, actor_scope: str, command_name: str,
                       key: str | None, payload_hash: str) -> dict | None:
        if key is None:
            return None
        row = conn.execute(
            "SELECT payload_hash, response_json FROM command_results "
            "WHERE org_id=? AND actor_scope=? AND command=? AND idempotency_key=?",
            (org_id, actor_scope, command_name, key),
        ).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != payload_hash:
            raise AsuraBookError("IDEMPOTENCY_CONFLICT", "同じ idempotency key で異なる内容が送られました",
                               {"idempotency_key": key})
        stored = json.loads(row["response_json"])
        stored["warnings"] = list(stored.get("warnings", [])) + [
            {"code": "IDEMPOTENT_REPLAY", "message": "保存済みの結果を返しました。再実行はしていません"}]
        return stored

    def _verify_capability(self, conn: sqlite3.Connection, token: str, session: Session, scope: str) -> dict:
        row = conn.execute("SELECT * FROM approval_capabilities WHERE token_hash=?",
                           (sha256_hex(token),)).fetchone()
        if row is None:
            raise AsuraBookError("CAPABILITY_INVALID", "capability が無効です")
        cap = dict(row)
        if cap["principal"] != session.principal:
            raise AsuraBookError("CAPABILITY_INVALID", "capability は別の主体に発行されています")
        if cap["scope"] != scope:
            raise AsuraBookError("CAPABILITY_INVALID", "capability の操作範囲が一致しません",
                               {"capability_scope": cap["scope"], "required": scope})
        if cap["consumed_at"] is not None:
            raise AsuraBookError("CAPABILITY_INVALID", "capability は使用済みです")
        if cap["revoked_at"] is not None:
            raise AsuraBookError("CAPABILITY_INVALID", "capability は取り消されています")
        if cap["expires_at"] <= utc_now():
            raise AsuraBookError("CAPABILITY_INVALID", "capability の期限が切れています")
        if cap["restore_generation"] != self.runtime().restore_generation:
            raise AsuraBookError("CAPABILITY_INVALID", "復元により capability は失効しています")
        return cap

    @staticmethod
    def _envelope(request_id: str, ok: bool, data: Any, warnings: list[dict], error: AsuraBookError | None,
                  observed_revision: int | None) -> dict:
        if data is not None and not isinstance(data, (dict, list)):
            data = {"value": data}
        return {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "ok": ok,
            "data": data if ok else None,
            "warnings": warnings,
            "error": None if error is None else error.to_dict(),
            "retryable": False if error is None else error.retryable,
            "observed_revision": observed_revision,
        }


def _map_integrity(exc: sqlite3.IntegrityError) -> AsuraBookError:
    message = str(exc)
    for code in ("POSTED_IMMUTABLE", "INVALID_TRANSITION", "APPEND_ONLY"):
        if message.startswith(code):
            return AsuraBookError(code, message.split(": ", 1)[-1])
    return AsuraBookError("INTEGRITY_ERROR", "DB整合制約に違反しました", {"constraint": message})


def exit_code_for(response: dict) -> int:
    if response["ok"]:
        return 0
    return AsuraBookError(response["error"]["code"], response["error"]["message"]).exit_code
