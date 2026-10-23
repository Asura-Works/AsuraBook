"""利用者設定 (config.json)。DB外に置き、owner だけが変更する。"""

from __future__ import annotations

import json
from typing import Any

from ..domain.errors import invalid
from ..infrastructure.paths import AppPaths
from ..infrastructure.runtime_control import durable_write_json

DEFAULTS: dict[str, Any] = {
    "ai": {
        "provider": "claude_cli",
        "exe": None,
        "auth_mode": "subscription",
        "allowed_auth_methods": ["claude.ai", "oauth_token"],
        "model": None,
        "timeout_seconds": 180,
        "auto_read_receipts": True,
        # Codex は利用者が設定したときだけ使う代替 (実CLIでは未検証)
        "codex_exe": None,
        "codex_model": None,
    },
    # cloud.provider: null (未設定) | "local_dir" (開発・検証用。クラウドではない)
    "cloud": {"provider": None, "root": None},
}

_ALLOWED = {
    "ai.exe", "ai.auth_mode", "ai.allowed_auth_methods", "ai.model", "ai.timeout_seconds", "ai.auto_read_receipts",
    "ai.codex_exe", "ai.codex_model",
    "cloud.provider", "cloud.root",
}


def load(paths: AppPaths) -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULTS))
    if paths.config.exists():
        stored = json.loads(paths.config.read_text(encoding="utf-8"))
        for section, values in stored.items():
            if isinstance(values, dict) and section in cfg:
                cfg[section].update(values)
    return cfg


def set_value(paths: AppPaths, key: str, value: Any) -> dict[str, Any]:
    if key not in _ALLOWED:
        raise invalid("変更できない設定です", key=key, allowed=sorted(_ALLOWED))
    if key == "ai.auth_mode" and value not in ("subscription", "api_key"):
        raise invalid("ai.auth_mode は subscription/api_key です")
    if key == "cloud.provider" and value not in (None, "local_dir"):
        raise invalid("cloud.provider は未設定 (null) または local_dir です。S3等のgatewayは未実装です")
    stored = json.loads(paths.config.read_text(encoding="utf-8")) if paths.config.exists() else {}
    section, name = key.split(".", 1)
    stored.setdefault(section, {})[name] = value
    durable_write_json(paths.config, stored)
    return load(paths)
