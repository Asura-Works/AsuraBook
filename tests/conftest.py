from __future__ import annotations

from pathlib import Path

import pytest

import asurabook.application.commands  # noqa: F401  command登録
from asurabook.application.core import Core
from asurabook.application.session import Session

OWNER = Session(principal="test:owner", profile="owner", entry_point="gui")
ASSISTANT = Session(principal="test:cli", profile="assistant", entry_point="cli")
READER = Session(principal="test:reader", profile="reader", entry_point="cli")


class Client:
    """テスト用: core.execute を呼び、失敗時は応答全体を表示する。"""

    def __init__(self, core: Core):
        self.core = core

    def raw(self, name: str, payload: dict | None = None, session: Session = OWNER, **kw) -> dict:
        return self.core.execute(name, payload or {}, session, **kw)

    def ok(self, name: str, payload: dict | None = None, session: Session = OWNER, **kw):
        resp = self.raw(name, payload, session, **kw)
        assert resp["ok"], resp
        return resp["data"]

    def err(self, name: str, payload: dict | None = None, session: Session = OWNER, **kw) -> dict:
        resp = self.raw(name, payload, session, **kw)
        assert not resp["ok"], resp
        return resp["error"]

    def post(self, journal_date: str, lines: list[tuple], description: str = "", **extra) -> dict:
        """(account, side, amount) のタプル列から下書き→検証→確定まで行う。"""
        payload = {"journal_date": journal_date, "description": description,
                   "lines": [_line(*l) for l in lines], **extra}
        draft = self.ok("journal.draft", payload)
        self.ok("journal.validate", {"id": draft["id"]})
        return self.ok("journal.post", {"id": draft["id"]})["posted"][0]


def _line(account: str, side: str, amount: int, extra: dict | None = None) -> dict:
    d = {"account": account, "side": side, "amount_jpy": amount}
    d.update(extra or {})
    return d


@pytest.fixture
def core(tmp_path: Path) -> Core:
    c = Core(tmp_path / "data")
    resp = c.execute("org.init", {"display_name": "試験事業者", "years": [2026, 2027]}, OWNER)
    assert resp["ok"], resp
    yield c
    c.close()


@pytest.fixture
def client(core: Core) -> Client:
    return Client(core)
