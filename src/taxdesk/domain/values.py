"""金額・日付・率の値型。

金額は円整数 (INTEGER)、比率と中間計算は Decimal。不明は None で、0 と区別する。
業務日は Asia/Tokyo の日付、監査時刻は UTC。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from .errors import invalid

TOKYO = ZoneInfo("Asia/Tokyo")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DECIMAL_RE = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")
MAX_AMOUNT_JPY = 10**15


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def today_tokyo() -> dt.date:
    return dt.datetime.now(TOKYO).date()


def parse_amount_jpy(value: Any, field: str = "amount_jpy", *, allow_negative: bool = False,
                     allow_zero: bool = False) -> int:
    """円整数を受け付ける。float・小数・文字化け・bool は拒否する。"""
    if isinstance(value, bool) or value is None:
        raise invalid(f"{field} は円整数で指定してください", field=field, value=value)
    if isinstance(value, int):
        amount = value
    elif isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        amount = int(value.strip())
    else:
        raise invalid(f"{field} は円整数で指定してください (小数・浮動小数は不可)", field=field, value=str(value))
    if abs(amount) > MAX_AMOUNT_JPY:
        raise invalid(f"{field} が上限を超えています", field=field)
    if amount < 0 and not allow_negative:
        raise invalid(f"{field} に負数は指定できません。逆の貸借で表現してください", field=field, value=amount)
    if amount == 0 and not allow_zero:
        raise invalid(f"{field} に0円は指定できません", field=field)
    return amount


def parse_optional_amount(value: Any, field: str, **kw: Any) -> int | None:
    if value is None:
        return None
    return parse_amount_jpy(value, field, **kw)


def parse_date(value: Any, field: str = "date") -> dt.date:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise invalid(f"{field} は YYYY-MM-DD 形式で指定してください", field=field, value=value)
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise invalid(f"{field} は実在する日付ではありません", field=field, value=value) from None


def parse_ratio(value: Any, field: str = "business_ratio") -> Decimal | None:
    """0〜1の Decimal 文字列。None は未指定。"""
    if value is None:
        return None
    if not isinstance(value, str) or not _DECIMAL_RE.match(value):
        raise invalid(f"{field} は Decimal 文字列 (例 \"0.6\") で指定してください", field=field, value=value)
    try:
        ratio = Decimal(value)
    except InvalidOperation:
        raise invalid(f"{field} が不正です", field=field, value=value) from None
    if not (Decimal(0) <= ratio <= Decimal(1)):
        raise invalid(f"{field} は0〜1の範囲で指定してください", field=field, value=value)
    return ratio


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def content_hash(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def format_jpy(amount: int | None) -> str:
    if amount is None:
        return "不明"
    return f"{amount:,}"
