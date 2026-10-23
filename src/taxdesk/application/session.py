"""認証主体と権限profile (15章7節)。

通常CLIの無token接続は assistant。同一OS利用者であることは owner の証明ではない。
owner は製品GUI entry point の session だけが持つ。
"""

from __future__ import annotations

import getpass
from dataclasses import dataclass, field

from ..domain.values import new_id

PROFILES = ("reader", "assistant", "bookkeeper_scoped", "owner")
_RANK = {name: i for i, name in enumerate(PROFILES)}


def rank(profile: str) -> int:
    return _RANK[profile]


def local_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - 環境依存
        return "unknown"


@dataclass(frozen=True)
class Session:
    """接続ごとの認証結果。principal は永続主体で、session_id/PID を含めない。"""

    principal: str
    profile: str
    entry_point: str
    session_id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        if self.profile not in _RANK:
            raise ValueError(f"unknown profile {self.profile}")

    def allows(self, required: str) -> bool:
        return rank(self.profile) >= rank(required)


def cli_session(*, reader: bool = False) -> Session:
    """公開CLIの session。明示 reader は assistant へ引き上げない。"""
    user = local_user()
    if reader:
        return Session(principal=f"local:{user}:cli-reader", profile="reader", entry_point="cli")
    return Session(principal=f"local:{user}:cli", profile="assistant", entry_point="cli")


def gui_owner_session() -> Session:
    """製品GUI entry point からだけ作る owner session。"""
    user = local_user()
    return Session(principal=f"local:{user}:owner", profile="owner", entry_point="gui")
