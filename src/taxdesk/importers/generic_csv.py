"""汎用CSV v1 の解析と正規化 (05章5節)。純粋関数で、DBや原本保存を行わない。

- 文字コードは UTF-8 / BOM付きUTF-8 / CP932。置換文字で通さず、判定不能なら ENCODING_AMBIGUOUS。
- 銀行 amount は入金正・出金負。カードは種別と正額。売上は gross と構成要素。
- 空欄は null (0 で埋めない)。異常行を黙って読み飛ばさず issue にする。
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

PARSER_VERSION = "generic_csv_v1/1.0"
PROFILE = {
    "provider": "generic",
    "data_kind": "csv",
    "version": "v1",
    "encodings": ["utf-8-sig", "utf-8", "cp932"],
    "sign_rule": "bank_credit>0, bank_debit<0, card/sale components positive",
}

REQUIRED = ("row_id", "source_account", "event_type", "currency")
KNOWN_COLUMNS = (
    "row_id", "source_account", "event_type", "currency",
    "transaction_date", "recognition_date", "settlement_date", "period_start", "period_end",
    "amount_jpy", "gross_jpy", "fee_jpy", "withholding_jpy", "refund_jpy", "net_jpy",
    "tax_status", "tax_rule_id", "tax_jpy", "invoice_registration_no", "business_ratio",
    "external_id", "parent_external_id", "order_id", "settlement_id", "statement_id", "original_event_id",
    "evidence_ref", "description",
)
EVENT_TYPES = {
    # event_type: (amount の符号規則, 会計役割)
    "bank_credit": ("positive", "settlement"),
    "bank_debit": ("negative", "settlement"),
    "card_charge": ("positive", "recognition"),
    "card_refund": ("positive", "recognition"),
    "card_payment": ("positive", "settlement"),
    "sale": ("positive", "recognition"),
    "fee": ("positive", "recognition"),
    "withholding": ("positive", "recognition"),
    "refund": ("positive", "recognition"),
    "payout": ("positive", "settlement"),
}
COMPONENT_FIELDS = (("gross_jpy", "gross"), ("fee_jpy", "fee"), ("withholding_jpy", "withholding"),
                    ("refund_jpy", "refund"), ("net_jpy", "net"))
_DATE_RE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$")
_INT_RE = re.compile(r"^-?\d{1,3}(,\d{3})*$|^-?\d+$")


@dataclass
class RowIssue:
    code: str
    severity: str
    locator: str | None
    field: str | None
    raw_masked: str | None
    message: str
    remedy: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class NormalizedRow:
    locator: str
    raw: dict[str, str]
    row_id: str
    source_account: str
    event_type: str
    currency: str
    transaction_date: str | None
    recognition_date: str | None
    settlement_date: str | None
    period_start: str | None
    period_end: str | None
    amount_jpy: int | None
    components: list[tuple[str, int]]
    tax_status: str
    tax_jpy: int | None
    external_id: str | None
    description: str
    grain: str
    role: str
    held: bool = False

    @property
    def primary_date(self) -> str | None:
        return self.transaction_date or self.recognition_date or self.settlement_date or self.period_end

    @property
    def capacity_jpy(self) -> int | None:
        if self.event_type == "sale":
            gross = dict(self.components).get("gross")
            return gross
        return None if self.amount_jpy is None else abs(self.amount_jpy)


@dataclass
class ParseResult:
    encoding: str | None
    header: list[str]
    rows: list[NormalizedRow] = field(default_factory=list)
    issues: list[RowIssue] = field(default_factory=list)
    total_rows: int = 0
    # 解析に失敗した行も含め、元位置と原値を観測として保存するため
    raw_rows: list[tuple[str, dict[str, str] | list[str]]] = field(default_factory=list)

    @property
    def fatal(self) -> bool:
        return any(i.severity == "error" and i.locator is None for i in self.issues)

    def summary(self) -> dict[str, Any]:
        accepted = [r for r in self.rows if not r.held]
        dates = sorted(d for d in (r.primary_date for r in accepted) if d)
        return {
            "encoding": self.encoding,
            "parser_version": PARSER_VERSION,
            "total_rows": self.total_rows,
            "accepted_rows": len(accepted),
            "held_rows": self.total_rows - len(accepted),
            "period_start": dates[0] if dates else None,
            "period_end": dates[-1] if dates else None,
            "amount_sum_jpy": sum(r.amount_jpy or 0 for r in accepted),
            "issues": len(self.issues),
        }


def decode(data: bytes) -> tuple[str | None, str | None, RowIssue | None]:
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return "utf-8-sig", data.decode("utf-8-sig"), None
        except UnicodeDecodeError:
            pass
    else:
        try:
            return "utf-8", data.decode("utf-8"), None
        except UnicodeDecodeError:
            pass
        try:
            return "cp932", data.decode("cp932"), None
        except UnicodeDecodeError:
            pass
    return None, None, RowIssue("ENCODING_AMBIGUOUS", "error", None, None, None,
                                "文字コードを UTF-8 / CP932 として解釈できません",
                                "銀行・サービスの画面から出力した元のファイルを指定してください")


def _mask(value: str) -> str:
    v = value.strip()
    if len(v) <= 4:
        return v
    return v[:2] + "…" + v[-2:]


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def _parse_int(value: str | None, field_name: str, locator: str, issues: list[RowIssue]) -> int | None:
    if value is None:
        return None
    v = _normalize_text(value).replace("−", "-").replace("ー", "-") if value else ""
    if v == "":
        return None
    if v in ("-",):
        issues.append(RowIssue("AMOUNT_MEANING_UNKNOWN", "needs_review", locator, field_name, v,
                               "金額欄が記号だけです。空欄と負数のどちらか判定できません"))
        raise ValueError
    if not _INT_RE.match(v):
        issues.append(RowIssue("AMOUNT_MEANING_UNKNOWN", "needs_review", locator, field_name, _mask(v),
                               "金額が円整数として解釈できません", "小数・通貨記号を含めないでください"))
        raise ValueError
    return int(v.replace(",", ""))


def _parse_date(value: str | None, field_name: str, locator: str, issues: list[RowIssue]) -> str | None:
    if value is None:
        return None
    v = _normalize_text(value)
    if v == "":
        return None
    m = _DATE_RE.match(v)
    if not m:
        issues.append(RowIssue("DATE_INVALID", "needs_review", locator, field_name, v,
                               "日付は YYYY-MM-DD 形式です (和暦は未対応)"))
        raise ValueError
    import datetime as dt
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        issues.append(RowIssue("DATE_INVALID", "needs_review", locator, field_name, v, "実在しない日付です"))
        raise


def parse(data: bytes, *, expected_account: str | None = None) -> ParseResult:
    encoding, text, issue = decode(data)
    if issue:
        return ParseResult(encoding=None, header=[], issues=[issue])
    assert text is not None
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration:
        return ParseResult(encoding, [], issues=[RowIssue("EMPTY_FILE", "error", None, None, None, "空のファイルです")])
    result = ParseResult(encoding=encoding, header=header)
    if len(set(header)) != len(header):
        result.issues.append(RowIssue("SCHEMA_CHANGED", "error", None, None, None, "列名が重複しています"))
        return result
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        result.issues.append(RowIssue("SCHEMA_CHANGED", "error", None, ",".join(missing), None,
                                      f"必須列がありません: {', '.join(missing)}"))
        return result
    unknown = [c for c in header if c not in KNOWN_COLUMNS]
    if unknown:
        result.issues.append(RowIssue("SCHEMA_CHANGED", "warning", None, ",".join(unknown), None,
                                      f"未知の列は解析に使いません: {', '.join(unknown)}"))
    seen_ids: dict[str, str] = {}
    for line_no, values in enumerate(reader, start=2):
        locator = f"row:{line_no}"
        if not any(v.strip() for v in values):
            continue
        result.total_rows += 1
        if len(values) != len(header):
            result.raw_rows.append((locator, values))
            result.issues.append(RowIssue("COLUMN_COUNT", "needs_review", locator, None, None,
                                          f"列数が見出し ({len(header)}) と一致しません ({len(values)})"))
            continue
        raw = dict(zip(header, values))
        result.raw_rows.append((locator, raw))
        row_issues: list[RowIssue] = []
        try:
            row = _normalize_row(raw, locator, row_issues, expected_account)
        except ValueError:
            row = None
        if row is not None and row.row_id in seen_ids:
            row_issues.append(RowIssue("DUPLICATE_ROW_ID", "needs_review", locator, "row_id", row.row_id,
                                       f"同じ row_id が {seen_ids[row.row_id]} にもあります"))
            row.held = True
        if row is not None:
            seen_ids.setdefault(row.row_id, locator)
            if any(i.severity in ("error", "needs_review") for i in row_issues):
                row.held = True
            result.rows.append(row)
        result.issues.extend(row_issues)
    return result


def _normalize_row(raw: dict[str, str], locator: str, issues: list[RowIssue],
                   expected_account: str | None) -> NormalizedRow | None:
    get = lambda k: raw.get(k)  # noqa: E731
    row_id = _normalize_text(get("row_id") or "")
    if not row_id:
        issues.append(RowIssue("ROW_ID_MISSING", "needs_review", locator, "row_id", None,
                               "row_id がありません。行番号で識別子を作りません"))
        return None
    account = _normalize_text(get("source_account") or "")
    event_type = _normalize_text(get("event_type") or "")
    currency = _normalize_text(get("currency") or "")
    if event_type not in EVENT_TYPES:
        issues.append(RowIssue("EVENT_TYPE_UNKNOWN", "needs_review", locator, "event_type", event_type,
                               "未知の event_type です"))
        raise ValueError
    sign_rule, role = EVENT_TYPES[event_type]
    dates = {k: _parse_date(get(k), k, locator, issues) for k in
             ("transaction_date", "recognition_date", "settlement_date", "period_start", "period_end")}
    amount = _parse_int(get("amount_jpy"), "amount_jpy", locator, issues)
    components = []
    for col, kind in COMPONENT_FIELDS:
        v = _parse_int(get(col), col, locator, issues)
        if v is not None:
            components.append((kind, v))
    tax_status = _normalize_text(get("tax_status") or "unknown") or "unknown"
    tax_jpy = _parse_int(get("tax_jpy"), "tax_jpy", locator, issues)
    held = False
    if expected_account is not None and account != expected_account:
        issues.append(RowIssue("ACCOUNT_MISMATCH", "needs_review", locator, "source_account", account,
                               f"取込先アカウント {expected_account} と行のアカウントが異なります"))
        held = True
    if currency != "JPY":
        issues.append(RowIssue("CURRENCY_NOT_JPY", "needs_review", locator, "currency", currency,
                               "外貨は円換算確定まで記帳を保留します"))
        held = True
    if tax_status not in ("unknown", "known", "not_applicable"):
        issues.append(RowIssue("TAX_STATUS_INVALID", "needs_review", locator, "tax_status", tax_status,
                               "tax_status は unknown/known/not_applicable です"))
        held = True
    elif tax_status != "known" and tax_jpy is not None:
        issues.append(RowIssue("TAX_STATUS_MISMATCH", "needs_review", locator, "tax_jpy", str(tax_jpy),
                               "税額が known 以外で指定されています"))
        held = True
    grain = "detail"
    if event_type == "sale":
        comp = dict(components)
        if "gross" not in comp:
            if "net" in comp:
                grain = "net_only"
                issues.append(RowIssue("NET_ONLY", "warning", locator, "gross_jpy", None,
                                       "net だけ判明しています。総売上の確定を保留します"))
            else:
                issues.append(RowIssue("AMOUNT_MEANING_UNKNOWN", "needs_review", locator, "gross_jpy", None,
                                       "売上の gross_jpy がありません"))
                held = True
    elif amount is None:
        issues.append(RowIssue("AMOUNT_MISSING", "needs_review", locator, "amount_jpy", None,
                               "amount_jpy が空欄です。0として扱いません"))
        held = True
    else:
        if sign_rule == "positive" and amount <= 0:
            issues.append(RowIssue("SIGN_INVALID", "needs_review", locator, "amount_jpy", str(amount),
                                   f"{event_type} は正の金額です"))
            held = True
        if sign_rule == "negative" and amount >= 0:
            issues.append(RowIssue("SIGN_INVALID", "needs_review", locator, "amount_jpy", str(amount),
                                   f"{event_type} は出金として負の金額です"))
            held = True
    if not any(dates.values()):
        issues.append(RowIssue("DATE_MISSING", "needs_review", locator, None, None, "日付がありません"))
        held = True
    return NormalizedRow(
        locator=locator, raw=raw, row_id=row_id, source_account=account, event_type=event_type, currency=currency,
        amount_jpy=amount, components=components, tax_status=tax_status, tax_jpy=tax_jpy,
        external_id=_normalize_text(get("external_id") or "") or None,
        description=(get("description") or "").strip(), grain=grain, role=role, held=held, **dates,
    )
