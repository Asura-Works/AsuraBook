# 架空サンプルの説明

全データは設計確認用の架空例です。実際の銀行・サービスのCSV形式ではありません。

receipt_extraction.example.jsonはClaudeの構造化候補例。T番号は不明なのでnull、confidence/bboxもnullです。仕訳確定データではありません。

cli_response.example.jsonは非同期受付例。ok=trueはjobの完了を意味せず、status=queuedです。

generic_bank_v1.csvは05章の汎用CSVに対応する例。bank_creditは入金正、bank_debitは出金負の規約をここで宣言します。どちらもBANK-DEMOの架空行です。入金を自動で売上にせず、カード引落しを再び経費にしません。tax_status=unknownを確認済み0と解釈しません。

JSON Schemaで表現できない貸借・日付・取引の意味・期間完全性はアプリ側の検証が必要です。
