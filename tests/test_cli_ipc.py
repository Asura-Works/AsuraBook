"""CLI 応答契約 (15章4節) と IPC (02章3節) の試験。"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from asurabook.cli.main import main
from asurabook.infrastructure.ipc import framing

SCHEMA = json.loads((Path(__file__).parents[1] / "src/asurabook/specs/cli_response.schema.json").read_text())
FIXTURES = Path(__file__).parent / "fixtures"


def run(capsys, *argv: str) -> tuple[int, dict]:
    code = main(list(argv) + ["--json"])
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1, "stdout は最終JSON 1個"
    resp = json.loads(out[0])
    Draft202012Validator(SCHEMA).validate(resp)
    return code, resp


def test_cli_exit_codes_and_schema(capsys, tmp_path: Path):
    d = ["--data-dir", str(tmp_path / "d")]
    code, resp = run(capsys, "journal", "list", *d)
    assert code == 8 and resp["error"]["code"] == "NOT_INITIALIZED"
    code, resp = run(capsys, "init", "--name", "架空", "--years", "2026", "--dry-run", *d)
    assert code == 0 and not (tmp_path / "d" / "asurabook.db").exists()  # 未初期化dry-runでDBを作らない
    code, resp = run(capsys, "init", "--name", "架空", "--years", "2026", *d)
    assert code == 0
    code, resp = run(capsys, "journal", "show", "missing-id", *d)
    assert code == 2
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps({"journal_date": "2026-03-01", "lines": [
        {"account": "519", "side": "debit", "amount_jpy": 100}, {"account": "100", "side": "credit", "amount_jpy": 99}]}))
    code, resp = run(capsys, "journal", "draft", "--input", str(draft), "--idempotency-key", "k1", *d)
    assert code == 0
    entry = resp["data"]["id"]
    code, resp = run(capsys, "journal", "validate", entry, *d)
    assert code == 3 and resp["error"]["code"] == "VALIDATION_FAILED"
    draft.write_text(json.dumps({"journal_date": "2026-03-01", "description": "x", "lines": []}))
    code, resp = run(capsys, "journal", "draft", "--input", str(draft), "--idempotency-key", "k1", *d)
    assert code == 5 and resp["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    code, resp = run(capsys, "journal", "validate", entry, "--expected-revision", "99", *d)
    assert code == 5 and resp["error"]["code"] == "REVISION_CONFLICT"
    code, resp = run(capsys, "report", "trial-balance", "--year", "2026", "--reader", *d)
    assert code == 0
    code, resp = run(capsys, "journal", "draft", "--input", str(draft), "--reader", *d)
    assert code == 4  # reader を assistant へ引き上げない


def test_cli_import_and_export_root(capsys, tmp_path: Path):
    d = ["--data-dir", str(tmp_path / "d")]
    run(capsys, "init", "--name", "架空", "--years", "2026", *d)
    code, resp = run(capsys, "import", "inspect", str(FIXTURES / "generic_bank_v1.csv"), "--profile", "generic-v1", *d)
    assert code == 0 and resp["data"]["summary"]["accepted_rows"] == 2
    code, resp = run(capsys, "report", "export", "--year", "2026", "--report", "journal", "--path", "j.csv", *d)
    assert code == 0 and Path(resp["data"]["written"]).exists()
    code, resp = run(capsys, "report", "export", "--year", "2026", "--report", "journal", "--path", "j.csv", *d)
    assert resp["error"]["code"] == "EXPORT_EXISTS"  # no-clobber
    code, resp = run(capsys, "report", "export", "--year", "2026", "--report", "journal",
                     "--path", str(tmp_path / "outside.csv"), *d)
    assert resp["error"]["code"] == "EXPORT_PATH_DENIED"
    code, resp = run(capsys, "report", "export", "--year", "2026", "--report", "journal",
                     "--path", "../asurabook.db.csv", *d)
    assert resp["error"]["code"] == "EXPORT_PATH_DENIED"


def test_csv_export_neutralizes_formulas():
    from asurabook.application.exports import rows_to_csv
    data = rows_to_csv(["a"], [["=HYPERLINK(\"x\")"], [-500], ["@SUM(A1)"]]).decode("utf-8-sig")
    assert "'=HYPERLINK" in data and "'@SUM" in data and "\r\n-500\r\n" in data


def test_framing_roundtrip_and_limit():
    buf = io.BytesIO(framing.encode({"a": "日本語"}))
    assert framing.read_frame(buf) == {"a": "日本語"}
    big = io.BytesIO((framing.MAX_FRAME + 1).to_bytes(4, "big"))
    with pytest.raises(framing.FrameError):
        framing.read_frame(big)


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket")
def test_ipc_server_uses_same_service_and_assistant_profile(tmp_path: Path):
    from asurabook.application.core import Core
    from asurabook.application.session import Session, cli_session
    from asurabook.infrastructure.ipc.server import CoreClient, CoreServer, socket_path
    core = Core(tmp_path / "d")
    owner = Session("t:owner", "owner", "gui")
    assert core.execute("org.init", {"display_name": "架空", "years": [2026]}, owner)["ok"]
    server = CoreServer(core, lambda reader: cli_session(reader=reader), "test")
    server.serve_in_thread()
    try:
        assert oct(socket_path(core.paths.root).stat().st_mode & 0o777) == "0o600"
        client = CoreClient(core.paths.root)
        assert client.welcome["profile"] == "assistant"
        resp = client.execute("journal.draft", {"journal_date": "2026-03-01", "lines": [
            {"account": "519", "side": "debit", "amount_jpy": 100},
            {"account": "100", "side": "credit", "amount_jpy": 100}]})
        assert resp["ok"]
        entry = resp["data"]["id"]
        assert client.execute("journal.validate", {"id": entry})["ok"]
        denied = client.execute("journal.post", {"id": entry})
        assert denied["error"]["code"] == "PERMISSION_DENIED"
        # GUI (in-process owner) と同じ帳簿を見ている
        assert core.execute("journal.show", {"id": entry}, owner)["data"]["status"] == "validated"
        client.close()
    finally:
        server.shutdown()
        server.server_close()
        core.close()
