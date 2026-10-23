"""業務エラー。CLI exit code (15章4節) と対応させる。"""

from __future__ import annotations

from typing import Any

# exit code: 0成功、2入力/schema、3業務検証、4認証/権限、5revision/idempotency競合、
# 6外部一時障害、7timeout/cancel、8未対応、9内部整合性
EXIT_INPUT = 2
EXIT_BUSINESS = 3
EXIT_AUTH = 4
EXIT_CONFLICT = 5
EXIT_EXTERNAL = 6
EXIT_TIMEOUT = 7
EXIT_UNSUPPORTED = 8
EXIT_INTERNAL = 9

_EXIT_BY_CODE = {
    "INVALID_INPUT": EXIT_INPUT,
    "SCHEMA_ERROR": EXIT_INPUT,
    "UNKNOWN_COMMAND": EXIT_INPUT,
    "NOT_FOUND": EXIT_INPUT,
    "AMBIGUOUS_REFERENCE": EXIT_INPUT,
    "ENCODING_AMBIGUOUS": EXIT_INPUT,
    "SCHEMA_CHANGED": EXIT_INPUT,
    "PERMISSION_DENIED": EXIT_AUTH,
    "CAPABILITY_INVALID": EXIT_AUTH,
    "AUTH_REQUIRED": EXIT_AUTH,
    "AUTH_MODE_MISMATCH": EXIT_AUTH,
    "REVISION_CONFLICT": EXIT_CONFLICT,
    "IDEMPOTENCY_CONFLICT": EXIT_CONFLICT,
    "SOURCE_REVISION_CONFLICT": EXIT_CONFLICT,
    "MAINTENANCE": EXIT_EXTERNAL,
    "EXTERNAL_UNAVAILABLE": EXIT_EXTERNAL,
    "QUOTA_WAIT": EXIT_EXTERNAL,
    "TIMEOUT": EXIT_TIMEOUT,
    "CANCELLED": EXIT_TIMEOUT,
    "NOT_SUPPORTED": EXIT_UNSUPPORTED,
    "NOT_INITIALIZED": EXIT_UNSUPPORTED,
    "INTERNAL_ERROR": EXIT_INTERNAL,
    "INTEGRITY_ERROR": EXIT_INTERNAL,
}

RETRYABLE_CODES = {"EXTERNAL_UNAVAILABLE", "QUOTA_WAIT", "TIMEOUT"}


class TaxDeskError(Exception):
    """利用者に意味のあるエラー。code が分類、details が機械可読の根拠。"""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    @property
    def exit_code(self) -> int:
        return _EXIT_BY_CODE.get(self.code, EXIT_BUSINESS)

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def invalid(message: str, **details: Any) -> TaxDeskError:
    return TaxDeskError("INVALID_INPUT", message, details or None)


def not_found(kind: str, ref: str) -> TaxDeskError:
    return TaxDeskError("NOT_FOUND", f"{kind} が見つかりません: {ref}", {"kind": kind, "ref": ref})
