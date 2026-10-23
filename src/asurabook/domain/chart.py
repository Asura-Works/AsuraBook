"""初期勘定科目。個人事業・一般用青色決算書の科目を中心にした初期値。

科目コードと申告欄の対応は別の版管理とする (04章3節)。ここでは帳簿の科目だけを定める。
事業主貸・事業主借は元入金の繰越計算に使う役割 (role) を持つ。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccountSeed:
    code: str
    name: str
    type: str
    normal_side: str
    open_item_type: str | None = None
    role: str | None = None


DEFAULT_ACCOUNTS: tuple[AccountSeed, ...] = (
    AccountSeed("100", "現金", "asset", "debit"),
    AccountSeed("110", "普通預金", "asset", "debit"),
    AccountSeed("111", "定期預金", "asset", "debit"),
    AccountSeed("120", "売掛金", "asset", "debit", open_item_type="receivable"),
    AccountSeed("121", "未収入金", "asset", "debit", open_item_type="receivable"),
    AccountSeed("130", "前払金", "asset", "debit"),
    AccountSeed("131", "前払費用", "asset", "debit"),
    AccountSeed("140", "仮払金", "asset", "debit"),
    AccountSeed("150", "工具器具備品", "asset", "debit"),
    AccountSeed("151", "一括償却資産", "asset", "debit"),
    AccountSeed("200", "買掛金", "liability", "credit", open_item_type="payable"),
    AccountSeed("210", "未払金", "liability", "credit", open_item_type="payable"),
    AccountSeed("211", "未払費用", "liability", "credit"),
    AccountSeed("220", "預り金", "liability", "credit"),
    AccountSeed("230", "前受金", "liability", "credit"),
    AccountSeed("240", "借入金", "liability", "credit"),
    AccountSeed("300", "事業主貸", "equity", "debit", role="owner_draw"),
    AccountSeed("310", "事業主借", "equity", "credit", role="owner_contribution"),
    AccountSeed("320", "元入金", "equity", "credit", role="owner_capital"),
    AccountSeed("400", "売上高", "revenue", "credit"),
    AccountSeed("410", "雑収入", "revenue", "credit"),
    AccountSeed("500", "仕入高", "expense", "debit"),
    AccountSeed("510", "租税公課", "expense", "debit"),
    AccountSeed("511", "荷造運賃", "expense", "debit"),
    AccountSeed("512", "水道光熱費", "expense", "debit"),
    AccountSeed("513", "旅費交通費", "expense", "debit"),
    AccountSeed("514", "通信費", "expense", "debit"),
    AccountSeed("515", "広告宣伝費", "expense", "debit"),
    AccountSeed("516", "接待交際費", "expense", "debit"),
    AccountSeed("517", "損害保険料", "expense", "debit"),
    AccountSeed("518", "修繕費", "expense", "debit"),
    AccountSeed("519", "消耗品費", "expense", "debit"),
    AccountSeed("520", "減価償却費", "expense", "debit"),
    AccountSeed("521", "福利厚生費", "expense", "debit"),
    AccountSeed("522", "給料賃金", "expense", "debit"),
    AccountSeed("523", "外注工賃", "expense", "debit"),
    AccountSeed("524", "利子割引料", "expense", "debit"),
    AccountSeed("525", "地代家賃", "expense", "debit"),
    AccountSeed("526", "貸倒金", "expense", "debit"),
    AccountSeed("527", "支払手数料", "expense", "debit"),
    AccountSeed("528", "新聞図書費", "expense", "debit"),
    AccountSeed("529", "研修費", "expense", "debit"),
    AccountSeed("599", "雑費", "expense", "debit"),
)

# (科目コード, 補助コード, 名称)
DEFAULT_SUBACCOUNTS: tuple[tuple[str, str, str], ...] = (
    ("300", "WHT", "源泉所得税"),
)
