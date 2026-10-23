"""DB外の実行gate (16章7節)。

restore_generation と recovery_hold を利用者別 runtime_control.json に durable 保存する。
backup に含めないため、古いbackupの値で上書き復元されない。
壊れた記録は副作用処理を止める側に倒す。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.values import new_id, utc_now

EXTERNAL_SCOPES = ("upload", "ai", "sync", "etax", "notify")


@dataclass
class RuntimeControl:
    restore_generation: str
    recovery_hold: bool = False
    held_scopes: list[str] = field(default_factory=list)
    corrupt: bool = False
    updated_at: str = ""
    history: list[dict] = field(default_factory=list)

    def is_held(self, scope: str) -> bool:
        return self.corrupt or (self.recovery_hold and scope in self.held_scopes)

    def to_dict(self) -> dict:
        return {
            "restore_generation": self.restore_generation,
            "recovery_hold": self.recovery_hold,
            "held_scopes": self.held_scopes,
            "updated_at": self.updated_at,
            "history": self.history[-50:],
        }


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":  # Windowsではdirectory fsyncを行えない
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{new_id()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def load(path: Path) -> RuntimeControl:
    if not path.exists():
        rc = RuntimeControl(restore_generation=new_id(), updated_at=utc_now())
        durable_write_json(path, rc.to_dict())
        return rc
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return RuntimeControl(
            restore_generation=str(raw["restore_generation"]),
            recovery_hold=bool(raw.get("recovery_hold", False)),
            held_scopes=list(raw.get("held_scopes", [])),
            updated_at=str(raw.get("updated_at", "")),
            history=list(raw.get("history", [])),
        )
    except (ValueError, KeyError, TypeError):
        return RuntimeControl(restore_generation="corrupt", recovery_hold=True,
                              held_scopes=list(EXTERNAL_SCOPES), corrupt=True)


def begin_restore(path: Path, *, actor: str, backup_id: str) -> RuntimeControl:
    """DB置換前に新 restore_generation と recovery_hold を保存する。"""
    previous = load(path)
    rc = RuntimeControl(
        restore_generation=new_id(),
        recovery_hold=True,
        held_scopes=list(EXTERNAL_SCOPES),
        updated_at=utc_now(),
        history=previous.history + [{
            "event": "restore_started", "backup_id": backup_id, "actor": actor, "at": utc_now(),
            "previous_generation": previous.restore_generation,
        }],
    )
    durable_write_json(path, rc.to_dict())
    return rc


def release_scope(path: Path, scope: str, *, actor: str, reason: str) -> RuntimeControl:
    rc = load(path)
    if rc.corrupt:
        raise ValueError("runtime_control.json が壊れています。手動確認が必要です")
    rc.held_scopes = [s for s in rc.held_scopes if s != scope]
    if not rc.held_scopes:
        rc.recovery_hold = False
    rc.updated_at = utc_now()
    rc.history.append({"event": "scope_released", "scope": scope, "actor": actor, "reason": reason, "at": rc.updated_at})
    durable_write_json(path, rc.to_dict())
    return rc
