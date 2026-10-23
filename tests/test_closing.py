"""決算整理: 家事按分と減価償却の試験。金額・償却率は架空の試験値で、税務上の推奨値ではない。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from conftest import Client
from asurabook.domain.assets import business_split, depreciation_for_year, months_in_service


def pl_amount(client: Client, year: int, code: str) -> int:
    pl = client.ok("report.profit_loss", {"year": year})
    return next((r["amount_jpy"] for r in pl["expense"] if r["code"] == code), 0)


def test_year_end_apportionment_once_and_not_on_top_of_entry_level(client: Client):
    for m in range(1, 13):
        client.post(f"2026-{m:02d}-25", [("514", "debit", 11000), ("110", "credit", 11000)], f"通信費{m}月")
    # 計上時に按分済みの仕訳 (04章6節の例: 費用6,600 + 事業主貸4,400 / 未払金11,000)
    client.post("2026-06-30", [("514", "debit", 6600, {"business_ratio": "0.6"}), ("300", "debit", 4400),
                               ("210", "credit", 11000)], "共用回線 (計上時按分)")
    rule = client.ok("apportionment.rule_create", {"year": 2026, "account": "514", "business_ratio": "0.6",
                                                   "basis": "業務使用時間の記録 (架空)"})
    listed = client.ok("apportionment.list", {"year": 2026})[0]
    assert listed["preview"] == {"base_jpy": 132000, "business_jpy": 79200, "private_jpy": 52800, "entries": 12,
                                 "skipped_already_apportioned": 1}
    result = client.raw("apportionment.apply", {"rule_id": rule["rule_id"]})
    assert result["ok"] and any(w["code"] == "SKIPPED_APPORTIONED_LINES" for w in result["warnings"])
    assert pl_amount(client, 2026, "514") == 132000 + 6600 - 52800
    assert client.err("apportionment.apply", {"rule_id": rule["rule_id"]})["code"] == "ALREADY_APPLIED"
    assert client.err("apportionment.rule_create", {"year": 2026, "account": "514", "business_ratio": "0.5",
                                                    "basis": "x"})["code"] == "APPORTIONMENT_OVERLAP"
    # 整理仕訳を取り消せば同じ対象額でやり直せる
    client.ok("journal.reverse", {"id": result["data"]["entry"]["id"], "reason": "割合の見直し"})
    again = client.ok("apportionment.list", {"year": 2026})[0]
    assert again["applied"] is False and again["preview"]["base_jpy"] == 132000
    client.ok("apportionment.apply", {"rule_id": rule["rule_id"]})
    assert pl_amount(client, 2026, "514") == 85800


def test_straight_line_depreciation_with_business_ratio(client: Client):
    client.post("2026-04-10", [("150", "debit", 300000), ("110", "credit", 300000)], "PC購入")
    reg = client.raw("asset.register", {"name": "PC", "acquired_on": "2026-04-10", "acquisition_cost_jpy": 300000,
                                        "method": "straight_line", "useful_life_years": 4, "rate_decimal": "0.250",
                                        "business_ratio": "0.8", "basis": "耐用年数表 (架空の試験値)"})
    assert reg["ok"] and any(w["code"] == "TAX_RULE_NOT_VERIFIED" for w in reg["warnings"])
    sched = client.ok("asset.list", {"year": 2026})[0]["calculation"]
    assert sched["months"] == 9 and sched["depreciation_jpy"] == 56250
    assert (sched["business_jpy"], sched["private_jpy"]) == (45000, 11250)
    monitor = client.ok("monitor.check", {"year": 2026})
    assert any(f["code"] == "ASSET_NOT_DEPRECIATED" for f in monitor["findings"])
    done = client.ok("asset.depreciate", {"year": 2026})
    assert done["posted"][0]["book_value_end_jpy"] == 243750
    assert client.ok("asset.depreciate", {"year": 2026})["skipped"][0]["reason"] == "計上済み"
    assert pl_amount(client, 2026, "520") == 45000
    bs = client.ok("report.balance_sheet", {"year": 2026})
    assert next(a for a in bs["assets"] if a["code"] == "150")["amount_jpy"] == 243750
    assert not any(f["code"] == "ASSET_NOT_DEPRECIATED" for f in client.ok("monitor.check", {"year": 2026})["findings"])
    assert client.ok("asset.list", {"year": 2027})[0]["calculation"]["depreciation_jpy"] == 75000


def test_depreciation_domain_edges():
    y = lambda n: (dt.date(n, 1, 1), dt.date(n, 12, 31))  # noqa: E731
    assert months_in_service(dt.date(2026, 12, 31), *y(2026)) == 1
    assert months_in_service(dt.date(2027, 1, 1), *y(2026)) == 0
    # 備忘価額 1円を残す
    last = depreciation_for_year(method="straight_line", cost=100000, rate=Decimal("0.250"), in_service=dt.date(2020, 1, 1),
                                 year_start=dt.date(2023, 1, 1), year_end=dt.date(2023, 12, 31),
                                 accumulated_before=75000, years_already=3)
    assert last["depreciation_jpy"] == 24999 and last["book_value_end_jpy"] == 1
    amounts = [depreciation_for_year(method="lump_sum_3y", cost=100000, rate=None, in_service=dt.date(2026, 5, 1),
                                     year_start=dt.date(2026 + i, 1, 1), year_end=dt.date(2026 + i, 12, 31),
                                     accumulated_before=33333 * i, years_already=i)["depreciation_jpy"] for i in range(3)]
    assert amounts == [33333, 33333, 33334]
    assert business_split(56250, Decimal("0.8")) == (45000, 11250)
