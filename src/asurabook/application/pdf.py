"""帳票の PDF 出力 (14章6節)。Qt の QTextDocument + QPdfWriter で、日本語fontの表を A4 に描く。

CSV と同じ表データを受け取り、対象期間・基準日・作成時刻・revision・未確定の含有有無を付記する。
PySide6 (gui extra) が必要。
"""

from __future__ import annotations

import html
import os
import sys
import tempfile
from pathlib import Path

from ..domain.errors import AsuraBookError

FONT_FAMILIES = ["Yu Gothic UI", "Yu Gothic", "Meiryo", "MS Gothic", "IPAGothic", "IPAexGothic",
                 "Noto Sans CJK JP", "Noto Sans JP"]


def _app():
    try:
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtWidgets import QApplication
    except ImportError:
        raise AsuraBookError("NOT_SUPPORTED", "PDF出力には PySide6 が必要です (uv sync --extra gui)") from None
    app = QCoreApplication.instance()
    if app is None:
        if sys.platform not in ("win32", "darwin") and not os.environ.get("DISPLAY") \
                and not os.environ.get("WAYLAND_DISPLAY"):
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        # GUI と同じ process で使っても壊れないよう Widgets 対応の QApplication を作る
        app = QApplication([])
    return app


def _font():
    from PySide6.QtGui import QFont, QFontDatabase
    available = set(QFontDatabase.families())
    for family in FONT_FAMILIES:
        if family in available:
            return QFont(family, 9), family
    return QFont(), None


def _cell(value, money: bool) -> str:
    if value is None:
        return ""
    if money and isinstance(value, int):
        return f"{value:,}"
    return html.escape(str(value))


def build_html(title: str, org: str, header: list[str], rows: list[list], money: set[int], meta: dict) -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for h in header)
    body = []
    for r in rows:
        tds = []
        for i, v in enumerate(r):
            align = ' align="right"' if i in money else ""
            tds.append(f"<td{align}>{_cell(v, i in money)}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    period = f"{meta.get('date_from') or '期首'} 〜 {meta.get('date_to') or '期末'}"
    notes = [
        f"事業者: {html.escape(org)}",
        f"対象: {meta['year']}年度 ({period})" + (f" / 科目: {html.escape(meta['account'])}" if meta.get("account") else ""),
        f"作成: {meta['generated_at']} / 帳簿revision: {meta['ledger_revision']} / 年度状態: {meta['workflow_state']}",
        f"確定仕訳だけを集計 (未確定 {meta['unposted_entries_excluded']} 件は含まない) / 金額単位: 円 / 税務ruleは未適用",
    ]
    return (
        "<html><body>"
        f"<h2>{html.escape(title)}</h2>"
        + "".join(f"<p style='margin:0'>{n}</p>" for n in notes)
        + "<br/><table border='1' cellspacing='0' cellpadding='3' width='100%'>"
        f"<thead><tr style='background:#e8e8e8'>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
        "</body></html>"
    )


def render_table_pdf(title: str, org: str, header: list[str], rows: list[list], money: set[int],
                     meta: dict) -> tuple[bytes, int]:
    _app()
    from PySide6.QtCore import QMarginsF, QSizeF
    from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter, QTextDocument

    font, family = _font()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "report.pdf"
        writer = QPdfWriter(str(out))
        writer.setTitle(title)
        writer.setCreator("AsuraBook")
        writer.setResolution(300)
        writer.setPageLayout(QPageLayout(QPageSize(QPageSize.PageSizeId.A4), QPageLayout.Orientation.Portrait,
                                         QMarginsF(12, 12, 12, 12), QPageLayout.Unit.Millimeter))
        doc = QTextDocument()
        # font の point size を PDF の解像度で解釈させる (既定の画面DPIだと文字が極端に小さくなる)
        doc.documentLayout().setPaintDevice(writer)
        doc.setDefaultFont(font)
        doc.setHtml(build_html(title, org, header, rows, money, meta))
        layout = writer.pageLayout()
        rect = layout.paintRectPixels(writer.resolution())
        doc.setPageSize(QSizeF(rect.width(), rect.height()))
        pages = doc.pageCount()
        doc.print_(writer)
        del writer
        data = out.read_bytes()
    if not data.startswith(b"%PDF"):
        raise AsuraBookError("INTERNAL_ERROR", "PDF の生成に失敗しました")
    return data, pages
