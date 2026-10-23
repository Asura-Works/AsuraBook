"""仕訳編集 (UI04)。貸借差額を常時表示し、不一致でも下書きは保存できる。確定は別操作。"""

from __future__ import annotations

import json

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QComboBox, QDateEdit, QDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QVBoxLayout)

from .common import AppContext, yen

SIDES = [("debit", "借方"), ("credit", "貸方")]
TAX = [("unknown", "税額不明"), ("known", "税額確認済"), ("not_applicable", "対象外")]


class JournalEditor(QDialog):
    def __init__(self, ctx: AppContext, entry: dict | None = None, *, preset: dict | None = None, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.entry = entry
        self.setWindowTitle("仕訳編集")
        self.resize(900, 480)
        self.accounts = ctx.call("accounts.list", quiet=True) or []
        self.posted = bool(entry and entry["status"] == "posted")

        self.date = QDateEdit(calendarPopup=True)
        self.date.setDisplayFormat("yyyy-MM-dd")
        self.description = QLineEdit()
        form = QFormLayout()
        form.addRow("日付", self.date)
        form.addRow("摘要", self.description)
        self.status_label = QLabel()
        form.addRow("状態", self.status_label)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["科目", "借貸", "金額(円)", "税額状態", "税額", "行摘要"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.diff = QLabel()

        buttons = QHBoxLayout()
        self.btn_add = QPushButton("行追加")
        self.btn_del = QPushButton("行削除")
        self.btn_save = QPushButton("下書き保存 (Ctrl+S)")
        self.btn_validate = QPushButton("検証")
        self.btn_post = QPushButton("確定")
        self.btn_reverse = QPushButton("訂正 (取消)")
        for b in (self.btn_add, self.btn_del, self.btn_save, self.btn_validate, self.btn_post, self.btn_reverse):
            buttons.addWidget(b)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.table)
        layout.addWidget(self.diff)
        layout.addLayout(buttons)

        self.btn_add.clicked.connect(lambda: self.add_line())
        self.btn_del.clicked.connect(self.remove_line)
        self.btn_save.clicked.connect(self.save)
        self.btn_validate.clicked.connect(self.validate)
        self.btn_post.clicked.connect(self.post)
        self.btn_reverse.clicked.connect(self.reverse)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.save)

        self.evidence_ids: list[str] = list((preset or {}).get("evidence_document_ids", []))
        if entry:
            self._load(entry)
        else:
            d = QDate.fromString((preset or {}).get("journal_date", ""), "yyyy-MM-dd")
            self.date.setDate(d if d.isValid() else QDate.currentDate())
            self.description.setText((preset or {}).get("description", ""))
            for line in (preset or {}).get("lines", []) or [{}, {}]:
                self.add_line(line)
        self._apply_mode()
        self.update_diff()

    # -------------------------------------------------------- 行

    def add_line(self, line: dict | None = None) -> None:
        line = line or {}
        r = self.table.rowCount()
        self.table.insertRow(r)
        acc = QComboBox()
        acc.setEditable(False)
        for a in self.accounts:
            if a["active"]:
                acc.addItem(f"{a['code']} {a['name']}", a["code"])
        if line.get("account"):
            i = acc.findData(line["account"])
            acc.setCurrentIndex(max(i, 0))
        # 取込明細から作った行は原資料リンク (利用枠) を保持する
        acc.setProperty("source_links", json.dumps(line.get("source_links") or []))
        side = QComboBox()
        for v, label in SIDES:
            side.addItem(label, v)
        side.setCurrentIndex(0 if line.get("side", "debit" if r % 2 == 0 else "credit") == "debit" else 1)
        amount = QLineEdit(str(line.get("amount_jpy", "")))
        amount.setAlignment(Qt.AlignmentFlag.AlignRight)
        tax = QComboBox()
        for v, label in TAX:
            tax.addItem(label, v)
        tax.setCurrentIndex([t[0] for t in TAX].index(line.get("tax_amount_status", "unknown")))
        tax_amount = QLineEdit("" if line.get("tax_amount_jpy") is None else str(line["tax_amount_jpy"]))
        desc = QLineEdit(line.get("description", ""))
        for c, w in enumerate((acc, side, amount, tax, tax_amount, desc)):
            self.table.setCellWidget(r, c, w)
        amount.textChanged.connect(self.update_diff)
        side.currentIndexChanged.connect(self.update_diff)

    def remove_line(self) -> None:
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)
            self.update_diff()

    def lines(self) -> list[dict]:
        out = []
        for r in range(self.table.rowCount()):
            w = [self.table.cellWidget(r, c) for c in range(6)]
            text = w[2].text().replace(",", "").strip()
            line = {"account": w[0].currentData(), "side": w[1].currentData(),
                    "amount_jpy": int(text) if text.isdigit() else text,
                    "tax_amount_status": w[3].currentData(), "description": w[5].text()}
            links = json.loads(w[0].property("source_links") or "[]")
            if links:
                line["source_links"] = links
            tax_text = w[4].text().replace(",", "").strip()
            if tax_text:
                line["tax_amount_jpy"] = int(tax_text) if tax_text.isdigit() else tax_text
            out.append(line)
        return out

    def update_diff(self) -> None:
        debit = credit = 0
        for l in self.lines():
            if isinstance(l["amount_jpy"], int):
                if l["side"] == "debit":
                    debit += l["amount_jpy"]
                else:
                    credit += l["amount_jpy"]
        diff = debit - credit
        state = "一致" if diff == 0 else "不一致 (下書きは保存できますが確定できません)"
        self.diff.setText(f"借方合計 {yen(debit)} / 貸方合計 {yen(credit)} / 差額 {yen(diff)} … {state}")

    # -------------------------------------------------------- 操作

    def _payload(self) -> dict:
        p = {"journal_date": self.date.date().toString("yyyy-MM-dd"), "description": self.description.text(),
             "lines": self.lines()}
        if self.evidence_ids:
            p["evidence_document_ids"] = self.evidence_ids
        return p

    def _load(self, entry: dict) -> None:
        self.entry = entry
        self.date.setDate(QDate.fromString(entry["journal_date"], "yyyy-MM-dd"))
        self.description.setText(entry["description"])
        self.table.setRowCount(0)
        for l in entry["lines"]:
            links = [{"event_id": s["event_id"], "role": s["role"], "amount_jpy": s["signed_effect_jpy"]}
                     for s in l.get("source_links", []) if s["role"] in ("recognition", "settlement")]
            self.add_line({"account": l["account_code"], "side": l["side"], "amount_jpy": l["amount_jpy"],
                           "tax_amount_status": l["tax_amount_status"], "tax_amount_jpy": l["tax_amount_jpy"],
                           "description": l["description"], "source_links": links})
        self.posted = entry["status"] == "posted"
        self._apply_mode()

    def _apply_mode(self) -> None:
        status = {"draft": "下書き", "validated": "検証済み", "posted": "確定"}.get(
            self.entry["status"], self.entry["status"]) if self.entry else "新規"
        number = f" No.{self.entry['number']}" if self.entry and self.entry.get("number") else ""
        extra = " (取消済)" if self.entry and self.entry.get("reversed") else ""
        self.status_label.setText(f"{status}{number}{extra}")
        for w in (self.date, self.description, self.table, self.btn_add, self.btn_del, self.btn_save,
                  self.btn_validate, self.btn_post):
            w.setEnabled(not self.posted)
        self.btn_reverse.setVisible(self.posted)
        self.btn_reverse.setEnabled(self.posted and not (self.entry or {}).get("reversed")
                                    and (self.entry or {}).get("kind") not in ("reversal", "opening",
                                                                              "opening_adjustment"))

    def save(self) -> dict | None:
        if self.posted:
            return None
        if self.entry is None:
            data = self.ctx.call("journal.draft", self._payload())
        else:
            data = self.ctx.call("journal.update", {"id": self.entry["id"], **self._payload()},
                                 expected_revision=self.entry["revision"])
        if data:
            self._load(data)
            self.ctx.changed.emit()
        return data

    def validate(self) -> dict | None:
        if self.save() is None:
            return None
        data = self.ctx.call("journal.validate", {"id": self.entry["id"]}, expected_revision=self.entry["revision"])
        if data:
            self._load(data)
            self.ctx.changed.emit()
        return data

    def post(self) -> None:
        if self.entry is None or self.entry["status"] != "validated":
            if self.validate() is None:
                return
        e = self.entry
        answer = QMessageBox.question(
            self, "確定", f"{e['journal_date']} 借方 {yen(e['debit_total_jpy'])} / 貸方 {yen(e['credit_total_jpy'])}\n"
                          f"{e['description']}\n\n確定すると内容は変更できず、訂正は取消+置換になります。確定しますか？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        data = self.ctx.call("journal.post", {"id": e["id"]}, expected_revision=e["revision"])
        if data:
            self._load(data["posted"][0])
            self.ctx.changed.emit()

    def reverse(self) -> None:
        reason, ok = QInputDialog.getText(self, "訂正", "訂正理由 (必須)")
        if not ok or not reason.strip():
            return
        data = self.ctx.call("journal.reverse", {"id": self.entry["id"], "reason": reason.strip()},
                             expected_revision=self.entry["revision"])
        if data:
            QMessageBox.information(self, "訂正", "完全反対仕訳を確定しました。正しい内容は新規仕訳 (置換) で入力してください。")
            self._load(self.ctx.call("journal.show", {"id": self.entry["id"]}))
            self.ctx.changed.emit()
