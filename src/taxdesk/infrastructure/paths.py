"""利用者データの配置 (16章2節)。

DB・spool・job・export・runtime control を分ける。既定は LOCALAPPDATA/TaxDesk/orgs/<org>。
DB正本を NAS/同期フォルダへ置かない。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def default_data_dir() -> Path:
    env = os.environ.get("TAXDESK_HOME")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "TaxDesk" / "orgs" / "default"


# 同期フォルダらしいpathの検出。正本DBを置かないための警告用。
_SYNC_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "box sync")


def looks_like_sync_folder(path: Path) -> bool:
    lowered = str(path).lower()
    return any(m in lowered for m in _SYNC_MARKERS) or lowered.startswith("\\\\")


@dataclass(frozen=True)
class AppPaths:
    root: Path

    @property
    def db(self) -> Path:
        return self.root / "taxdesk.db"

    @property
    def spool(self) -> Path:
        return self.root / "spool"

    @property
    def jobs(self) -> Path:
        return self.root / "jobs"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    @property
    def cloud_local(self) -> Path:
        return self.root / "cloud-local"

    @property
    def runtime_control(self) -> Path:
        return self.root / "runtime_control.json"

    @property
    def maintenance_lock(self) -> Path:
        return self.root / "maintenance.lock"

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    def ensure(self) -> None:
        for p in (self.root, self.spool, self.jobs, self.exports):
            p.mkdir(parents=True, exist_ok=True)

    def protected_paths(self) -> list[Path]:
        """export先に指定させない管理領域。"""
        return [
            self.db, Path(str(self.db) + "-wal"), Path(str(self.db) + "-shm"),
            self.spool, self.jobs, self.runtime_control, self.maintenance_lock, self.config,
            self.cloud_local,
        ]
