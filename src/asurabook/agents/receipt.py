"""レシート抽出結果の検証 (08章3・6節)。

AIのJSONは構文・schema・業務条件をアプリで再検証する。不一致を勝手に修正せず issue として返す。
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from functools import lru_cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_VERSION = "1.0"
PROMPT_VERSION = "receipt_extract/1.0"
MAX_ABS_JPY = 10**12


@lru_cache(maxsize=1)
def schema() -> dict:
    text = (resources.files("asurabook") / "specs" / "receipt_extraction.schema.json").read_text(encoding="utf-8")
    return json.loads(text)


def compact_schema() -> str:
    """CLIへ渡す schema。Claude CLI 2.1 の検証器は draft 2020-12 の $schema 参照を解決できないため外す。
    アプリ側の再検証は元の完全な schema で行う。"""
    cli_schema = {k: v for k, v in schema().items() if k != "$schema"}
    return json.dumps(cli_schema, ensure_ascii=False, separators=(",", ":"))


@lru_cache(maxsize=1)
def prompt_template() -> str:
    text = (resources.files("asurabook") / "specs" / "prompts" / "receipt_extract.md").read_text(encoding="utf-8")
    start = text.index("## 指示")
    end = text.index("## アプリ側の必須後処理")
    return text[start + len("## 指示"):end].strip()


def build_prompt(file_names: list[str], *, attached: bool = False) -> str:
    manifest = json.dumps(file_names, ensure_ascii=False)
    body = prompt_template().replace("{{input_manifest}}", manifest)
    how = ("画像はこの依頼に添付されています。" if attached
           else "画像は作業フォルダ内の上記ファイル名です。Read ツールで読み、")
    return body + "\n\n" + how + "結果は指定された JSON Schema の構造化出力だけで返してください。"


def validate(extraction: Any) -> tuple[bool, list[dict]]:
    """(schema適合, issues)。schema不適合は invalid_output。業務条件は needs_review の issue。"""
    validator = Draft202012Validator(schema(), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(extraction), key=lambda e: list(e.absolute_path))
    if errors:
        return False, [{"code": "SCHEMA_VIOLATION", "field_path": "/".join(str(p) for p in e.absolute_path) or None,
                        "message": e.message[:300], "severity": "needs_review"} for e in errors[:20]]
    issues: list[dict] = []

    def add(code: str, field: str | None, message: str) -> None:
        issues.append({"code": code, "field_path": field, "message": message, "severity": "needs_review"})

    date = extraction["transaction_date"]
    if date is not None:
        try:
            dt.date.fromisoformat(date)
        except ValueError:
            add("DATE_INVALID", "transaction_date", "実在しない日付です")
    for path, value in _amounts(extraction):
        if value is not None and abs(value) > MAX_ABS_JPY:
            add("AMOUNT_OUT_OF_RANGE", path, "金額が上限を超えています")
    total = extraction["total_jpy"]
    if extraction["currency"] != "JPY" and total is not None:
        add("CURRENCY_TOTAL", "total_jpy", "外貨の場合 total_jpy は null です")
    bases = [b["tax_inclusive_base_jpy"] for b in extraction["tax_breakdowns"]]
    if total is not None and bases and all(b is not None for b in bases) and sum(bases) != total:
        add("TAX_BASE_MISMATCH", "tax_breakdowns", f"税率別対象額の合計 {sum(bases):,} と総額 {total:,} が一致しません")
    for i, b in enumerate(extraction["tax_breakdowns"]):
        if b["rate_decimal"] is not None and not (Decimal(0) <= Decimal(b["rate_decimal"]) < Decimal(1)):
            add("TAX_RATE_RANGE", f"tax_breakdowns/{i}/rate_decimal", "税率は0以上1未満の小数です")
        inc, exc, tax = b["tax_inclusive_base_jpy"], b["tax_exclusive_base_jpy"], b["tax_jpy"]
        if inc is not None and exc is not None and tax is not None and exc + tax != inc:
            add("TAX_ARITHMETIC", f"tax_breakdowns/{i}", "税抜額+税額が税込額と一致しません")
    lines = [it["line_total_jpy"] for it in extraction["items"]]
    if total is not None and lines and all(x is not None for x in lines) and sum(lines) != total:
        add("ITEMS_TOTAL_MISMATCH", "items", f"品目合計 {sum(lines):,} と総額 {total:,} が一致しません (値引・送料等を確認)")
    if total is None:
        add("TOTAL_UNREADABLE", "total_jpy", "総額が読めていません。手入力してください")
    if date is None:
        add("DATE_UNREADABLE", "transaction_date", "取引日が読めていません。年を推測で補いません")
    for i, issue in enumerate(extraction["issues"]):
        issues.append({"code": "MODEL_" + str(issue["code"])[:40], "field_path": issue["field_path"],
                       "message": issue["message"][:300], "severity": issue["severity"]})
    return True, issues


def _amounts(x: dict):
    yield "total_jpy", x["total_jpy"]
    for i, b in enumerate(x["tax_breakdowns"]):
        for k in ("tax_inclusive_base_jpy", "tax_exclusive_base_jpy", "tax_jpy"):
            yield f"tax_breakdowns/{i}/{k}", b[k]
    for i, it in enumerate(x["items"]):
        for k in ("unit_price_jpy", "line_total_jpy"):
            yield f"items/{i}/{k}", it[k]
