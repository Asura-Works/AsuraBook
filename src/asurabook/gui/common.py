"""GUI共通部品: core呼び出し、非同期実行、表モデル、金額表示。

GUIはSQLiteへ直接接続せず core (Application Service) へ依頼する。widget更新はGUI threadに限る。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from ..application.core import Core
from ..application.session import Session


def yen(value: Any) -> str:
    if value is None:
        return "不明"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:  # 内部例外は種別だけを表示する
            self.signals.failed.emit(type(exc).__name__)
            return
        self.signals.done.emit(result)


class AppContext(QObject):
    """画面間で共有する状態。year は上部の年度選択。"""

    changed = Signal()  # 帳簿や状態が変わったとき各画面が再読込する

    def __init__(self, core: Core, session: Session, parent: QWidget | None = None):
        super().__init__()
        self.core = core
        self.session = session
        self.parent_widget = parent
        self.year: int | None = None
        self._tasks: list[_Task] = []

    def raw(self, name: str, payload: dict | None = None, **kw: Any) -> dict:
        return self.core.execute(name, payload or {}, self.session, **kw)

    def call(self, name: str, payload: dict | None = None, *, quiet: bool = False, **kw: Any) -> Any:
        resp = self.raw(name, payload, **kw)
        if not resp["ok"]:
            if not quiet:
                self.show_error(resp["error"])
            return None
        for w in resp.get("warnings") or []:
            if w["code"] not in ("IDEMPOTENT_REPLAY",) and not quiet:
                QMessageBox.information(self.parent_widget, "注意", f"[{w['code']}] {w['message']}")
        return resp["data"]

    def show_error(self, error: dict) -> None:
        details = error.get("details") or {}
        issues = details.get("issues") if isinstance(details, dict) else None
        lines = [f"{error['message']}  [{error['code']}]"]
        if issues:
            lines.append("")
            lines += [f"・{i.get('message')}" + (f" (行{i['line_no']})" if i.get("line_no") else "") for i in issues]
        elif details:
            lines.append("")
            lines.append(json.dumps(details, ensure_ascii=False, indent=1)[:800])
        lines.append("")
        lines.append("この操作は保存されていません。内容を確認して再度実行してください。")
        QMessageBox.warning(self.parent_widget, "操作できませんでした", "\n".join(lines))

    def run_async(self, fn: Callable[[], Any], on_done: Callable[[Any], None],
                  on_failed: Callable[[str], None] | None = None) -> None:
        task = _Task(fn)
        self._tasks.append(task)

        def done(result: Any) -> None:
            self._tasks.remove(task)
            on_done(result)

        def failed(name: str) -> None:
            self._tasks.remove(task)
            if on_failed:
                on_failed(name)
            else:
                QMessageBox.warning(self.parent_widget, "処理に失敗しました", f"内部エラー ({name})")

        task.signals.done.connect(done)
        task.signals.failed.connect(failed)
        QThreadPool.globalInstance().start(task)

    @property
    def running_tasks(self) -> int:
        return len(self._tasks)


class DictTableModel(QAbstractTableModel):
    """dict 行の表。金額列は右寄せ・桁区切り。行番号ではなく行の id を更新対象に使う。"""

    def __init__(self, columns: list[tuple[str, str]], money: set[str] | None = None,
                 formatters: dict[str, Callable[[dict], str]] | None = None):
        super().__init__()
        self.columns = columns
        self.money = money or set()
        self.formatters = formatters or {}
        self.rows: list[dict] = []

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def row(self, index: int) -> dict | None:
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return len(self.columns)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.columns[section][1]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        key = self.columns[index.column()][0]
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            if key in self.formatters:
                return self.formatters[key](row)
            value = row.get(key)
            if key in self.money:
                return yen(value)
            return "" if value is None else str(value)
        if role == Qt.ItemDataRole.TextAlignmentRole and key in self.money:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None
