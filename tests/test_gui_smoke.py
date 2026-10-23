"""GUI の起動と主要操作の smoke test (offscreen)。Windows 実機・DPI・IME の検証ではない。"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from asurabook.application.core import Core  # noqa: E402
from asurabook.application.session import gui_owner_session  # noqa: E402


pytestmark = pytest.mark.gui


@pytest.fixture
def app():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app
    # Qt object を QApplication より先に確実に破棄する (終了時の segfault を防ぐ)
    from PySide6.QtCore import QEvent, QThreadPool
    import gc
    QThreadPool.globalInstance().waitForDone()
    for w in QtWidgets.QApplication.topLevelWidgets():
        w.close()
        w.deleteLater()
    QtWidgets.QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    gc.collect()


def test_main_window_pages_and_journal_editor(app, tmp_path, monkeypatch):
    from asurabook.gui.app import MainWindow
    from asurabook.gui.journal_editor import JournalEditor
    core = Core(tmp_path / "d")
    assert core.execute("org.init", {"display_name": "架空", "years": [2026]}, gui_owner_session())["ok"]
    window = MainWindow(core)
    for i in range(window.nav.count()):
        window.nav.setCurrentRow(i)  # 全画面が例外なく表示・再読込できる
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", staticmethod(lambda *a, **k: None))
    editor = JournalEditor(window.ctx, preset={"journal_date": "2026-10-05", "description": "文具", "lines": [
        {"account": "519", "side": "debit", "amount_jpy": 11000},
        {"account": "210", "side": "credit", "amount_jpy": 10000}]})
    assert "不一致" in editor.diff.text()
    assert editor.save()["status"] == "draft"  # 不一致でも下書き保存できる
    assert editor.validate() is None  # 確定条件を満たさない
    editor.table.cellWidget(1, 2).setText("11000")
    assert "一致" in editor.diff.text()
    editor.post()
    assert editor.entry["status"] == "posted"
    assert not editor.btn_save.isEnabled()  # posted は編集保存させない
    window.nav.setCurrentRow(1)
    journal_page = window.pages[1]
    assert journal_page.model.rowCount() == 1
    window.nav.setCurrentRow(0)
    assert window.pages[0].labels["expense_jpy"].text() == "11,000 円"
    checks = next(p for p in window.pages if p.title == "申告準備チェック")
    checks.run_check()
    assert checks.steps.rowCount() == 11 and checks.model.rowCount() > 0
    tax = next(p for p in window.pages if p.title == "税務設定")
    window.nav.setCurrentRow(window.pages.index(tax))
    tax.inputs["filing_type"].setCurrentIndex(tax.inputs["filing_type"].findData("blue"))
    tax.save()
    assert core.execute("tax.profile_show", {"year": 2026}, window.ctx.session)["data"]["current"]["version"] == 1
    editor.close()
    window.close()
    core.close()
