"""申告準備チェック・税務設定の画面 (10章 UI12/UI13、11章1節、14章5節)。"""

from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QVBoxLayout, QWidget)
from PySide6.QtCore import Qt

from .common import AppContext, DictTableModel
from .pages import Page, _selected, _table

SEVERITY = {"error": "要対応", "warning": "要確認", "info": "参考"}
STATUS = {"open": "未読", "acknowledged": "既読", "resolved": "解消"}
VALUE_LABELS = {
    "unknown": "未確認", "blue": "青色", "white": "白色", "approved": "承認済み", "applied": "申請中",
    "not_applied": "未申請", "not_applicable": "対象外", "double_entry": "複式簿記", "simple": "簡易簿記",
    "business": "事業所得", "miscellaneous": "雑所得", "taxable": "課税事業者", "exempt": "免税事業者",
    "general": "一般課税", "simplified": "簡易課税", "two_tenths": "2割特例", "yes": "あり", "no": "なし",
    "tax_inclusive": "税込経理", "tax_exclusive": "税抜経理",
}


class ChecksPage(Page):
    title = "申告準備チェック"

    def __init__(self, ctx: AppContext):
        super().__init__(ctx)
        self.steps = DictTableModel([("step", "工程"), ("state", "状態"), ("basis", "根拠")])
        self.model = DictTableModel(
            [("severity", "重要度"), ("topic", "工程"), ("message", "内容"), ("next_action", "次の操作"),
             ("status", "状態"), ("stale", "")],
            formatters={"severity": lambda r: SEVERITY.get(r["severity"], r["severity"]),
                        "status": lambda r: STATUS.get(r["status"], r["status"]),
                        "stale": lambda r: "帳簿変更後・要再点検" if r.get("stale") else ""})
        self.view = _table(self.model)
        self.summary = QLabel()
        run = QPushButton("再点検")
        run.clicked.connect(self.run_check)
        ack = QPushButton("既読にする (解消にはなりません)")
        ack.clicked.connect(self.ack)
        row = QHBoxLayout()
        row.addWidget(run)
        row.addWidget(ack)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(_table(self.steps))
        split.addWidget(self.view)
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(split)
        layout.addLayout(row)

    def refresh(self) -> None:
        if self.ctx.year is None:
            return
        data = self.ctx.call("monitor.findings", {"year": self.ctx.year}, quiet=True)
        if data:
            self.model.set_rows(data["findings"])
            if not self.steps.rows:
                self.summary.setText("「再点検」で申告準備の進捗を確認します (帳簿データだけを使う決定的検査です)")

    def run_check(self) -> None:
        data = self.ctx.call("monitor.check", {"year": self.ctx.year})
        if data:
            self.steps.set_rows(data["readiness"])
            self.model.set_rows(data["findings"])
            ready = "提出準備完了" if data["ready_for_filing"] else "提出準備は未完了"
            self.summary.setText(f"{data['year']}年度: {ready} / 新規 {data['stats']['created']} 件・"
                                 f"解消 {data['stats']['resolved']} 件 (revision {data['ledger_revision']})")

    def ack(self) -> None:
        for r in _selected(self.view, self.model):
            if r["status"] == "open":
                self.ctx.call("monitor.ack", {"id": r["id"]})
        self.refresh()


class TaxProfilePage(Page):
    title = "税務設定"

    def __init__(self, ctx: AppContext):
        super().__init__(ctx)
        self.form = QFormLayout()
        self.inputs: dict[str, QWidget] = {}
        self.version = 0
        holder = QWidget()
        holder.setLayout(self.form)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        self.unknown = QPlainTextEdit(readOnly=True)
        self.unknown.setMaximumHeight(120)
        self.confirm = QCheckBox("確認済みとして保存する (未確認の項目が残る場合は未確認のまま)")
        save = QPushButton("新しい版として保存")
        save.clicked.connect(self.save)
        copy = QPushButton("前年度から複写")
        copy.clicked.connect(self.copy_previous)
        row = QHBoxLayout()
        row.addWidget(self.confirm)
        row.addWidget(save)
        row.addWidget(copy)
        self.header = QLabel()
        layout = QVBoxLayout(self)
        layout.addWidget(self.header)
        layout.addWidget(scroll)
        layout.addWidget(QLabel("未確認の項目"))
        layout.addWidget(self.unknown)
        layout.addLayout(row)
        self._built = False

    def _build(self, data: dict) -> None:
        labels = data["labels"]
        for path, allowed in data["allowed"].items():
            label = labels.get(path, {"invoice.registration_number": "インボイス登録番号",
                                      "invoice.registered_from": "登録日", "invoice.registered_until": "登録失効日",
                                      "business_start_date": "開業日", "notes": "メモ"}.get(path, path))
            if allowed:
                w = QComboBox()
                for v in allowed:
                    w.addItem(VALUE_LABELS.get(v, v), v)
            else:
                w = QLineEdit()
                if path.endswith("_from") or path.endswith("_until") or path.endswith("_date"):
                    w.setPlaceholderText("YYYY-MM-DD")
            self.inputs[path] = w
            self.form.addRow(label, w)
        self._built = True

    def refresh(self) -> None:
        if self.ctx.year is None:
            return
        data = self.ctx.call("tax.profile_show", {"year": self.ctx.year}, quiet=True)
        if not data:
            return
        if not self._built:
            self._build(data)
        current = data["current"]
        profile = current["profile"] if current else data["template"]
        self.version = current["version"] if current else 0
        for path, w in self.inputs.items():
            value = profile
            for part in path.split("."):
                value = (value or {}).get(part)
            if isinstance(w, QComboBox):
                w.setCurrentIndex(max(0, w.findData(value)))
            else:
                w.setText(value or "")
        state = "未登録" if not current else f"v{current['version']} ({'確認済み' if current['status'] == 'confirmed' else '未確認'})"
        self.header.setText(f"{data['year']}年度の税務設定: {state}。年度ごとに版を残し、過去年度は変更しません。")
        self.unknown.setPlainText("\n".join(f"・{u['label']}" for u in data["unknown_fields"]) or "なし")

    def _profile(self) -> dict:
        out: dict = {}
        for path, w in self.inputs.items():
            value = w.currentData() if isinstance(w, QComboBox) else (w.text().strip() or None)
            if value is None:
                continue
            cur = out
            parts = path.split(".")
            for part in parts[:-1]:
                cur = cur.setdefault(part, {})
            cur[parts[-1]] = value
        return out

    def save(self) -> None:
        data = self.ctx.call("tax.profile_set", {"year": self.ctx.year, "profile": self._profile(),
                                                 "confirm": self.confirm.isChecked()},
                             expected_revision=self.version)
        if data:
            QMessageBox.information(self, "税務設定", f"v{data['version']} を保存しました "
                                    f"({'確認済み' if data['status'] == 'confirmed' else '未確認'})")
            self.ctx.changed.emit()

    def copy_previous(self) -> None:
        data = self.ctx.call("tax.profile_set", {"year": self.ctx.year, "copy_from_year": self.ctx.year - 1},
                             expected_revision=self.version)
        if data:
            self.ctx.changed.emit()
