"""仕訳の純粋な検証ロジック (04章)。

DB・Qt・CLIに依存しない。確定に必要な条件:
2行以上、借方合計=貸方合計、正の円整数、税額状態の整合。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .values import content_hash

SIDES = ("debit", "credit")
TAX_STATUSES = ("unknown", "known", "not_applicable")


@dataclass
class Line:
    line_no: int
    account_id: str
    side: str
    amount_jpy: int
    subaccount_id: str | None = None
    tax_code: str | None = None
    tax_amount_jpy: int | None = None
    tax_amount_status: str = "unknown"
    business_ratio: Decimal | None = None
    counterparty_id: str | None = None
    description: str = ""

    def fingerprint(self) -> dict[str, Any]:
        return {
            "line_no": self.line_no,
            "account_id": self.account_id,
            "subaccount_id": self.subaccount_id,
            "side": self.side,
            "amount_jpy": self.amount_jpy,
            "tax_code": self.tax_code,
            "tax_amount_jpy": self.tax_amount_jpy,
            "tax_amount_status": self.tax_amount_status,
            "business_ratio": None if self.business_ratio is None else str(self.business_ratio),
            "counterparty_id": self.counterparty_id,
            "description": self.description,
        }


@dataclass
class Issue:
    code: str
    message: str
    line_no: int | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.line_no is not None:
            d["line_no"] = self.line_no
        if self.data:
            d.update(self.data)
        return d


def totals(lines: list[Line]) -> tuple[int, int]:
    debit = sum(l.amount_jpy for l in lines if l.side == "debit")
    credit = sum(l.amount_jpy for l in lines if l.side == "credit")
    return debit, credit


def structural_issues(lines: list[Line]) -> list[Issue]:
    """下書き段階でも検出できる構造上の問題。貸借不一致は下書きを妨げない。"""
    issues: list[Issue] = []
    if len(lines) < 2:
        issues.append(Issue("TOO_FEW_LINES", "仕訳は2行以上が必要です"))
    debit, credit = totals(lines)
    if debit != credit:
        issues.append(Issue("UNBALANCED", f"借方合計 {debit:,} と貸方合計 {credit:,} が一致しません",
                            data={"debit_total": debit, "credit_total": credit, "difference": debit - credit}))
    for line in lines:
        if line.side not in SIDES:
            issues.append(Issue("INVALID_SIDE", "side は debit/credit です", line.line_no))
        if not isinstance(line.amount_jpy, int) or line.amount_jpy <= 0:
            issues.append(Issue("INVALID_AMOUNT", "金額は正の円整数です", line.line_no))
        if line.tax_amount_status not in TAX_STATUSES:
            issues.append(Issue("INVALID_TAX_STATUS", "税額状態が不正です", line.line_no))
        elif line.tax_amount_status == "known":
            if line.tax_amount_jpy is None or line.tax_amount_jpy < 0:
                issues.append(Issue("TAX_AMOUNT_REQUIRED", "確認済み税額は0以上の円整数です", line.line_no))
        elif line.tax_amount_jpy is not None:
            issues.append(Issue("TAX_AMOUNT_STATUS_MISMATCH",
                                "税額が不明/対象外のときは税額を空欄にしてください", line.line_no))
    return issues


def validation_hash(journal_date: str, description: str, counterparty_id: str | None,
                    fiscal_year_id: str, kind: str, lines: list[Line],
                    source_links: list[dict[str, Any]] | None = None) -> str:
    return content_hash({
        "journal_date": journal_date,
        "description": description,
        "counterparty_id": counterparty_id,
        "fiscal_year_id": fiscal_year_id,
        "kind": kind,
        "lines": [l.fingerprint() for l in sorted(lines, key=lambda x: x.line_no)],
        "source_links": sorted(source_links or [], key=lambda x: (x["line_no"], x["event_id"], x["role"])),
    })


def reversed_side(side: str) -> str:
    return "credit" if side == "debit" else "debit"
