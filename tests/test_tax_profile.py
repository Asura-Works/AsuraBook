"""税務profile (11章1節) と migration の試験。"""

from __future__ import annotations

from conftest import ASSISTANT, Client
from asurabook.application.core import Core
from asurabook.application.session import Session
from asurabook.infrastructure.db.connection import connect
from asurabook.infrastructure.db.migrate import current_version, latest_version, migrate


def test_profile_versions_unknowns_and_no_retroactive_change(client: Client, core):
    shown = client.ok("tax.profile_show", {"year": 2026})
    assert shown["current"] is None and len(shown["unknown_fields"]) >= 5
    v1 = client.ok("tax.profile_set", {"year": 2026, "profile": {"filing_type": "blue"}, "confirm": True})
    assert v1["version"] == 1 and v1["status"] == "unconfirmed"
    full = {"filing_type": "blue", "blue_approval": "approved", "bookkeeping": "double_entry",
            "income_category": "business", "consumption_tax": {"status": "exempt", "method": "not_applicable"},
            "invoice": {"registered": "no"}, "accounting_method": "tax_inclusive",
            "other_income": {"salary": "no", "pension": "no", "other": "no"}}
    v2 = client.ok("tax.profile_set", {"year": 2026, "profile": full, "confirm": True})
    assert v2["version"] == 2 and v2["status"] == "confirmed" and v2["unknown_fields"] == []
    # 翌年へ複写しても 2026 の版は変わらない
    v27 = client.ok("tax.profile_set", {"year": 2027, "copy_from_year": 2026,
                                        "profile": {"consumption_tax": {"status": "unknown", "method": "unknown"}}})
    assert v27["status"] == "unconfirmed"
    assert client.ok("tax.profile_show", {"year": 2026})["current"]["status"] == "confirmed"
    assert len(client.ok("tax.profile_show", {"year": 2026})["history"]) == 1
    err = client.err("tax.profile_set", {"year": 2026, "profile": {"invoice": {"registered": "yes"},
                                                                   "consumption_tax": {"status": "exempt"}}})
    assert err["code"] == "INVALID_INPUT"
    err = client.err("tax.profile_set", {"year": 2026, "profile": {"invoice": {"registration_number": "T12"}}})
    assert err["code"] == "INVALID_INPUT"
    assert client.err("tax.profile_set", {"year": 2026, "profile": {}}, session=ASSISTANT)["code"] == \
        "PERMISSION_DENIED"


def test_old_schema_is_backed_up_then_migrated(tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    conn = connect(data / "asurabook.db")
    migrate(conn, upto=1)
    assert current_version(conn) == 1
    conn.close()
    core = Core(data)
    owner = Session("t:o", "owner", "gui")
    resp = core.execute("journal.list", {}, owner)
    assert resp["ok"]
    assert current_version(core.writer()) == latest_version()
    backups = list((data / "pre-migration").glob("schema-v1-*.db"))
    assert len(backups) == 1
    assert current_version(connect(backups[0], readonly=True)) == 1
    core.close()
