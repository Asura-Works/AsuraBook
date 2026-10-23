# 配布用設計データの確認

{
  "date": "2026-10-23",
  "page_readback_count": 22,
  "page_readback_nonwhitespace_equal": true,
  "json_files_parsed": 5,
  "example_schema_tests": [
    {
      "name": "receipt_valid",
      "pass": true
    },
    {
      "name": "cli_valid",
      "pass": true
    },
    {
      "name": "receipt_null_allowed",
      "pass": true
    },
    {
      "name": "receipt_fraction_rejected",
      "pass": true
    },
    {
      "name": "receipt_invalid_date_rejected",
      "pass": true
    },
    {
      "name": "receipt_extra_field_rejected",
      "pass": true
    },
    {
      "name": "cli_inconsistent_success_rejected",
      "pass": true
    }
  ],
  "relative_links_missing": 0,
  "windows_application_tests": "not_run",
  "validation_method": "JavaScriptによる配布用データ/使用Schemaキーワードの検査。製品実装の公式JSON Schema engineとWindows試験は別。"
}

ZIPは各ファイルのCRC32・byte一致、中央ディレクトリ、格納数、非空を確認する方式です。アプリを実装・実行した試験ではありません。ダウンロード画面の実ブラウザ操作は環境障害のため未確認です。
