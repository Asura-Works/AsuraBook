"""取込共通基盤 (05章9節) の試験。"""

from __future__ import annotations

from pathlib import Path

from conftest import ASSISTANT, Client

FIXTURES = Path(__file__).parent / "fixtures"


def _bank(client: Client) -> None:
    client.ok("source_accounts.create", {"code": "BANK-DEMO", "provider": "demo-bank", "kind": "bank",
                                         "display_name": "架空銀行 普通", "account": "110"})


def test_inspect_has_no_side_effects(client: Client, core):
    data = client.ok("import.inspect", {"path": str(FIXTURES / "generic_bank_v1.csv"), "profile": "generic-v1"})
    assert data["summary"]["total_rows"] == 2
    assert data["summary"]["accepted_rows"] == 2
    assert data["rows"][0]["amount_jpy"] == 82960
    assert data["rows"][1]["amount_jpy"] == -11000
    assert not (core.paths.spool / "sources").exists()


def test_stage_then_restage_same_file_is_idempotent(client: Client):
    _bank(client)
    path = str(FIXTURES / "generic_bank_v1.csv")
    first = client.ok("import.stage", {"path": path, "profile": "generic-v1", "account": "BANK-DEMO"},
                      session=ASSISTANT)
    assert first["new_events"] == 2 and first["accounted_rows"] == 2
    again = client.raw("import.stage", {"path": path, "profile": "generic-v1", "account": "BANK-DEMO"},
                       session=ASSISTANT)
    assert again["ok"] and again["data"]["duplicate_file"] is True
    assert any(w["code"] == "SAME_FILE_REUSED" for w in again["warnings"])
    assert len(client.ok("import.events")) == 2


def test_full_period_reacquisition_does_not_duplicate(client: Client, tmp_path: Path):
    _bank(client)
    a = tmp_path / "a.csv"
    a.write_text("row_id,source_account,event_type,currency,transaction_date,amount_jpy,description\n"
                 "R1,BANK-DEMO,bank_credit,JPY,2026-10-01,1000,入金\n", encoding="utf-8")
    b = tmp_path / "b.csv"  # 期間を重ねて再取得 (R1 + 新規 R2)
    b.write_text("row_id,source_account,event_type,currency,transaction_date,amount_jpy,description\n"
                 "R1,BANK-DEMO,bank_credit,JPY,2026-10-01,1000,入金\n"
                 "R2,BANK-DEMO,bank_debit,JPY,2026-10-02,-500,出金\n", encoding="utf-8")
    client.ok("import.stage", {"path": str(a), "profile": "generic-v1", "account": "BANK-DEMO"})
    second = client.ok("import.stage", {"path": str(b), "profile": "generic-v1", "account": "BANK-DEMO"})
    assert second["new_events"] == 1 and second["known_events"] == 1
    assert len(client.ok("import.events")) == 2


def test_same_id_changed_amount_is_held_not_overwritten(client: Client, tmp_path: Path):
    _bank(client)
    a = tmp_path / "a.csv"
    a.write_text("row_id,source_account,event_type,currency,transaction_date,amount_jpy\n"
                 "R1,BANK-DEMO,bank_credit,JPY,2026-10-01,1000\n", encoding="utf-8")
    b = tmp_path / "b.csv"
    b.write_text("row_id,source_account,event_type,currency,transaction_date,amount_jpy\n"
                 "R1,BANK-DEMO,bank_credit,JPY,2026-10-01,1200\n", encoding="utf-8")
    client.ok("import.stage", {"path": str(a), "profile": "generic-v1", "account": "BANK-DEMO"})
    second = client.ok("import.stage", {"path": str(b), "profile": "generic-v1", "account": "BANK-DEMO"})
    assert second["held_rows"] == 1
    assert any(i["code"] == "SOURCE_REVISION_CONFLICT" for i in second["issues"])
    events = client.ok("import.events")
    assert [e["amount_jpy"] for e in events] == [1000]


def test_same_day_same_amount_rows_kept(client: Client, tmp_path: Path):
    _bank(client)
    f = tmp_path / "x.csv"
    f.write_text("row_id,source_account,event_type,currency,transaction_date,amount_jpy,description\n"
                 "R1,BANK-DEMO,bank_debit,JPY,2026-10-01,-1100,同店\n"
                 "R2,BANK-DEMO,bank_debit,JPY,2026-10-01,-1100,同店\n", encoding="utf-8")
    result = client.ok("import.stage", {"path": str(f), "profile": "generic-v1", "account": "BANK-DEMO"})
    assert result["new_events"] == 2


def test_cp932_and_bad_rows_are_reported_not_dropped(client: Client, tmp_path: Path):
    _bank(client)
    f = tmp_path / "sjis.csv"
    text = ("row_id,source_account,event_type,currency,transaction_date,amount_jpy,description\n"
            "R1,BANK-DEMO,bank_credit,JPY,2026-10-01,\"1,000\",入金テスト\n"
            "R2,BANK-DEMO,bank_credit,JPY,2026-10-01,,空欄\n"
            "R3,BANK-DEMO,bank_debit,JPY,2026-10-01,500,符号違い\n"
            "R4,OTHER,bank_credit,JPY,2026-10-01,100,別口座\n"
            "R5,BANK-DEMO,bank_credit,USD,2026-10-01,100,外貨\n"
            "R6,BANK-DEMO,bank_credit,JPY,2026-02-30,100,日付\n")
    f.write_bytes(text.encode("cp932"))
    result = client.ok("import.stage", {"path": str(f), "profile": "generic-v1", "account": "BANK-DEMO"})
    assert result["summary"]["encoding"] == "cp932"
    assert result["new_events"] == 1
    codes = {i["code"] for i in result["issues"]}
    assert {"AMOUNT_MISSING", "SIGN_INVALID", "ACCOUNT_MISMATCH", "CURRENCY_NOT_JPY", "DATE_INVALID"} <= codes
    # 成功件数と保留件数の総和が原資料の行数を説明できる
    assert result["accounted_rows"] == 6
    review = client.ok("import.review", {"batch_id": result["batch_id"]})
    assert review["batch"]["status"] == "needs_review"


def test_undecodable_file_is_rejected(client: Client, tmp_path: Path):
    _bank(client)
    f = tmp_path / "bad.csv"
    f.write_bytes(b"row_id,source_account\n\xff\xfe\xfa\x80\x81\n")
    err = client.err("import.stage", {"path": str(f), "profile": "generic-v1", "account": "BANK-DEMO"})
    assert err["code"] == "ENCODING_AMBIGUOUS"


def test_bank_event_cannot_be_used_twice(client: Client):
    """同じ入金を2回仕訳に使うと利用枠超過で確定できない。"""
    _bank(client)
    staged = client.ok("import.stage", {"path": str(FIXTURES / "generic_bank_v1.csv"), "profile": "generic-v1",
                                        "account": "BANK-DEMO"})
    credit = staged["events"][0]["event_id"]
    lines = [{"account": "110", "side": "debit", "amount_jpy": 82960,
              "source_links": [{"event_id": credit, "role": "settlement"}]},
             {"account": "120", "side": "credit", "amount_jpy": 82960}]
    client.post("2026-10-20", [(l["account"], l["side"], l["amount_jpy"],
                                {k: v for k, v in l.items() if k == "source_links"}) for l in lines])
    draft = client.ok("journal.draft", {"journal_date": "2026-10-20", "lines": lines})
    err = client.err("journal.validate", {"id": draft["id"]})
    assert any(i["code"] == "SOURCE_CAPACITY_EXCEEDED" for i in err["details"]["issues"])
    err = client.err("journal.draft", {"journal_date": "2026-10-20", "lines": [
        {"account": "110", "side": "debit", "amount_jpy": 82960,
         "source_links": [{"event_id": credit, "role": "recognition"}]},
        {"account": "400", "side": "credit", "amount_jpy": 82960}]})
    assert err["code"] == "SOURCE_ROLE_UNAVAILABLE"
