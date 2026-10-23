"""backup / restore / recovery_hold (16章) の試験。"""

from __future__ import annotations

from pathlib import Path

from conftest import ASSISTANT, OWNER, Client
from asurabook.application import config as app_config
from asurabook.application.core import Core
from asurabook.application.worker import Worker

PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 64


def test_backup_restore_to_new_pc_with_recovery_hold(client: Client, core, tmp_path: Path):
    app_config.set_value(core.paths, "cloud.provider", "local_dir")
    client.post("2026-02-01", [("519", "debit", 500), ("100", "credit", 500)], "backup前")
    img = tmp_path / "r.png"
    img.write_bytes(PNG)
    doc = client.ok("evidence.add", {"path": str(img), "auto_read": False})
    validated = client.ok("journal.validate", {"id": client.ok("journal.draft", {
        "journal_date": "2026-02-02", "lines": [{"account": "519", "side": "debit", "amount_jpy": 1},
                                                {"account": "100", "side": "credit", "amount_jpy": 1}]})["id"]})
    cap = client.ok("capability.issue", {"scope": "journal.post", "principal": ASSISTANT.principal, "targets": [
        {"id": validated["id"], "revision": validated["revision"], "validation_hash": validated["validation_hash"]}]})

    backup = client.ok("backup.create", {"dest": str(tmp_path / "backups")}, session=ASSISTANT)
    verify = client.ok("backup.verify", {"path": backup["path"]})
    assert verify["ok"], verify
    assert backup["manifest"]["unsynced_objects"] == 1

    # backup後の変更
    client.post("2026-02-03", [("519", "debit", 700), ("100", "credit", 700)], "backup後")

    # 新PCへ復元 (別のデータフォルダ)
    new_core = Core(tmp_path / "newpc")
    new_client = Client(new_core)
    preview = new_client.ok("restore.preview", {"path": backup["path"]})
    assert preview["ok"]
    err = new_client.err("restore.apply", {"path": backup["path"], "confirm": True}, session=ASSISTANT)
    assert err["code"] == "PERMISSION_DENIED"
    result = new_client.ok("restore.apply", {"path": backup["path"], "confirm": True})
    assert result["recovery_hold"] is True
    assert result["held_items"]["held_outbox"] == 1
    assert result["held_items"]["revoked_capabilities"] == 1

    # 帳簿は backup 時点 (backup後の700円は含まない)
    assert new_client.ok("report.profit_loss", {"year": 2026})["total_expense_jpy"] == 500
    shown = new_client.ok("evidence.show", {"document_id": doc["id"]})
    assert (new_core.paths.spool / shown["objects"][0]["local_ref"]).read_bytes() == PNG
    # 復元前の capability は失効
    err = new_client.err("journal.post", {"id": validated["id"]}, session=ASSISTANT, capability_token=cap["token"])
    assert err["code"] == "CAPABILITY_INVALID"
    # recovery_hold 中は upload を再開しない
    app_config.set_value(new_core.paths, "cloud.provider", "local_dir")
    assert Worker(new_core, ASSISTANT).run_upload_once()["error"]["code"] == "RECOVERY_HOLD"
    status = new_client.ok("recovery.status")
    assert "upload" in status["held_scopes"]
    released = new_client.ok("recovery.release", {"scope": "upload", "reason": "保管先に同hashのobjectがないことを確認"})
    assert released["requeued_uploads"] == 1
    assert Worker(new_core, ASSISTANT).run_upload_once()["outcome"] == "verified"
    new_core.close()


def test_restore_in_place_keeps_aside_copy(client: Client, core, tmp_path: Path):
    client.post("2026-02-01", [("519", "debit", 500), ("100", "credit", 500)])
    backup = client.ok("backup.create", {"dest": str(tmp_path / "b")})
    client.post("2026-02-02", [("519", "debit", 900), ("100", "credit", 900)])
    preview = client.ok("restore.preview", {"path": backup["path"]})
    assert preview["changes_since_backup"]["audit_events_after_backup"] > 0
    result = client.ok("restore.apply", {"path": backup["path"], "confirm": True})
    assert Path(result["aside"], "asurabook.db").exists()
    assert client.ok("report.profit_loss", {"year": 2026})["total_expense_jpy"] == 500


def test_tampered_backup_is_rejected(client: Client, tmp_path: Path):
    backup = client.ok("backup.create", {"dest": str(tmp_path / "b")})
    db = Path(backup["path"]) / "asurabook.db"
    db.write_bytes(db.read_bytes() + b"x")
    verify = client.ok("backup.verify", {"path": backup["path"]})
    assert not verify["ok"]
    err = client.err("restore.apply", {"path": backup["path"], "confirm": True})
    assert err["code"] == "BACKUP_INVALID"


def test_backup_inside_data_dir_rejected(client: Client, core):
    err = client.err("backup.create", {"dest": str(core.paths.root / "bk")})
    assert err["code"] == "INVALID_INPUT"
