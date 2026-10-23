"""asurabook CLI (15章)。

GUI と同じ Application Service (Core.execute) を呼ぶ。SQLite直接操作や任意SQLは公開しない。
stdout は最終JSON 1個 (--json)、進捗と人向けの補足は stderr。exit code は 15章4節に従う。
公開CLIの session は assistant (無token)。--reader で reader に下げられるが、owner の自己申告はできない。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

from ..application import commands  # noqa: F401  command登録
from ..application.core import APP_VERSION, PROTOCOL_VERSION, REGISTRY, Core, exit_code_for
from ..application.session import cli_session
from ..domain.errors import EXIT_INPUT, EXIT_UNSUPPORTED
from ..domain.values import new_id
from ..infrastructure.paths import default_data_dir

Builder = Callable[[argparse.Namespace], dict]


def _common() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("共通option")
    g.add_argument("--data-dir", help="利用者データフォルダ (既定: LOCALAPPDATA/AsuraBook/orgs/<org>)")
    g.add_argument("--org", help="事業者フォルダ名 (既定 default)。暗黙に別事業者へ切り替えない")
    g.add_argument("--json", action="store_true", help="機械可読JSON応答を stdout に出す")
    g.add_argument("--format", choices=["json", "text"], help="--json と同じ (json)")
    g.add_argument("--input", help="payload JSON file。- で stdin")
    g.add_argument("--dry-run", action="store_true", help="副作用を起こさず検証する")
    g.add_argument("--request-id", help="追跡ID (UUID)")
    g.add_argument("--idempotency-key", help="成功済み再送の重複防止キー")
    g.add_argument("--expected-revision", type=int, help="更新対象の revision")
    g.add_argument("--no-input", action="store_true", help="対話を禁止する (不足はerror)")
    g.add_argument("--timeout-seconds", type=float, help="core 接続時の client 待機時間")
    g.add_argument("--reader", action="store_true", help="reader 権限で接続する")
    g.add_argument("--capability-stdin", action="store_true", help="GUIで承認された capability token を stdin から読む")
    g.add_argument("--via-core", action="store_true", help="起動中の core server へ IPC で接続する")
    return p


class Cli:
    def __init__(self) -> None:
        self.common = _common()
        self.parser = argparse.ArgumentParser(prog="asurabook", description="AsuraBook 会計CLI", parents=[self.common])
        self.groups = self.parser.add_subparsers(dest="group", metavar="<group>")
        self._group_parsers: dict[str, argparse._SubParsersAction] = {}

    def leaf(self, group: str, action: str | None, command: str | None, builder: Builder | None = None,
             help: str = "", local: Callable | None = None) -> argparse.ArgumentParser:
        if action is None:
            p = self.groups.add_parser(group, parents=[self.common], help=help)
        else:
            if group not in self._group_parsers:
                gp = self.groups.add_parser(group, help=f"{group} command")
                self._group_parsers[group] = gp.add_subparsers(dest="action", metavar="<action>")
            p = self._group_parsers[group].add_parser(action, parents=[self.common], help=help)
        p.set_defaults(core_command=command, builder=builder or (lambda a: {}), local=local)
        return p


def _year(a: argparse.Namespace) -> dict:
    return {"year": a.year} if getattr(a, "year", None) is not None else {}


def build() -> Cli:
    cli = Cli()
    L = cli.leaf

    p = L("init", None, "org.init", lambda a: {"display_name": a.name, "years": a.years or None},
          help="DBを新規作成する")
    p.add_argument("--name", required=True)
    p.add_argument("--years", type=int, nargs="*")

    L("doctor", None, "doctor", help="runtime・DB・配置の診断")
    L("version", None, None, local=lambda a, c: {"app_version": APP_VERSION, "protocol_version": PROTOCOL_VERSION},
      help="版")
    L("capabilities", None, "capabilities", help="利用できる command と権限")
    p = L("schema", "show", None, local=_schema_show, help="command の説明と権限")
    p.add_argument("name")

    L("accounts", "list", "accounts.list", help="勘定科目")
    L("years", "list", "year.list", help="年度")
    L("settings", "show", "settings.show", help="設定")
    L("profiles", "list", None, local=lambda a, c: [{"profile": "generic-v1", "status": "本番利用可 (汎用CSV v1)"}],
      help="取込profile")
    L("source-accounts", "list", "source_accounts.list", help="取込元アカウント")

    p = L("import", "inspect", "import.inspect",
          lambda a: {"path": _abs(a.file), "profile": a.profile, "account": a.account}, help="副作用なしの解析")
    p.add_argument("file")
    p.add_argument("--profile", required=True)
    p.add_argument("--account")
    p = L("import", "stage", "import.stage",
          lambda a: {"path": _abs(a.file), "profile": a.profile, "account": a.account}, help="原本と候補を登録")
    p.add_argument("file")
    p.add_argument("--profile", required=True)
    p.add_argument("--account", required=True)
    p = L("import", "review", "import.review", lambda a: {"batch_id": a.batch_id}, help="取込batchの確認")
    p.add_argument("batch_id")
    p = L("import", "events", "import.events", lambda a: {"include_used": a.all}, help="未記帳の経済イベント")
    p.add_argument("--all", action="store_true")

    p = L("journal", "list", "journal.list", lambda a: _year(a) | _drop_none(
        {"date_from": a.date_from, "date_to": a.date_to, "status": a.status, "text": a.text, "account": a.account,
         "limit": a.limit, "cursor": a.cursor}), help="仕訳一覧")
    p.add_argument("--year", type=int)
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    p.add_argument("--status", choices=["draft", "validated", "posted"])
    p.add_argument("--text")
    p.add_argument("--account")
    p.add_argument("--limit", type=int)
    p.add_argument("--cursor")
    p = L("journal", "show", "journal.show", lambda a: {"id": a.id}, help="仕訳1件")
    p.add_argument("id")
    L("journal", "draft", "journal.draft", help="下書き (--input で payload)")
    p = L("journal", "update", "journal.update", lambda a: {"id": a.id}, help="下書きの更新 (--input)")
    p.add_argument("id")
    p = L("journal", "validate", "journal.validate", lambda a: {"id": a.id}, help="確定条件の検査")
    p.add_argument("id")
    p = L("journal", "post", "journal.post", lambda a: {"ids": a.ids}, help="確定 (権限または capability が必要)")
    p.add_argument("ids", nargs="+")
    p = L("journal", "reverse", "journal.reverse", lambda a: _drop_none({"id": a.id, "reason": a.reason,
                                                                        "journal_date": a.date}),
          help="訂正 (反対+置換。置換は --input の replacements)")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    p.add_argument("--date")
    p = L("journal", "discard", "journal.discard", lambda a: {"id": a.id}, help="下書きの破棄")
    p.add_argument("id")

    p = L("openitems", "list", "openitems.list", lambda a: _drop_none({"as_of": a.as_of, "type": a.type}),
          help="as_of 時点の未回収/未払")
    p.add_argument("--as-of")
    p.add_argument("--type", choices=["receivable", "payable"])
    L("reconcile", "suggest", "reconcile.suggest", help="消込候補")
    L("reconcile", "apply", "reconcile.apply", help="消込の適用 (--input: allocations)")
    p = L("reconcile", "cancel", "reconcile.cancel",
          lambda a: {"allocation_id": a.allocation_id, "effective_date": a.effective_date, "reason": a.reason},
          help="割当の取消")
    p.add_argument("allocation_id")
    p.add_argument("--effective-date", required=True)
    p.add_argument("--reason", required=True)

    p = L("evidence", "add", "evidence.add", lambda a: _drop_none(
        {"path": _abs(a.file), "receipt_kind": a.kind, "auto_read": False if a.no_read else None}), help="証憑の受付")
    p.add_argument("file")
    p.add_argument("--kind", default="paper_photo")
    p.add_argument("--no-read", action="store_true", help="自動読取りしない")
    L("evidence", "list", "evidence.list", help="証憑一覧")
    p = L("evidence", "show", "evidence.show", lambda a: {"document_id": a.id}, help="証憑1件")
    p.add_argument("id")
    p = L("evidence", "read", "evidence.read", lambda a: {"document_id": a.id}, help="読取りを待ち行列へ")
    p.add_argument("id")
    p = L("evidence", "review", "evidence.review", lambda a: {"document_id": a.id}, help="確認値の保存 (--input)")
    p.add_argument("id")
    p = L("evidence", "link", "evidence.link", lambda a: {"document_id": a.id, "entry_id": a.entry_id},
          help="証憑と仕訳の関連付け")
    p.add_argument("id")
    p.add_argument("entry_id")

    for action, cmd in (("trial-balance", "report.trial_balance"), ("profit-loss", "report.profit_loss"),
                        ("balance-sheet", "report.balance_sheet")):
        p = L("report", action, cmd, lambda a: _year(a) | _drop_none({"date_from": a.date_from, "date_to": a.date_to}))
        p.add_argument("--year", type=int, required=True)
        p.add_argument("--from", dest="date_from")
        p.add_argument("--to", dest="date_to")
    p = L("report", "ledger", "report.general_ledger", lambda a: _year(a) | {"account": a.account}, help="元帳")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--account", required=True)
    p = L("report", "export", "report.export", lambda a: _year(a) | {"report": a.report, "path": a.path},
          help="CSV出力 (export root 配下、上書きしない)")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--report", required=True, choices=["trial_balance", "profit_loss", "journal"])
    p.add_argument("--path", required=True)
    p = L("dashboard", None, "dashboard.summary", lambda a: _year(a), help="現在状況")
    p.add_argument("--year", type=int, required=True)

    p = L("year-end", "status", "year.carryforward_status", lambda a: {"from_year": a.from_year}, help="繰越の状態")
    p.add_argument("--from-year", type=int, required=True)
    p = L("year-end", "carryforward", "year.carryforward", lambda a: {"from_year": a.from_year}, help="翌期繰越 (owner)")
    p.add_argument("--from-year", type=int, required=True)
    p = L("tax", "snapshot", "tax.snapshot_create", lambda a: _year(a), help="帳簿集計snapshot")
    p.add_argument("--year", type=int, required=True)

    p = L("tax", "profile-show", "tax.profile_show", lambda a: _year(a), help="年度の税務設定")
    p.add_argument("--year", type=int, required=True)
    p = L("tax", "profile-set", "tax.profile_set", lambda a: _year(a) | ({"confirm": True} if a.confirm else {}),
          help="税務設定の新しい版 (owner、--input で profile)")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--confirm", action="store_true")
    p = L("monitor", "check", "monitor.check", lambda a: _year(a), help="申告準備の決定的検査")
    p.add_argument("--year", type=int, required=True)
    p = L("monitor", "findings", "monitor.findings", lambda a: _year(a) | _drop_none({"status": a.status}),
          help="保存済みの finding")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--status", choices=["open", "acknowledged", "resolved"])
    p = L("monitor", "ack", "monitor.ack", lambda a: {"id": a.id}, help="finding を既読にする (解消ではない)")
    p.add_argument("id")

    L("ai", "status", None, local=_ai_status, help="Claude CLI の版・flag・実効認証")
    L("ai", "jobs", "ai.jobs", help="AI job 履歴")
    p = L("worker", "run", None, local=_worker_run, help="OCR・upload を処理する")
    p.add_argument("--drain", action="store_true")

    p = L("backup", "create", "backup.create", lambda a: {"dest": _abs(a.dest), "full": not a.light}, help="backup")
    p.add_argument("--dest", required=True)
    p.add_argument("--light", action="store_true", help="cloud verified 原本を参照だけにする")
    p = L("backup", "verify", "backup.verify", lambda a: {"path": _abs(a.path)}, help="backup検査")
    p.add_argument("path")
    p = L("restore", "preview", "restore.preview", lambda a: {"path": _abs(a.path)}, help="復元の影響")
    p.add_argument("path")
    L("recovery", "status", "recovery.status", help="復元gateの状態")

    L("core", "serve", None, local=_core_serve, help="core server を起動 (Unix socket。Windowsは未実装)")
    L("gui", None, None, local=_gui, help="GUI を起動")
    return cli


def _abs(path: str | None) -> str | None:
    return None if path is None else str(Path(path).expanduser().resolve())


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _data_dir(a: argparse.Namespace) -> Path:
    if a.data_dir:
        return Path(a.data_dir)
    base = default_data_dir()
    return base.parent / a.org if a.org else base


# ------------------------------------------------------------------ local command

def _schema_show(a: argparse.Namespace, core: Core) -> dict:
    spec = REGISTRY.get(a.name)
    if spec is None:
        raise SystemExit(_print_local_error(a, "UNKNOWN_COMMAND", f"未知のcommandです: {a.name}", EXIT_INPUT))
    return {"command": spec.name, "summary": spec.summary, "doc": (spec.handler.__doc__ or "").strip(),
            "min_profile": spec.min_profile, "mutating": spec.mutating, "capability_scope": spec.capability_scope}


def _ai_status(a: argparse.Namespace, core: Core) -> dict:
    from ..agents import bridge
    from ..application import config as app_config
    try:
        return bridge.doctor(bridge.AgentPolicy.from_config(app_config.load(core.paths)["ai"]))
    except bridge.AgentError as exc:
        return {"ok": False, "error": {"code": exc.code, "message": exc.message, **exc.details}}


def _worker_run(a: argparse.Namespace, core: Core) -> Any:
    from ..application.worker import Worker
    worker = Worker(core, cli_session(reader=False))
    return worker.drain() if a.drain else worker.run_once()


def _core_serve(a: argparse.Namespace, core: Core) -> dict:
    from ..infrastructure.ipc.server import CoreServer, NotSupportedOnPlatform
    try:
        server = CoreServer(core, lambda reader: cli_session(reader=reader), APP_VERSION)
    except NotSupportedOnPlatform as exc:
        raise SystemExit(_print_local_error(a, "NOT_SUPPORTED", str(exc), EXIT_UNSUPPORTED))
    print(f"core serving on {server.server_address}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return {"stopped": True}


def _gui(a: argparse.Namespace, core: Core) -> dict:
    from ..gui.app import run
    core.close()
    raise SystemExit(run(_data_dir(a)))


def _print_local_error(a: argparse.Namespace, code: str, message: str, exit_code: int) -> int:
    resp = {"protocol_version": PROTOCOL_VERSION, "request_id": a.request_id or new_id(), "ok": False, "data": None,
            "warnings": [], "error": {"code": code, "message": message, "details": None}, "retryable": False,
            "observed_revision": None}
    _emit(a, resp)
    return exit_code


# ------------------------------------------------------------------ 実行

def _load_input(a: argparse.Namespace) -> dict:
    if not a.input:
        return {}
    if a.input == "-":
        if a.capability_stdin:
            raise ValueError("--input - と --capability-stdin は同時に使えません")
        text = sys.stdin.read()
    else:
        text = Path(a.input).read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("--input は JSON object です")
    return data


def _emit(a: argparse.Namespace, resp: dict) -> None:
    if a.json or a.format == "json":
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        return
    for w in resp.get("warnings") or []:
        print(f"注意 [{w['code']}]: {w['message']}", file=sys.stderr)
    if resp["ok"]:
        print(json.dumps(resp["data"], ensure_ascii=False, indent=2))
    else:
        err = resp["error"]
        print(f"エラー [{err['code']}]: {err['message']}", file=sys.stderr)
        if err.get("details"):
            print(json.dumps(err["details"], ensure_ascii=False, indent=2), file=sys.stderr)


def _utf8_streams() -> None:
    """Windows の pipe でも JSON の日本語を落とさないよう stdout/stderr を UTF-8 にする。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    cli = build()
    a = cli.parser.parse_args(argv)
    if not getattr(a, "group", None) or (hasattr(a, "action") and a.action is None and a.core_command is None
                                         and a.local is None):
        cli.parser.print_help(sys.stderr)
        return EXIT_INPUT
    if a.request_id:
        try:
            uuid.UUID(a.request_id)
        except ValueError:
            return _print_local_error(a, "INVALID_INPUT", "--request-id は UUID です", EXIT_INPUT)
    try:
        payload = _load_input(a) | {k: v for k, v in a.builder(a).items() if v is not None}
    except (ValueError, OSError) as exc:
        return _print_local_error(a, "INVALID_INPUT", str(exc), EXIT_INPUT)
    token = None
    if a.capability_stdin:
        token = sys.stdin.readline().strip()
        if not token:
            return _print_local_error(a, "INVALID_INPUT", "capability token が stdin にありません", EXIT_INPUT)
    data_dir = _data_dir(a)
    core = Core(data_dir)
    try:
        if a.local is not None:
            data = a.local(a, core)
            resp = {"protocol_version": PROTOCOL_VERSION, "request_id": a.request_id or new_id(), "ok": True,
                    "data": data, "warnings": [], "error": None, "retryable": False, "observed_revision": None}
            if isinstance(data, dict) and data.get("ok") is False and data.get("error"):
                resp.update(ok=False, data=None, error={"code": data["error"].get("code", "ERROR"),
                                                        "message": data["error"].get("message", ""),
                                                        "details": data["error"]})
            _emit(a, resp)
            return 0 if resp["ok"] else EXIT_UNSUPPORTED
        kwargs = {"request_id": a.request_id, "idempotency_key": a.idempotency_key,
                  "expected_revision": a.expected_revision, "dry_run": a.dry_run, "capability_token": token}
        if a.via_core:
            from ..infrastructure.ipc.server import CoreClient
            client = CoreClient(data_dir, reader=a.reader, timeout=a.timeout_seconds)
            try:
                resp = client.execute(a.core_command, payload, **kwargs)
            finally:
                client.close()
        else:
            resp = core.execute(a.core_command, payload, cli_session(reader=a.reader), **kwargs)
        _emit(a, resp)
        return exit_code_for(resp)
    finally:
        core.close()


if __name__ == "__main__":
    sys.exit(main())
