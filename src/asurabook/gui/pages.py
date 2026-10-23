"""PySide6 画面 (10章の UI02〜UI15 のうち M1 範囲)。"""

from __future__ import annotations

import getpass
import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QMessageBox,
                               QPlainTextEdit, QPushButton, QSplitter, QTableView, QVBoxLayout, QWidget)

from ..application.worker import Worker
from .common import AppContext, DictTableModel, yen
from .journal_editor import JournalEditor


def _table(model: DictTableModel) -> QTableView:
    view = QTableView()
    view.setModel(model)
    view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
    view.horizontalHeader().setStretchLastSection(True)
    view.verticalHeader().setVisible(False)
    view.setAlternatingRowColors(True)
    return view


def _selected(view: QTableView, model: DictTableModel) -> list[dict]:
    rows = sorted({i.row() for i in view.selectionModel().selectedRows()})
    return [model.row(r) for r in rows if model.row(r) is not None]


class Page(QWidget):
    title = ""

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx

    def refresh(self) -> None:
        pass


# ------------------------------------------------------------------ UI02 ホーム

class HomePage(Page):
    title = "ホーム"

    def __init__(self, ctx: AppContext):
        super().__init__(ctx)
        self.grid = QGridLayout()
        self.labels: dict[str, QLabel] = {}
        items = [("revenue_jpy", "売上高"), ("expense_jpy", "費用"), ("book_profit_jpy", "帳簿利益 (青色控除前)"),
                 ("cash_and_deposits_jpy", "現預金 (帳簿)"), ("receivable_open_jpy", "未回収"),
                 ("payable_open_jpy", "未払 (カード等)")]
        for i, (key, label) in enumerate(items):
            box = QGroupBox(label)
            v = QVBoxLayout(box)
            value = QLabel("-")
            value.setAlignment(Qt.AlignmentFlag.AlignRight)
            value.setStyleSheet("font-size: 20px;")
            v.addWidget(value)
            self.labels[key] = value
            self.grid.addWidget(box, i // 3, i % 3)
        self.counts = QPlainTextEdit(readOnly=True)
        self.meta = QLabel()
        layout = QVBoxLayout(self)
        layout.addWidget(self.meta)
        layout.addLayout(self.grid)
        layout.addWidget(QLabel("未処理"))
        layout.addWidget(self.counts)

    def refresh(self) -> None:
        if self.ctx.year is None:
            return
        data = self.ctx.call("dashboard.summary", {"year": self.ctx.year}, quiet=True)
        if not data:
            return
        for key, label in self.labels.items():
            label.setText(f"{yen(data[key])} 円")
        meta = data["meta"]
        self.meta.setText(f"{meta['year']}年度 ({meta['workflow_state']}) 基準日 {meta['as_of']} / "
                          f"帳簿revision {meta['ledger_revision']} / 確定仕訳のみ集計")
        names = {"draft_entries": "下書き仕訳", "validated_entries": "検証済み (未確定)", "evidence_unsynced": "クラウド未同期の原本",
                 "ocr_needs_review": "読取り結果の確認待ち", "import_issues": "取込の要確認事項",
                 "unlinked_source_events": "未記帳の取込明細", "open_findings": "未対応の指摘"}
        self.counts.setPlainText("\n".join(f"{names[k]}: {v} 件" for k, v in data["counts"].items())
                                 + "\n\n" + "\n".join(data["notes"]))


# ------------------------------------------------------------------ UI03 日次仕訳表

STATUS_LABEL = {"draft": "下書き", "validated": "検証済み", "posted": "確定"}


def _accounts_summary(row: dict, side: str) -> str:
    names = [f"{l['account_name']}" for l in row.get("lines", []) if l["side"] == side]
    return " / ".join(names)


class JournalPage(Page):
    title = "仕訳"

    def __init__(self, ctx: AppContext):
        super().__init__(ctx)
        self.model = DictTableModel(
            [("journal_date", "日付"), ("number", "番号"), ("status", "状態"), ("debit", "借方"), ("credit", "貸方"),
             ("debit_total_jpy", "金額"), ("description", "摘要"), ("flags", "訂正")],
            money={"debit_total_jpy"},
            formatters={"status": lambda r: STATUS_LABEL.get(r["status"], r["status"]),
                        "debit": lambda r: _accounts_summary(r, "debit"),
                        "credit": lambda r: _accounts_summary(r, "credit"),
                        "flags": lambda r: ("取消済" if r.get("reversed") else
                                            {"reversal": "取消仕訳", "replacement": "置換"}.get(r["kind"], ""))})
        self.view = _table(self.model)
        self.view.doubleClicked.connect(lambda _: self.open_selected())
        self.search = QLineEdit(placeholderText="摘要で検索 (Ctrl+F)")
        self.status = QComboBox()
        for v, label in ((None, "全状態"), ("draft", "下書き"), ("validated", "検証済み"), ("posted", "確定")):
            self.status.addItem(label, v)
        self.search.returnPressed.connect(self.refresh)
        self.status.currentIndexChanged.connect(self.refresh)
        bar = QHBoxLayout()
        bar.addWidget(self.search)
        bar.addWidget(self.status)
        buttons = QHBoxLayout()
        for label, slot in (("新規 (Ctrl+N)", self.new), ("開く", self.open_selected), ("検証", self.validate_selected),
                            ("確定", self.post_selected), ("CLI/AIへ確定を委任", self.delegate),
                            ("下書き破棄", self.discard), ("再読込", self.refresh)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.view)
        layout.addLayout(buttons)
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self.new)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self.search.setFocus)

    def refresh(self) -> None:
        if self.ctx.year is None:
            return
        payload = {"year": self.ctx.year, "limit": 1000}
        if self.search.text():
            payload["text"] = self.search.text()
        if self.status.currentData():
            payload["status"] = self.status.currentData()
        data = self.ctx.call("journal.list", payload, quiet=True)
        self.model.set_rows(data["items"] if data else [])

    def new(self) -> None:
        JournalEditor(self.ctx, parent=self).exec()
        self.refresh()

    def open_selected(self) -> None:
        rows = _selected(self.view, self.model)
        if rows:
            entry = self.ctx.call("journal.show", {"id": rows[0]["id"]})
            if entry:
                JournalEditor(self.ctx, entry, parent=self).exec()
                self.refresh()

    def validate_selected(self) -> None:
        for r in _selected(self.view, self.model):
            if r["status"] == "draft":
                self.ctx.call("journal.validate", {"id": r["id"]}, expected_revision=r["revision"])
        self.ctx.changed.emit()

    def post_selected(self) -> None:
        rows = [r for r in _selected(self.view, self.model) if r["status"] == "validated"]
        if not rows:
            QMessageBox.information(self, "確定", "検証済みの仕訳を選択してください")
            return
        total = sum(r["debit_total_jpy"] for r in rows)
        if QMessageBox.question(self, "確定", f"{len(rows)}件 (借方合計 {yen(total)} 円) を確定します。"
                                f"確定後は変更できません。よろしいですか？") != QMessageBox.StandardButton.Yes:
            return
        self.ctx.call("journal.post", {"ids": [r["id"] for r in rows]})
        self.ctx.changed.emit()

    def delegate(self) -> None:
        """人が確認した検証済み仕訳だけに、CLI/AI 用の一回用 capability を発行する。"""
        rows = [r for r in _selected(self.view, self.model) if r["status"] == "validated"]
        if not rows:
            QMessageBox.information(self, "委任", "検証済みの仕訳を選択してください")
            return
        principal = f"local:{getpass.getuser()}:cli"
        summary = "\n".join(f"{r['journal_date']} {yen(r['debit_total_jpy'])}円 {r['description']}" for r in rows)
        if QMessageBox.question(self, "確定の委任", f"次の{len(rows)}件の確定を {principal} に1回だけ許可します。\n\n"
                                f"{summary}\n\n内容が変わった場合は無効になります。発行しますか？"
                                ) != QMessageBox.StandardButton.Yes:
            return
        cap = self.ctx.call("capability.issue", {
            "scope": "journal.post", "principal": principal, "ttl_seconds": 600,
            "targets": [{"id": r["id"], "revision": r["revision"], "validation_hash": r["validation_hash"]}
                        for r in rows]})
        if cap:
            ids = " ".join(r["id"] for r in rows)
            QInputDialog.getMultiLineText(
                self, "capability (10分で失効)",
                "CLI で次のように stdin から渡してください。token はこの画面にだけ表示されます。",
                f"echo {cap['token']} | asurabook journal post {ids} --capability-stdin --json")

    def discard(self) -> None:
        for r in _selected(self.view, self.model):
            if r["status"] != "posted" and QMessageBox.question(
                    self, "破棄", f"{r['journal_date']} {r['description']} の下書きを破棄しますか？"
            ) == QMessageBox.StandardButton.Yes:
                self.ctx.call("journal.discard", {"id": r["id"]}, expected_revision=r["revision"])
        self.ctx.changed.emit()


# ------------------------------------------------------------------ UI05 取込受信箱

class ImportPage(Page):
    title = "取込"

    def __init__(self, ctx: AppContext):
        super().__init__(ctx)
        self.path = QLineEdit()
        browse = QPushButton("ファイル選択")
        browse.clicked.connect(self.browse)
        self.profile = QComboBox()
        self.profile.addItem("汎用CSV v1", "generic-v1")
        self.account = QComboBox()
        top = QHBoxLayout()
        for w in (QLabel("原本"), self.path, browse, QLabel("形式"), self.profile, QLabel("取込先"), self.account):
            top.addWidget(w)
        inspect = QPushButton("解析 (プレビュー)")
        inspect.clicked.connect(self.inspect)
        stage = QPushButton("取込 (原本保存と候補登録)")
        stage.clicked.connect(self.stage)
        add_acc = QPushButton("取込元アカウント追加")
        add_acc.clicked.connect(self.add_source_account)
        btns = QHBoxLayout()
        for b in (inspect, stage, add_acc):
            btns.addWidget(b)
        self.model = DictTableModel([("locator", "位置"), ("row_id", "row_id"), ("event_type", "種別"),
                                     ("date", "日付"), ("amount_jpy", "金額"), ("description", "摘要"),
                                     ("held", "保留")], money={"amount_jpy"})
        self.issues = QPlainTextEdit(readOnly=True)
        self.events_model = DictTableModel([("transaction_date", "日付"), ("source_account_code", "取込元"),
                                            ("event_type", "種別"), ("amount_jpy", "金額"), ("role", "会計役割"),
                                            ("used_jpy", "記帳済"), ("description", "摘要")],
                                           money={"amount_jpy", "used_jpy"})
        self.events_view = _table(self.events_model)
        to_journal = QPushButton("選択明細から仕訳案を作る")
        to_journal.clicked.connect(self.draft_from_event)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(btns)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(_table(self.model))
        split.addWidget(self.issues)
        split.addWidget(self.events_view)
        layout.addWidget(split)
        layout.addWidget(to_journal)

    def refresh(self) -> None:
        current = self.account.currentData()
        self.account.clear()
        for sa in self.ctx.call("source_accounts.list", quiet=True) or []:
            self.account.addItem(f"{sa['code']} {sa['display_name']}", sa["code"])
        if current:
            self.account.setCurrentIndex(max(0, self.account.findData(current)))
        self.events_model.set_rows(self.ctx.call("import.events", quiet=True) or [])

    def browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "取込ファイル", "", "CSV (*.csv);;すべて (*)")
        if path:
            self.path.setText(path)

    def inspect(self) -> None:
        data = self.ctx.call("import.inspect", {"path": self.path.text(), "profile": self.profile.currentData(),
                                                "account": self.account.currentData()})
        if data:
            self.model.set_rows(data["rows"])
            s = data["summary"]
            self.issues.setPlainText(
                f"文字コード {s['encoding']} / 行数 {s['total_rows']} / 採用 {s['accepted_rows']} / 保留 {s['held_rows']} / "
                f"期間 {s['period_start']}〜{s['period_end']}\n" +
                "\n".join(f"[{i['severity']}] {i['locator'] or ''} {i['code']}: {i['message']}" for i in data["issues"]))

    def stage(self) -> None:
        data = self.ctx.call("import.stage", {"path": self.path.text(), "profile": self.profile.currentData(),
                                              "account": self.account.currentData()})
        if data:
            QMessageBox.information(self, "取込", f"新規 {data.get('new_events', 0)} 件 / 既知 {data.get('known_events', 0)} 件 / "
                                    f"保留 {data.get('held_rows', 0)} 件\n取込は確認待ちの候補登録で、仕訳の確定ではありません。")
            self.ctx.changed.emit()

    def add_source_account(self) -> None:
        code, ok = QInputDialog.getText(self, "取込元", "コード (例 BANK-A)")
        if not ok or not code:
            return
        name, ok = QInputDialog.getText(self, "取込元", "表示名")
        if not ok:
            return
        kind, ok = QInputDialog.getItem(self, "取込元", "種類", ["bank", "card", "platform", "other"], 0, False)
        if not ok:
            return
        account, ok = QInputDialog.getText(self, "取込元", "帳簿科目コード (例 110 普通預金 / 210 未払金 / 120 売掛金)")
        if ok:
            self.ctx.call("source_accounts.create", {"code": code, "provider": code, "kind": kind,
                                                     "display_name": name or code, "account": account})
            self.refresh()

    def draft_from_event(self) -> None:
        rows = _selected(self.events_view, self.events_model)
        if not rows:
            return
        ev = rows[0]
        accounts = {a["id"]: a for a in self.ctx.call("accounts.list", quiet=True) or []}
        sas = {s["code"]: s for s in self.ctx.call("source_accounts.list", quiet=True) or []}
        own = accounts.get(sas.get(ev["source_account_code"], {}).get("account_id"), {}).get("code")
        amount = abs(ev["amount_jpy"] or 0)
        link = [{"event_id": ev["id"], "role": ev["role"]}] if ev.get("role") else []
        inflow = (ev["amount_jpy"] or 0) > 0 and ev["event_type"].startswith("bank")
        own_line = {"account": own, "side": "debit" if inflow else "credit", "amount_jpy": amount,
                    "source_links": link, "description": ev["description"]}
        other_line = {"side": "credit" if inflow else "debit", "amount_jpy": amount}
        preset = {"journal_date": ev["transaction_date"], "description": ev["description"],
                  "lines": [own_line, other_line] if inflow else [other_line, own_line]}
        # 相手科目は利用者が選ぶ (入金を自動で売上にしない)。原資料リンクは自分側の行に付ける
        editor = JournalEditor(self.ctx, preset=preset, parent=self)
        editor.exec()
        self.ctx.changed.emit()


# ------------------------------------------------------------------ UI08 証憑確認

class EvidencePage(Page):
    title = "証憑"

    def __init__(self, ctx: AppContext):
        super().__init__(ctx)
        self.model = DictTableModel([("created_at", "受付"), ("receipt_kind", "区分"), ("mime", "形式"),
                                     ("ocr_state", "読取"), ("sync_state", "保管"), ("link_count", "関連仕訳")])
        self.view = _table(self.model)
        self.view.clicked.connect(lambda _: self.show_selected())
        self.image = QLabel("証憑を選択してください")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumSize(320, 400)
        self.date = QLineEdit()
        self.amount = QLineEdit()
        self.counterparty = QLineEdit()
        self.issues = QPlainTextEdit(readOnly=True)
        self.candidates = QComboBox()
        form = QFormLayout()
        form.addRow("取引日", self.date)
        form.addRow("金額 (円)", self.amount)
        form.addRow("取引先", self.counterparty)
        form.addRow("既存仕訳候補", self.candidates)
        right = QVBoxLayout()
        right.addLayout(form)
        right.addWidget(QLabel("要確認事項 (読取り結果は候補です)"))
        right.addWidget(self.issues)
        btns = QGridLayout()
        for i, (label, slot) in enumerate((("証憑を追加", self.add), ("Claudeで読取り", self.read),
                                           ("確認値を保存", self.review), ("既存仕訳へ関連付け", self.link),
                                           ("新規仕訳案", self.new_entry), ("再読込", self.refresh))):
            b = QPushButton(label)
            b.clicked.connect(slot)
            btns.addWidget(b, i // 3, i % 3)
        right.addLayout(btns)
        rw = QWidget()
        rw.setLayout(right)
        split = QSplitter()
        split.addWidget(self.view)
        split.addWidget(self.image)
        split.addWidget(rw)
        layout = QVBoxLayout(self)
        layout.addWidget(split)
        self.current: dict | None = None

    def refresh(self) -> None:
        self.model.set_rows(self.ctx.call("evidence.list", quiet=True) or [])

    def add(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "証憑", "", "画像/PDF (*.jpg *.jpeg *.png *.pdf *.heic);;すべて (*)")
        for p in paths:
            self.ctx.call("evidence.add", {"path": p})
        self.ctx.changed.emit()

    def show_selected(self) -> None:
        rows = _selected(self.view, self.model)
        if not rows:
            return
        doc = self.ctx.call("evidence.show", {"document_id": rows[0]["id"]})
        if not doc:
            return
        self.current = doc
        original = doc["objects"][0]
        path = self.ctx.core.paths.spool / original["local_ref"]
        pix = QPixmap(str(path)) if original["mime"].startswith("image/") else QPixmap()
        if pix.isNull():
            self.image.setText(f"{original['mime']} (プレビュー非対応)")
        else:
            self.image.setPixmap(pix.scaled(self.image.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                            Qt.TransformationMode.SmoothTransformation))
        current_index = doc["index"][-1]["current"] if doc["index"] else None
        job = doc["ocr_jobs"][-1] if doc["ocr_jobs"] else None
        ext = (job or {}).get("extraction") or {}
        source = current_index or {"business_date": ext.get("transaction_date"), "amount_jpy": ext.get("total_jpy"),
                                   "counterparty": ext.get("merchant_name")}
        self.date.setText(source.get("business_date") or "")
        self.amount.setText("" if source.get("amount_jpy") is None else str(source["amount_jpy"]))
        self.counterparty.setText(source.get("counterparty") or "")
        issues = (job or {}).get("validation") or []
        state = f"読取状態: {(job or {}).get('state', '未実行')}"
        if current_index:
            state += f" / 索引 v{current_index['version']} ({current_index['status']})"
        self.issues.setPlainText(state + "\n" + "\n".join(f"・{i['message']}" for i in issues))
        self.candidates.clear()
        if self.date.text() and self.amount.text().lstrip("-").isdigit():
            for c in self.ctx.call("evidence.match_candidates", {"amount_jpy": int(self.amount.text()),
                                                                 "business_date": self.date.text()}, quiet=True) or []:
                self.candidates.addItem(f"{c['journal_date']} No.{c['number']} {c['account_name']} "
                                        f"{yen(c['amount_jpy'])} {c['description']}", c["id"])

    def read(self) -> None:
        if not self.current:
            return
        if not self.ctx.call("evidence.read", {"document_id": self.current["id"]}):
            return
        worker = Worker(self.ctx.core, self.ctx.session)
        self.issues.setPlainText("Claude で読取り中… (画像はモデル提供先へ送信されます)")
        self.ctx.run_async(worker.run_ocr_once, lambda result: (self.ctx.changed.emit(), self.show_selected()))

    def review(self) -> None:
        if not self.current:
            return
        amount = self.amount.text().replace(",", "").strip()
        values = {"business_date": self.date.text() or None,
                  "amount_jpy": int(amount) if amount.lstrip("-").isdigit() else None,
                  "counterparty": self.counterparty.text() or None}
        job = self.current["ocr_jobs"][-1] if self.current["ocr_jobs"] else None
        payload = {"document_id": self.current["id"], "values": values}
        if self.current["index"]:
            payload["index_row_id"] = self.current["index"][-1]["index_row_id"]
        if job:
            payload["ocr_job_id"] = job["id"]
        if self.ctx.call("evidence.review", payload):
            self.ctx.changed.emit()
            self.show_selected()

    def link(self) -> None:
        if self.current and self.candidates.currentData():
            if self.ctx.call("evidence.link", {"document_id": self.current["id"],
                                               "entry_id": self.candidates.currentData()}):
                QMessageBox.information(self, "関連付け", "証憑を既存仕訳に関連付けました (経費は再計上しません)")
                self.ctx.changed.emit()

    def new_entry(self) -> None:
        if not self.current:
            return
        amount = self.amount.text().replace(",", "").strip()
        amt = int(amount) if amount.isdigit() else ""
        preset = {"journal_date": self.date.text(), "description": self.counterparty.text(),
                  "evidence_document_ids": [self.current["id"]],
                  "lines": [{"account": "519", "side": "debit", "amount_jpy": amt},
                            {"account": "100", "side": "credit", "amount_jpy": amt}]}
        JournalEditor(self.ctx, preset=preset, parent=self).exec()
        self.ctx.changed.emit()


# ------------------------------------------------------------------ UI09 消込

class ReconcilePage(Page):
    title = "消込"

    def __init__(self, ctx: AppContext):
        super().__init__(ctx)
        self.model = DictTableModel([("journal_date", "決済日"), ("entry_number", "伝票"), ("description", "摘要"),
                                     ("unallocated_jpy", "未割当"), ("best", "候補 (未決済残)")],
                                    money={"unallocated_jpy"},
                                    formatters={"best": lambda r: ", ".join(
                                        f"{o['created_business_date']} {o['description']} 残{yen(o['balance_jpy'])}"
                                        + (" ★一致" if o["exact_match"] else "") for o in r["open_items"][:3])})
        self.view = _table(self.model)
        self.items_model = DictTableModel([("created_business_date", "発生日"), ("type", "種類"),
                                           ("account_name", "科目"), ("description", "摘要"),
                                           ("origin_amount_jpy", "元本"), ("balance_jpy", "残高")],
                                          money={"origin_amount_jpy", "balance_jpy"})
        self.as_of = QLineEdit(placeholderText="基準日 YYYY-MM-DD (空欄は全期間)")
        self.as_of.returnPressed.connect(self.refresh)
        apply_btn = QPushButton("第1候補へ割当")
        apply_btn.clicked.connect(self.apply)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("未割当の決済行"))
        layout.addWidget(self.view)
        layout.addWidget(apply_btn)
        layout.addWidget(self.as_of)
        layout.addWidget(_table(self.items_model))

    def refresh(self) -> None:
        data = self.ctx.call("reconcile.suggest", quiet=True)
        self.model.set_rows(data["candidates"] if data else [])
        payload = {"as_of": self.as_of.text()} if self.as_of.text() else {}
        items = self.ctx.call("openitems.list", payload, quiet=True)
        self.items_model.set_rows(items["items"] if items else [])

    def apply(self) -> None:
        for r in _selected(self.view, self.model):
            if not r["open_items"]:
                continue
            target = r["open_items"][0]
            amount = min(r["unallocated_jpy"], target["balance_jpy"])
            if QMessageBox.question(self, "消込", f"{r['description']} の {yen(amount)} 円を "
                                    f"{target['description']} (残 {yen(target['balance_jpy'])}) へ割り当てますか？"
                                    ) == QMessageBox.StandardButton.Yes:
                self.ctx.call("reconcile.apply", {"allocations": [{"open_item_id": target["open_item_id"],
                                                                   "settlement_line_id": r["settlement_line_id"],
                                                                   "amount_jpy": amount}]})
        self.ctx.changed.emit()


# ------------------------------------------------------------------ UI10 帳簿/帳票

class ReportsPage(Page):
    title = "帳簿・帳票"

    def __init__(self, ctx: AppContext):
        super().__init__(ctx)
        self.kind = QComboBox()
        for v, label in (("trial_balance", "残高試算表"), ("profit_loss", "損益計算書"), ("balance_sheet", "貸借対照表")):
            self.kind.addItem(label, v)
        self.kind.currentIndexChanged.connect(self.refresh)
        export = QPushButton("CSV出力")
        export.clicked.connect(self.export)
        self.model = DictTableModel([("section", "区分"), ("code", "コード"), ("name", "科目"),
                                     ("debit", "借方"), ("credit", "貸方"), ("amount", "金額")],
                                    money={"debit", "credit", "amount"})
        self.footer = QLabel()
        top = QHBoxLayout()
        top.addWidget(self.kind)
        top.addWidget(export)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(_table(self.model))
        layout.addWidget(self.footer)

    def refresh(self) -> None:
        if self.ctx.year is None:
            return
        kind = self.kind.currentData()
        data = self.ctx.call(f"report.{kind}", {"year": self.ctx.year}, quiet=True)
        if not data:
            return
        rows = []
        if kind == "trial_balance":
            rows = [{"section": r["type"], "code": r["code"], "name": r["name"], "debit": r["debit_balance_jpy"],
                     "credit": r["credit_balance_jpy"]} for r in data["rows"]]
            footer = f"借方合計 {yen(data['debit_total_jpy'])} / 貸方合計 {yen(data['credit_total_jpy'])} / " \
                     f"{'一致' if data['balanced'] else '不一致'}"
        elif kind == "profit_loss":
            rows = [{"section": "収益", **r, "amount": r["amount_jpy"]} for r in data["revenue"]] + \
                   [{"section": "費用", **r, "amount": r["amount_jpy"]} for r in data["expense"]]
            footer = f"帳簿利益 {yen(data['book_profit_jpy'])} 円 ({data['note']})"
        else:
            for section, key in (("資産", "assets"), ("負債", "liabilities"), ("資本", "equity")):
                rows += [{"section": section, **r, "amount": r["amount_jpy"]} for r in data[key]]
            footer = f"資産 {yen(data['total_assets_jpy'])} / 負債 {yen(data['total_liabilities_jpy'])} / " \
                     f"資本 {yen(data['total_equity_jpy'])} / 当期利益 {yen(data['book_profit_jpy'])}"
        self.model.set_rows(rows)
        meta = data["meta"]
        self.footer.setText(f"{footer}  [revision {meta['ledger_revision']}、未確定 {meta['unposted_entries_excluded']} 件は含まない]")

    def export(self) -> None:
        kind = self.kind.currentData()
        if kind == "balance_sheet":
            kind = "trial_balance"
        path, _ = QFileDialog.getSaveFileName(self, "CSV出力", str(self.ctx.core.paths.exports / f"{kind}.csv"), "CSV (*.csv)")
        if path:
            data = self.ctx.call("report.export", {"year": self.ctx.year, "report": kind, "path": path,
                                                   "export_root_override": str(Path(path).parent)})
            if data:
                QMessageBox.information(self, "出力", f"{data['written']} に {data['rows']} 行を出力しました")


# ------------------------------------------------------------------ UI11 決算

class YearEndPage(Page):
    title = "決算・繰越"

    def __init__(self, ctx: AppContext):
        super().__init__(ctx)
        self.text = QPlainTextEdit(readOnly=True)
        carry = QPushButton("翌期へ繰越 (期首仕訳/差額調整)")
        carry.clicked.connect(self.carryforward)
        snap = QPushButton("帳簿集計 snapshot を保存")
        snap.clicked.connect(self.snapshot)
        layout = QVBoxLayout(self)
        layout.addWidget(self.text)
        row = QHBoxLayout()
        row.addWidget(carry)
        row.addWidget(snap)
        layout.addLayout(row)

    def refresh(self) -> None:
        if self.ctx.year is None:
            return
        data = self.ctx.call("year.carryforward_status", {"from_year": self.ctx.year}, quiet=True)
        if data:
            p = data["projection"]
            self.text.setPlainText(
                f"{data['from_year']} → {data['to_year']} 繰越状態: {data['state']}\n\n"
                f"期首元入金 (現在の帳簿から投影): {yen(p['opening_capital_jpy'])} 円\n"
                f"= 元入金 {yen(p['capital_before_jpy'])} + 帳簿利益 {yen(p['book_profit_jpy'])} + 事業主借 "
                f"{yen(p['owner_contribution_net_jpy'])} − 事業主貸 {yen(p['owner_draw_net_jpy'])}\n\n"
                f"未反映の差額: {json.dumps(data.get('pending_diff') or {}, ensure_ascii=False)}\n\n"
                "税額計算・申告書作成は年版ruleの確認後に対応します (現在は未対応)。")

    def carryforward(self) -> None:
        if QMessageBox.question(self, "繰越", f"{self.ctx.year}年度の帳簿から翌期首の残高を作ります。よろしいですか？"
                                ) == QMessageBox.StandardButton.Yes:
            data = self.ctx.call("year.carryforward", {"from_year": self.ctx.year})
            if data:
                QMessageBox.information(self, "繰越", f"状態: {data['state']} {data.get('kind', '')}")
                self.ctx.changed.emit()

    def snapshot(self) -> None:
        data = self.ctx.call("tax.snapshot_create", {"year": self.ctx.year})
        if data:
            QMessageBox.information(self, "snapshot", f"保存しました: {data['snapshot_id']}")


# ------------------------------------------------------------------ UI14 実行履歴

class JobsPage(Page):
    title = "AI・実行履歴"

    def __init__(self, ctx: AppContext):
        super().__init__(ctx)
        self.model = DictTableModel([("created_at", "開始"), ("job_kind", "種類"), ("provider", "Provider"),
                                     ("model", "model"), ("status", "状態"), ("attempt", "試行"),
                                     ("error_code", "error"), ("error_message", "内容")])
        self.doctor = QPlainTextEdit(readOnly=True)
        run = QPushButton("待機中の処理を実行")
        run.clicked.connect(self.run_worker)
        check = QPushButton("Claude CLI を点検 (版・認証mode)")
        check.clicked.connect(self.check)
        row = QHBoxLayout()
        row.addWidget(run)
        row.addWidget(check)
        layout = QVBoxLayout(self)
        layout.addWidget(_table(self.model))
        layout.addLayout(row)
        layout.addWidget(self.doctor)

    def refresh(self) -> None:
        self.model.set_rows(self.ctx.call("ai.jobs", quiet=True) or [])

    def run_worker(self) -> None:
        worker = Worker(self.ctx.core, self.ctx.session)
        self.ctx.run_async(worker.drain, lambda r: (self.ctx.changed.emit(),
                                                    self.doctor.setPlainText(json.dumps(r, ensure_ascii=False, indent=1))))

    def check(self) -> None:
        from ..agents import bridge
        from ..application import config as app_config

        def doctor():
            try:
                return bridge.doctor(bridge.AgentPolicy.from_config(app_config.load(self.ctx.core.paths)["ai"]))
            except bridge.AgentError as exc:
                return {"ok": False, "error": {"code": exc.code, "message": exc.message}}
        self.doctor.setPlainText("点検中…")
        self.ctx.run_async(doctor, lambda r: self.doctor.setPlainText(json.dumps(r, ensure_ascii=False, indent=1)))


# ------------------------------------------------------------------ UI15 保全

class BackupPage(Page):
    title = "保全・復元"

    def __init__(self, ctx: AppContext):
        super().__init__(ctx)
        self.text = QPlainTextEdit(readOnly=True)
        grid = QGridLayout()
        for i, (label, slot) in enumerate((("backup 作成", self.create), ("backup 検査", self.verify),
                                           ("復元 (preview→実行)", self.restore), ("復旧状態", self.status),
                                           ("保留の解除", self.release))):
            b = QPushButton(label)
            b.clicked.connect(slot)
            grid.addWidget(b, 0, i)
        layout = QVBoxLayout(self)
        layout.addLayout(grid)
        layout.addWidget(self.text)

    def _show(self, data) -> None:
        if data is not None:
            self.text.setPlainText(json.dumps(data, ensure_ascii=False, indent=1))

    def create(self) -> None:
        dest = QFileDialog.getExistingDirectory(self, "backup 保存先 (データフォルダの外)")
        if dest:
            self.text.setPlainText("backup 作成中…")
            self.ctx.run_async(lambda: self.ctx.raw("backup.create", {"dest": dest}),
                               lambda r: self._show(r["data"] if r["ok"] else r["error"]))

    def verify(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "backup フォルダ")
        if path:
            self._show(self.ctx.call("backup.verify", {"path": path}))

    def restore(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "復元する backup フォルダ")
        if not path:
            return
        preview = self.ctx.call("restore.preview", {"path": path})
        self._show(preview)
        if not preview or not preview["ok"]:
            return
        if QMessageBox.question(self, "復元", "現在のDBを退避して backup で置換します。backup後の変更は失われる可能性があります。\n"
                                "復元後、外部処理 (upload・AI・申告) は確認するまで保留されます。実行しますか？"
                                ) == QMessageBox.StandardButton.Yes:
            self._show(self.ctx.call("restore.apply", {"path": path, "confirm": True}))
            self.ctx.changed.emit()

    def status(self) -> None:
        self._show(self.ctx.call("recovery.status"))

    def release(self) -> None:
        scope, ok = QInputDialog.getItem(self, "保留の解除", "scope", ["upload", "ai", "sync", "etax", "notify"], 0, False)
        if not ok:
            return
        reason, ok = QInputDialog.getText(self, "保留の解除", "確認した内容 (必須)")
        if ok and reason:
            self._show(self.ctx.call("recovery.release", {"scope": scope, "reason": reason}))


# ------------------------------------------------------------------ 設定

class SettingsPage(Page):
    title = "設定"

    def __init__(self, ctx: AppContext):
        super().__init__(ctx)
        self.text = QPlainTextEdit(readOnly=True)
        set_btn = QPushButton("設定を変更")
        set_btn.clicked.connect(self.set_value)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("AI と証憑保管の設定 (画像は AI 処理先へ、原本は保管先へ別々に送信されます)"))
        layout.addWidget(self.text)
        layout.addWidget(set_btn)

    def refresh(self) -> None:
        cfg = self.ctx.call("settings.show", quiet=True)
        self.text.setPlainText(json.dumps(cfg, ensure_ascii=False, indent=1))

    def set_value(self) -> None:
        keys = ["ai.exe", "ai.model", "ai.timeout_seconds", "ai.auto_read_receipts", "cloud.provider", "cloud.root"]
        key, ok = QInputDialog.getItem(self, "設定", "項目", keys, 0, False)
        if not ok:
            return
        raw, ok = QInputDialog.getText(self, "設定", f"{key} の値 (JSON。空欄は null)")
        if not ok:
            return
        try:
            value = json.loads(raw) if raw.strip() else None
        except ValueError:
            value = raw
        self.ctx.call("settings.set", {"key": key, "value": value})
        self.refresh()

