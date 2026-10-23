"""AsuraBook GUI entry point (10章1節)。

左navigation、上部の事業者/年度、中央workspace、下部の状態表示。
この entry point の session だけが owner になる。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QMainWindow, QMessageBox, QStackedWidget, QVBoxLayout, QWidget)

from ..application import commands  # noqa: F401  command登録
from ..application.core import Core
from ..application.session import gui_owner_session
from .common import AppContext
from .pages import (BackupPage, EvidencePage, HomePage, ImportPage, JobsPage, JournalPage, Page, ReconcilePage,
                    ReportsPage, SettingsPage, YearEndPage)
from .pages_extra import ChecksPage, TaxProfilePage

ALL_PAGES = (HomePage, JournalPage, ImportPage, EvidencePage, ReconcilePage, ReportsPage, ChecksPage, YearEndPage,
             TaxProfilePage, JobsPage, BackupPage, SettingsPage)


class SetupDialog(QDialog):
    """UI01 初期設定 (最小): 事業者名と年度。税務属性は未確定のまま保留できる。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AsuraBook 初期設定")
        self.name = QLineEdit()
        self.years = QLineEdit("2026")
        form = QFormLayout(self)
        form.addRow("事業者名 (表示名)", self.name)
        form.addRow("年度 (カンマ区切り)", self.years)
        form.addRow(QLabel("青色申告・消費税・インボイス等の税務属性は後で確認して設定します。"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class MainWindow(QMainWindow):
    def __init__(self, core: Core):
        super().__init__()
        self.setWindowTitle("AsuraBook")
        self.resize(1280, 800)
        self.ctx = AppContext(core, gui_owner_session(), self)
        org = self.ctx.call("doctor", quiet=True) or {}
        self.year_box = QComboBox()
        years = self.ctx.call("year.list", quiet=True) or []
        for y in years:
            self.year_box.addItem(f"{y['year']}年度 ({y['workflow_state']})", y["year"])
        if years:
            self.ctx.year = years[-1]["year"]
            self.year_box.setCurrentIndex(len(years) - 1)
        self.year_box.currentIndexChanged.connect(self._year_changed)
        header = QHBoxLayout()
        header.addWidget(QLabel(f"データ: {org.get('data_dir', '')}"))
        header.addStretch(1)
        header.addWidget(QLabel("年度"))
        header.addWidget(self.year_box)

        self.nav = QListWidget()
        self.nav.setFixedWidth(160)
        self.stack = QStackedWidget()
        self.pages: list[Page] = []
        for cls in ALL_PAGES:
            page = cls(self.ctx)
            self.pages.append(page)
            self.nav.addItem(cls.title)
            self.stack.addWidget(page)
        self.nav.currentRowChanged.connect(self._switch)
        body = QHBoxLayout()
        body.addWidget(self.nav)
        body.addWidget(self.stack, 1)
        root = QVBoxLayout()
        root.addLayout(header)
        root.addLayout(body)
        container = QWidget()
        container.setLayout(root)
        self.setCentralWidget(container)
        self.ctx.changed.connect(self._refresh_current)
        self.nav.setCurrentRow(0)

    def _year_changed(self) -> None:
        self.ctx.year = self.year_box.currentData()
        self._refresh_current()

    def _switch(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self._refresh_current()

    def _refresh_current(self) -> None:
        page = self.stack.currentWidget()
        if isinstance(page, Page):
            page.refresh()
        self._update_status()

    def _update_status(self) -> None:
        summary = self.ctx.call("dashboard.summary", {"year": self.ctx.year}, quiet=True) if self.ctx.year else None
        rc = self.ctx.core.runtime()
        last = self.ctx.core.paths.root / "last_backup.json"
        last_backup = json.loads(last.read_text(encoding="utf-8"))["created_at"][:19] if last.exists() else "未実施"
        parts = ["core: 同一process"]
        if summary:
            parts.append(f"revision {summary['meta']['ledger_revision']}")
            parts.append(f"未同期原本 {summary['counts']['evidence_unsynced']}")
        parts.append(f"実行中job {self.ctx.running_tasks}")
        parts.append(f"最終backup {last_backup}")
        if rc.recovery_hold:
            parts.append(f"復元後の保留中: {', '.join(rc.held_scopes)}")
        self.statusBar().showMessage(" | ".join(parts))


def run(data_dir: Path) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    core = Core(data_dir)
    if not core.db_exists():
        dialog = SetupDialog()
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.name.text().strip():
            return 1
        years = [int(y) for y in dialog.years.text().replace(" ", "").split(",") if y.isdigit()]
        resp = core.execute("org.init", {"display_name": dialog.name.text().strip(), "years": years},
                            gui_owner_session())
        if not resp["ok"]:
            QMessageBox.critical(None, "初期設定", resp["error"]["message"])
            return 1
    window = MainWindow(core)
    window.show()
    code = app.exec()
    # 実行中の worker を待ち、Qt object を QApplication より先に破棄してから DB を閉じる
    QThreadPool.globalInstance().waitForDone()
    window.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    core.close()
    return code


def main() -> None:
    from ..infrastructure.paths import default_data_dir
    sys.exit(run(default_data_dir()))
