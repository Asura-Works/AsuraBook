"""17章2節の会計 golden fixtures (AC01〜AC12)。金額は架空の試験資料。"""

from __future__ import annotations

import sqlite3

import pytest

from conftest import ASSISTANT, OWNER, Client


def balance_of(client: Client, year: int, code: str) -> int:
    tb = client.ok("report.trial_balance", {"year": year})
    for r in tb["rows"]:
        if r["code"] == code:
            return r["debit_balance_jpy"] - r["credit_balance_jpy"]
    return 0


def test_ac01_unbalanced_draft_allowed_post_rejected(client: Client):
    draft = client.ok("journal.draft", {"journal_date": "2026-03-01", "description": "貸借不一致",
                                        "lines": [{"account": "519", "side": "debit", "amount_jpy": 100},
                                                  {"account": "100", "side": "credit", "amount_jpy": 99}]})
    assert draft["status"] == "draft"
    assert draft["difference_jpy"] == 1
    assert any(i["code"] == "UNBALANCED" for i in draft["issues"])
    err = client.err("journal.validate", {"id": draft["id"]})
    assert err["code"] == "VALIDATION_FAILED"
    err = client.err("journal.post", {"id": draft["id"]})
    assert err["code"] == "NOT_VALIDATED"


def test_ac02_card_receipt_debit_expense_once(client: Client):
    client.post("2026-10-05", [("519", "debit", 11000), ("210", "credit", 11000)], "カードA 文具")
    # レシート後着: 仕訳追加なし (証憑リンクは evidence テストで確認)
    item = client.ok("openitems.list", {"type": "payable"})["items"][0]
    settle = client.post("2026-11-27", [("210", "debit", 11000), ("110", "credit", 11000)], "カードA 引落し")
    line_id = next(l["id"] for l in settle["lines"] if l["account_code"] == "210")
    client.ok("reconcile.apply", {"allocations": [{"open_item_id": item["open_item_id"],
                                                   "settlement_line_id": line_id, "amount_jpy": 11000}]})
    pl = client.ok("report.profit_loss", {"year": 2026})
    assert pl["total_expense_jpy"] == 11000
    assert balance_of(client, 2026, "210") == 0
    assert client.ok("openitems.list", {"type": "payable"})["total_jpy"] == 0


def test_ac03_platform_sale_fee_withholding(client: Client):
    sale = client.post("2026-10-01", [("120", "debit", 110000), ("400", "credit", 110000)], "売上確定")
    item_id = sale["lines"][0]["open_item_id"]
    fee = client.post("2026-10-01", [("527", "debit", 16500), ("120", "credit", 16500)], "手数料")
    wht = client.post("2026-10-01", [("300", "debit", 10210, {"subaccount": "WHT"}),
                                     ("120", "credit", 10210)], "源泉控除")
    transfer = client.post("2026-10-20", [("110", "debit", 82960), ("527", "debit", 330), ("120", "credit", 83290)],
                           "振込")
    allocs = []
    for entry, amount in ((fee, 16500), (wht, 10210), (transfer, 83290)):
        line = next(l for l in entry["lines"] if l["account_code"] == "120")
        allocs.append({"open_item_id": item_id, "settlement_line_id": line["id"], "amount_jpy": amount})
    client.ok("reconcile.apply", {"allocations": allocs})
    pl = client.ok("report.profit_loss", {"year": 2026})
    assert pl["total_revenue_jpy"] == 110000
    # 源泉所得税は事業主貸であり費用に含まれない
    assert pl["total_expense_jpy"] == 16500 + 330
    assert balance_of(client, 2026, "120") == 0
    assert client.ok("openitems.list", {"type": "receivable"})["items"] == []


def test_ac04_same_day_same_amount_kept(client: Client):
    a = client.post("2026-05-01", [("519", "debit", 1100), ("100", "credit", 1100)], "同店")
    b = client.post("2026-05-01", [("519", "debit", 1100), ("100", "credit", 1100)], "同店")
    assert a["id"] != b["id"]
    assert client.ok("report.profit_loss", {"year": 2026})["total_expense_jpy"] == 2200


def test_ac05_posted_update_delete_rejected_by_db(client: Client, core):
    e = client.post("2026-02-01", [("519", "debit", 500), ("100", "credit", 500)])
    conn = core.writer()
    with pytest.raises(sqlite3.IntegrityError, match="POSTED_IMMUTABLE"):
        conn.execute("UPDATE journal_entries SET description='改ざん' WHERE id=?", (e["id"],))
    with pytest.raises(sqlite3.IntegrityError, match="POSTED_IMMUTABLE"):
        conn.execute("DELETE FROM journal_entries WHERE id=?", (e["id"],))
    with pytest.raises(sqlite3.IntegrityError, match="POSTED_IMMUTABLE"):
        conn.execute("UPDATE journal_lines SET amount_jpy=1 WHERE entry_id=?", (e["id"],))
    with pytest.raises(sqlite3.IntegrityError, match="POSTED_IMMUTABLE"):
        conn.execute("DELETE FROM journal_lines WHERE entry_id=?", (e["id"],))
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        conn.execute("DELETE FROM audit_events")
    err = client.err("journal.update", {"id": e["id"], "description": "x"})
    assert err["code"] == "POSTED_IMMUTABLE"


def test_ac06_double_reverse_single_reversal(client: Client):
    e = client.post("2026-02-01", [("519", "debit", 500), ("100", "credit", 500)])
    first = client.ok("journal.reverse", {"id": e["id"], "reason": "誤記帳"}, idempotency_key="rev-1")
    # 同key再送は保存結果だけを返す
    replay = client.raw("journal.reverse", {"id": e["id"], "reason": "誤記帳"}, idempotency_key="rev-1")
    assert replay["ok"] and replay["data"]["correction_group_id"] == first["correction_group_id"]
    assert any(w["code"] == "IDEMPOTENT_REPLAY" for w in replay["warnings"])
    # 別keyの二重取消は拒否
    err = client.err("journal.reverse", {"id": e["id"], "reason": "誤記帳"}, idempotency_key="rev-2")
    assert err["code"] == "ALREADY_REVERSED"
    reversals = client.ok("journal.list", {"year": 2026, "text": "取消"})["items"]
    assert len(reversals) == 1
    assert client.ok("report.profit_loss", {"year": 2026})["total_expense_jpy"] == 0


def test_ac07_monthly_to_detail_no_duplicate_revenue(client: Client, core):
    from asurabook.domain.values import new_id
    conn = core.writer()
    org = conn.execute("SELECT id FROM org").fetchone()[0]
    sa = client.ok("source_accounts.create", {"code": "PF-A", "provider": "demo", "kind": "platform",
                                              "display_name": "架空サービス", "account": "120"})
    # 月次集計イベント1件と明細イベント2件 (取込済みを想定)
    def event(amount: int, key: str, grain: str) -> str:
        eid = new_id()
        conn.execute("INSERT INTO source_transactions (id, org_id, source_account_id, event_type, "
                     "normalized_identity_key, transaction_date, grain, recognition_scope, amount_jpy, created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (eid, org, sa["source_account_id"], "sale", key, "2026-06-30", grain, "2026-06", amount, "t"))
        conn.execute("INSERT INTO source_usage_slots (id, event_id, role, capacity_jpy) VALUES (?,?,?,?)",
                     (new_id(), eid, "recognition", amount))
        return eid
    monthly = event(30000, "demo|PF-A|M2026-06", "monthly")
    d1 = event(10000, "demo|PF-A|D1", "detail")
    d2 = event(20000, "demo|PF-A|D2", "detail")
    m = client.post("2026-06-30", [("120", "debit", 30000), ("400", "credit", 30000,
                                   {"source_links": [{"event_id": monthly, "role": "recognition"}]})], "6月売上(月次)")
    # 同じ月次イベントをもう一度売上にすると利用枠超過
    draft = client.ok("journal.draft", {"journal_date": "2026-06-30", "lines": [
        {"account": "120", "side": "debit", "amount_jpy": 30000},
        {"account": "400", "side": "credit", "amount_jpy": 30000,
         "source_links": [{"event_id": monthly, "role": "recognition"}]}]})
    err = client.err("journal.validate", {"id": draft["id"]})
    assert any(i["code"] == "SOURCE_CAPACITY_EXCEEDED" for i in err["details"]["issues"])
    client.ok("journal.discard", {"id": draft["id"]})
    # 月次1件 → 反対1件 + 明細2件
    result = client.ok("journal.reverse", {"id": m["id"], "reason": "明細へ移行", "replacements": [
        {"journal_date": "2026-06-10", "description": "明細1", "lines": [
            {"account": "120", "side": "debit", "amount_jpy": 10000},
            {"account": "400", "side": "credit", "amount_jpy": 10000,
             "source_links": [{"event_id": d1, "role": "recognition"}]}]},
        {"journal_date": "2026-06-20", "description": "明細2", "lines": [
            {"account": "120", "side": "debit", "amount_jpy": 20000},
            {"account": "400", "side": "credit", "amount_jpy": 20000,
             "source_links": [{"event_id": d2, "role": "recognition"}]}]},
    ]})
    assert len(result["replacements"]) == 2
    assert client.ok("report.profit_loss", {"year": 2026})["total_revenue_jpy"] == 30000
    # 元の月次open itemは0、明細の2件が未回収
    items = client.ok("openitems.list", {"type": "receivable"})
    assert items["total_jpy"] == 30000
    assert len(items["items"]) == 2
    used = conn.execute("SELECT SUM(signed_effect_jpy) FROM source_links WHERE event_id=?", (monthly,)).fetchone()[0]
    assert used == 0


def test_ac08_ac09_dated_balance_and_cancel(client: Client):
    sale = client.post("2026-12-15", [("120", "debit", 110000), ("400", "credit", 110000)], "12月売上")
    item_id = sale["lines"][0]["open_item_id"]
    receipt = client.post("2027-01-10", [("110", "debit", 66000), ("120", "credit", 66000)], "一部回収")
    line_id = next(l["id"] for l in receipt["lines"] if l["account_code"] == "120")
    alloc = client.ok("reconcile.apply", {"allocations": [
        {"open_item_id": item_id, "settlement_line_id": line_id, "amount_jpy": 66000}]})["allocation_ids"][0]

    def bal(as_of: str) -> int:
        return client.ok("openitems.list", {"type": "receivable", "as_of": as_of, "include_settled": True})["total_jpy"]

    assert bal("2026-12-31") == 110000  # AC08
    assert bal("2027-01-10") == 44000
    client.ok("reconcile.cancel", {"allocation_id": alloc, "effective_date": "2027-01-15", "reason": "誤消込"})
    assert bal("2026-12-31") == 110000  # AC09
    assert bal("2027-01-10") == 44000
    assert bal("2027-01-15") == 110000


def _ac10_setup(client: Client) -> None:
    client.post("2026-01-01", [("110", "debit", 1000000), ("320", "credit", 1000000)], "期首元入金")
    client.post("2026-01-05", [("110", "debit", 700000), ("240", "credit", 700000)], "借入")
    client.post("2026-06-30", [("110", "debit", 600000), ("400", "credit", 600000)], "売上")
    client.post("2026-07-01", [("110", "debit", 100000), ("310", "credit", 100000)], "事業主借")
    client.post("2026-08-01", [("300", "debit", 300000), ("110", "credit", 300000)], "事業主貸")


def test_ac10_capital_carryforward(client: Client):
    _ac10_setup(client)
    result = client.ok("year.carryforward", {"from_year": 2026})
    assert result["kind"] == "opening"
    assert result["projection"]["opening_capital_jpy"] == 1400000
    assert balance_of(client, 2027, "320") == -1400000
    bs = client.ok("report.balance_sheet", {"year": 2027})
    assert bs["total_assets_jpy"] - bs["total_liabilities_jpy"] == 1400000
    assert bs["total_assets_jpy"] == 2100000
    # 2回目は差分なし
    assert client.ok("year.carryforward", {"from_year": 2026})["state"] == "current"


def test_ac11_prior_year_correction_adjusts_opening(client: Client):
    _ac10_setup(client)
    client.ok("year.carryforward", {"from_year": 2026})
    client.post("2026-12-31", [("599", "debit", 20000), ("210", "credit", 20000)], "前年費用/未払の追加")
    status = client.ok("year.carryforward_status", {"from_year": 2026})
    assert status["state"] == "opening_stale"
    result = client.ok("year.carryforward", {"from_year": 2026})
    assert result["kind"] == "opening_adjustment"
    assert balance_of(client, 2027, "320") == -1380000
    bs = client.ok("report.balance_sheet", {"year": 2027})
    assert bs["total_liabilities_jpy"] == 720000
    # 翌期の費用は増えない
    assert client.ok("report.profit_loss", {"year": 2027})["total_expense_jpy"] == 0


def test_ac12_tax_amount_states(client: Client, core):
    e = client.post("2026-04-01", [
        ("519", "debit", 1000, {"tax_amount_status": "unknown"}),
        ("514", "debit", 2000, {"tax_amount_status": "known", "tax_amount_jpy": 0}),
        ("510", "debit", 3000, {"tax_amount_status": "not_applicable"}),
        ("100", "credit", 6000)])
    rows = {l["account_code"]: (l["tax_amount_status"], l["tax_amount_jpy"]) for l in e["lines"]}
    assert rows["519"] == ("unknown", None)
    assert rows["514"] == ("known", 0)
    assert rows["510"] == ("not_applicable", None)
    err = client.err("journal.draft", {"journal_date": "2026-04-01", "lines": [
        {"account": "519", "side": "debit", "amount_jpy": 100, "tax_amount_status": "unknown", "tax_amount_jpy": 0},
        {"account": "100", "side": "credit", "amount_jpy": 100}]})
    assert err["code"] == "INVALID_INPUT"


def test_assistant_cannot_post_without_capability(client: Client):
    draft = client.ok("journal.draft", {"journal_date": "2026-03-01", "lines": [
        {"account": "519", "side": "debit", "amount_jpy": 100}, {"account": "100", "side": "credit", "amount_jpy": 100}]},
        session=ASSISTANT)
    validated = client.ok("journal.validate", {"id": draft["id"]}, session=ASSISTANT)
    err = client.err("journal.post", {"id": draft["id"]}, session=ASSISTANT)
    assert err["code"] == "PERMISSION_DENIED"
    # GUI owner が承認した plan だけ一回用 capability で確定できる
    cap = client.ok("capability.issue", {"scope": "journal.post", "principal": ASSISTANT.principal, "targets": [
        {"id": draft["id"], "revision": validated["revision"], "validation_hash": validated["validation_hash"]}]})
    posted = client.ok("journal.post", {"id": draft["id"]}, session=ASSISTANT, capability_token=cap["token"],
                       idempotency_key="p1")
    assert posted["posted"][0]["status"] == "posted"
    # 同key再送は消費済tokenで再実行せず保存結果を返す
    replay = client.raw("journal.post", {"id": draft["id"]}, session=ASSISTANT, capability_token=cap["token"],
                        idempotency_key="p1")
    assert replay["ok"] and any(w["code"] == "IDEMPOTENT_REPLAY" for w in replay["warnings"])
    # 別key・使用済token は拒否
    err = client.err("journal.post", {"id": draft["id"]}, session=ASSISTANT, capability_token=cap["token"],
                     idempotency_key="p2")
    assert err["code"] == "CAPABILITY_INVALID"


def test_capability_cannot_be_issued_from_cli(client: Client):
    from asurabook.application.session import Session
    cli_owner_claim = Session(principal="x", profile="owner", entry_point="cli")
    err = client.err("capability.issue", {"scope": "journal.post", "principal": "x", "targets": [{"id": "a"}]},
                     session=cli_owner_claim)
    assert err["code"] == "PERMISSION_DENIED"


def test_idempotency_conflict_on_different_payload(client: Client):
    base = {"journal_date": "2026-03-01", "lines": [{"account": "519", "side": "debit", "amount_jpy": 100},
                                                    {"account": "100", "side": "credit", "amount_jpy": 100}]}
    client.ok("journal.draft", base, idempotency_key="k1", session=ASSISTANT)
    changed = dict(base, description="changed")
    err = client.err("journal.draft", changed, idempotency_key="k1", session=ASSISTANT)
    assert err["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(client.ok("journal.list", {"year": 2026})["items"]) == 1


def test_dry_run_changes_nothing(client: Client, core):
    before = core.paths.db.read_bytes()
    resp = client.raw("journal.draft", {"journal_date": "2026-03-01", "lines": [
        {"account": "519", "side": "debit", "amount_jpy": 100}, {"account": "100", "side": "credit", "amount_jpy": 100}]},
        dry_run=True, idempotency_key="dry")
    assert resp["ok"] and any(w["code"] == "DRY_RUN" for w in resp["warnings"])
    assert client.ok("journal.list", {"year": 2026})["items"] == []
    count = core.writer().execute("SELECT COUNT(*) FROM command_results").fetchone()[0]
    assert count == 0


def test_period_close_blocks_posting(client: Client):
    client.ok("period.set_state", {"month": "2026-03", "state": "soft_closed"})
    draft = client.ok("journal.draft", {"journal_date": "2026-03-15", "lines": [
        {"account": "519", "side": "debit", "amount_jpy": 100}, {"account": "100", "side": "credit", "amount_jpy": 100}]})
    err = client.err("journal.validate", {"id": draft["id"]})
    assert any(i["code"] == "PERIOD_CLOSED" for i in err["details"]["issues"])
    err = client.err("period.set_state", {"month": "2026-03", "state": "open"})
    assert err["code"] == "INVALID_INPUT"  # 理由なしの再開は不可
    client.ok("period.set_state", {"month": "2026-03", "state": "open", "reason": "訂正のため"})
    client.ok("journal.validate", {"id": draft["id"]})


def test_audit_chain_links(core, client: Client):
    client.post("2026-02-01", [("519", "debit", 500), ("100", "credit", 500)])
    from asurabook.domain.values import canonical_json, sha256_hex
    rows = core.writer().execute("SELECT * FROM audit_events ORDER BY sequence_no").fetchall()
    prev = "0" * 64
    for r in rows:
        assert r["prev_hash"] == prev
        event = {k: r[k] for k in ("id", "org_id", "actor", "request_id", "command", "job_id", "target_type",
                                   "target_id", "before_revision", "after_revision", "input_hash", "result_hash",
                                   "reason", "detail_json", "created_at")}
        assert sha256_hex(prev + canonical_json(event)) == r["hash"]
        prev = r["hash"]
