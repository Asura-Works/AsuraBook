"""減価償却と家事按分の計算 (11章5節、04章6節)。DB・税年ruleに依存しない純粋関数。

耐用年数・償却率・償却方法・特例の適用可否は利用者が公式資料から選んで入力する。
この計算は入力された条件を機械的に適用するだけで、税務上の適否を判定しない。
採用した端数処理と前提は calculation に残す。
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

MEMO_VALUE_JPY = 1  # 定額法で残す備忘価額 (前提として calculation に記録する)


def months_in_service(in_service: dt.date, year_start: dt.date, year_end: dt.date,
                      disposed: dt.date | None = None) -> int:
    """年度内で事業に供した月数 (供用月を1か月と数える)。"""
    start = max(in_service, year_start)
    end = min(year_end, disposed) if disposed else year_end
    if start > end:
        return 0
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def business_split(amount: int, ratio: Decimal) -> tuple[int, int]:
    """事業分を円未満切捨て、残りを私用分 (事業主貸) にする。"""
    business = int((Decimal(amount) * ratio).to_integral_value(rounding=ROUND_FLOOR))
    return business, amount - business


def depreciation_for_year(*, method: str, cost: int, rate: Decimal | None, in_service: dt.date,
                          year_start: dt.date, year_end: dt.date, accumulated_before: int,
                          years_already: int, disposed: dt.date | None = None) -> dict:
    months = months_in_service(in_service, year_start, year_end, disposed)
    book_before = cost - accumulated_before
    assumptions: list[str] = []
    if months == 0 or book_before <= 0:
        amount = 0
    elif method == "straight_line":
        if rate is None:
            raise ValueError("定額法には償却率が必要です")
        annual = (Decimal(cost) * rate).to_integral_value(rounding=ROUND_CEILING)
        amount = int((annual * months / 12).to_integral_value(rounding=ROUND_CEILING))
        amount = min(amount, book_before - MEMO_VALUE_JPY)
        amount = max(amount, 0)
        assumptions = ["年償却額 = 取得価額 × 償却率 (円未満切上げ)", "年度の償却額 = 年償却額 × 月数/12 (円未満切上げ)",
                       f"未償却残高が備忘価額 {MEMO_VALUE_JPY} 円を下回らないよう制限"]
    elif method == "lump_sum_3y":
        if years_already >= 3:
            amount = 0
        else:
            share = cost // 3
            amount = share if years_already < 2 else cost - share * 2
            amount = min(amount, book_before)
        assumptions = ["3年均等 (月数按分なし)。端数は3年目に配賦"]
    elif method == "immediate_small":
        amount = book_before if years_already == 0 else 0
        assumptions = ["供用年度に全額を費用化 (特例の適用可否は利用者が確認)"]
    else:
        raise ValueError(f"unknown method {method}")
    return {"months": months, "depreciation_jpy": amount, "book_value_before_jpy": book_before,
            "book_value_end_jpy": book_before - amount, "assumptions": assumptions}
