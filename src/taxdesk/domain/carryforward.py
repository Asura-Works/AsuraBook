"""元入金の繰越 (opening_projection 方式、11章11節)。

翌期首元入金 = 当期首元入金 + book_profit + 事業主借純増 − 事業主貸純増 + 明示した資本調整

資産・負債は残高のまま投影する。収益・費用・事業主貸・事業主借は翌期へ持ち越さず元入金へ整理する。
opening は残高投影であり、債権債務 (open item) を再生成しない。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccountBalance:
    account_id: str
    code: str
    name: str
    type: str
    role: str | None
    debit_total: int
    credit_total: int

    @property
    def debit_balance(self) -> int:
        """借方を正とした残高。"""
        return self.debit_total - self.credit_total


@dataclass(frozen=True)
class Projection:
    capital_account_id: str
    opening_capital: int  # 貸方正
    book_profit: int
    owner_contribution_net: int
    owner_draw_net: int
    capital_before: int
    # account_id -> 借方正の期首残高 (元入金を含む)
    balances: dict[str, int]

    def to_dict(self) -> dict:
        return {
            "capital_account_id": self.capital_account_id,
            "opening_capital_jpy": self.opening_capital,
            "book_profit_jpy": self.book_profit,
            "owner_contribution_net_jpy": self.owner_contribution_net,
            "owner_draw_net_jpy": self.owner_draw_net,
            "capital_before_jpy": self.capital_before,
            "balances": dict(sorted(self.balances.items())),
        }


class CarryforwardError(Exception):
    pass


def project_opening(balances: list[AccountBalance]) -> Projection:
    capital = [b for b in balances if b.role == "owner_capital"]
    if len(capital) != 1:
        raise CarryforwardError("元入金 (role=owner_capital) の科目がちょうど1つ必要です")
    capital_acc = capital[0]

    revenue = sum(-b.debit_balance for b in balances if b.type == "revenue")
    expense = sum(b.debit_balance for b in balances if b.type == "expense")
    book_profit = revenue - expense
    contribution = sum(-b.debit_balance for b in balances if b.role == "owner_contribution")
    draw = sum(b.debit_balance for b in balances if b.role == "owner_draw")
    capital_before = -capital_acc.debit_balance

    opening_capital = capital_before + book_profit + contribution - draw

    projected: dict[str, int] = {}
    for b in balances:
        if b.type in ("asset", "liability") or (b.type == "equity" and b.role is None):
            if b.debit_balance != 0:
                projected[b.account_id] = b.debit_balance
    if opening_capital != 0:
        projected[capital_acc.account_id] = -opening_capital

    if sum(projected.values()) != 0:
        raise CarryforwardError("投影した期首残高の貸借が一致しません。試算表を確認してください")

    return Projection(
        capital_account_id=capital_acc.account_id,
        opening_capital=opening_capital,
        book_profit=book_profit,
        owner_contribution_net=contribution,
        owner_draw_net=draw,
        capital_before=capital_before,
        balances=projected,
    )


def diff_balances(old: dict[str, int], new: dict[str, int]) -> dict[str, int]:
    """借方正の差分。前期訂正の翌期影響を差額調整で表す (11章12節)。"""
    keys = set(old) | set(new)
    diff = {k: new.get(k, 0) - old.get(k, 0) for k in keys}
    return {k: v for k, v in sorted(diff.items()) if v != 0}
