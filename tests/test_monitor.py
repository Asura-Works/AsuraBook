"""申告モニタリングの決定的検査 (21章9節) の試験。"""

from __future__ import annotations

from pathlib import Path

from conftest import ASSISTANT, Client

FIXTURES = Path(__file__).parent / "fixtures"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x02" * 64


def codes(result: dict) -> set[str]:
    return {f["code"] for f in result["findings"]}


def test_checks_detect_gaps_and_resolve_after_fix(client: Client, tmp_path: Path):
    client.ok("source_accounts.create", {"code": "BANK-DEMO", "provider": "demo", "kind": "bank",
                                         "display_name": "架空銀行", "account": "110"})
    client.ok("import.stage", {"path": str(FIXTURES / "generic_bank_v1.csv"), "profile": "generic-v1",
                               "account": "BANK-DEMO"})
    expense = client.post("2026-03-10", [("519", "debit", 3300), ("100", "credit", 3300)], "文具")
    client.ok("journal.draft", {"journal_date": "2026-03-11", "lines": [
        {"account": "519", "side": "debit", "amount_jpy": 1}, {"account": "100", "side": "credit", "amount_jpy": 1}]})
    result = client.ok("monitor.check", {"year": 2026}, session=ASSISTANT)
    found = codes(result)
    assert {"COVERAGE_GAP", "UNPOSTED_ENTRIES", "UNLINKED_SOURCE_EVENTS", "NEGATIVE_CASH",
            "EXPENSE_WITHOUT_EVIDENCE", "TAX_PROFILE_MISSING"} <= found
    gap = next(f for f in result["findings"] if f["code"] == "COVERAGE_GAP")
    assert "2026-01" in gap["evidence"]["months"] and "2026-10" not in gap["evidence"]["months"]
    assert result["ready_for_filing"] is False
    states = {s["step"]: s["state"] for s in result["readiness"]}
    assert states["申告書"] == "未対応" and states["証憑"] == "要確認"

    # 既読は解消ではない
    target = next(f for f in result["findings"] if f["code"] == "EXPENSE_WITHOUT_EVIDENCE")
    client.ok("monitor.ack", {"id": target["id"]})
    again = client.ok("monitor.check", {"year": 2026})
    still = next(f for f in again["findings"] if f["code"] == "EXPENSE_WITHOUT_EVIDENCE")
    assert still["id"] == target["id"] and still["status"] == "acknowledged"

    # 帳簿が変わると stale
    client.post("2026-03-01", [("100", "debit", 10000), ("310", "credit", 10000)], "事業主借")
    stale = client.ok("monitor.findings", {"year": 2026})["findings"]
    assert all(f["stale"] for f in stale)

    # 証憑を関連付けて再点検すると解消される
    img = tmp_path / "r.png"
    img.write_bytes(PNG)
    doc = client.ok("evidence.add", {"path": str(img), "auto_read": False})
    client.ok("evidence.link", {"document_id": doc["id"], "entry_id": expense["id"]})
    after = client.ok("monitor.check", {"year": 2026})
    assert "EXPENSE_WITHOUT_EVIDENCE" not in codes(after)
    assert "NEGATIVE_CASH" not in codes(after)
    assert after["stats"]["resolved"] >= 2
    resolved = client.ok("monitor.findings", {"year": 2026, "status": "resolved"})["findings"]
    assert any(f["code"] == "EXPENSE_WITHOUT_EVIDENCE" for f in resolved)


def test_carryforward_stale_is_error(client: Client):
    client.post("2026-01-01", [("110", "debit", 1000), ("320", "credit", 1000)], "元入金")
    client.ok("year.carryforward", {"from_year": 2026})
    assert "CARRYFORWARD_STALE" not in codes(client.ok("monitor.check", {"year": 2026}))
    client.post("2026-12-31", [("599", "debit", 100), ("210", "credit", 100)], "追加")
    result = client.ok("monitor.check", {"year": 2026})
    stale = next(f for f in result["findings"] if f["code"] == "CARRYFORWARD_STALE")
    assert stale["severity"] == "error"
    assert "OPENING_MISSING" not in codes(client.ok("monitor.check", {"year": 2027}))
