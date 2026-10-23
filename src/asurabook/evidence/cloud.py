"""証憑の保管先 (09章)。

正式構成は HTTPS 証憑gateway → 非公開 object storage だが、提供先・region・費用が未確定のため未実装。
ここでは同じ契約 (put/head、version、SHA-256 照合) を持つ StorageProvider と、
開発・検証用の LocalDirStorage を提供する。LocalDirStorage はクラウド保管ではない。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.values import new_id, utc_now


@dataclass(frozen=True)
class RemoteObject:
    key: str
    version: str
    sha256: str
    size_bytes: int


class StorageProvider(Protocol):
    name: str

    def head(self, key: str) -> list[RemoteObject]: ...

    def put(self, key: str, data: bytes, sha256: str) -> RemoteObject: ...


class LocalDirStorage:
    """version付きの擬似object storage。同じkeyへの再putは新versionで、上書きしない。"""

    name = "local_dir"

    def __init__(self, root: Path):
        self.root = root

    def _dir(self, key: str) -> Path:
        safe = key.replace("/", "_")
        return self.root / safe

    def head(self, key: str) -> list[RemoteObject]:
        d = self._dir(key)
        if not d.exists():
            return []
        out = []
        for meta in sorted(d.glob("*.json")):
            m = json.loads(meta.read_text(encoding="utf-8"))
            out.append(RemoteObject(key, m["version"], m["sha256"], m["size_bytes"]))
        return out

    def put(self, key: str, data: bytes, sha256: str) -> RemoteObject:
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError("checksum mismatch")
        d = self._dir(key)
        d.mkdir(parents=True, exist_ok=True)
        version = f"{utc_now().replace(':', '').replace('-', '')}-{new_id()[:8]}"
        blob = d / f"{version}.bin"
        with open(blob, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        stored = blob.read_bytes()
        actual = hashlib.sha256(stored).hexdigest()
        (d / f"{version}.json").write_text(json.dumps({"version": version, "sha256": actual,
                                                       "size_bytes": len(stored)}), encoding="utf-8")
        return RemoteObject(key, version, actual, len(stored))


def provider_from_config(cfg: dict, default_root: Path) -> StorageProvider | None:
    cloud = cfg.get("cloud") or {}
    if cloud.get("provider") == "local_dir":
        return LocalDirStorage(Path(cloud.get("root") or default_root))
    return None
