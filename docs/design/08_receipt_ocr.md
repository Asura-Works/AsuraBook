# 08 レシート画像読取りと確認

版1.0 / 2026-10-23。標準ProviderはClaude Code CLI。詳細な起動・認証・監視は21章。

## 1. 受付

スマホで撮影した画像をUSB転送や利用者が選んだ共有フォルダからPCへ渡し、画像追加またはdrag/dropで取り込む。初期版はスマホ専用アプリ不要。コピー途中の画像は読み始めず、サイズ/更新時刻の安定と内容hashを確認する。コピー元は自動削除しない。

| 入力 | 原本保存 | 読取り用派生物 |
|---|---|---|
| JPEG/PNG | 受領byte列 | 必要に応じ回転/補正 |
| HEIC/HEIF | 元形式 | 検証済decoderでPNG/JPEGへ |
| 紙スキャンPDF | 元PDF | ページ画像 |
| 電子取引PDF | 受領元PDF | テキストまたはページ画像 |
| スクリーンショット | 受領画像 | OCR画像。取得可能な原資料も追加 |

紙の撮影と電子取引原本を区別する。電子PDFを印刷して撮り直したものだけで元PDFを代替しない。紙廃棄可否は13章の保存要件確認による。

初期設計上限は25MiB、50メガピクセル、PDF20ページ、1batch100ファイル。Provider上限とは別であり、より小さい制限がある場合は通知する。壊れた画像・暗号PDF・過大画像は保留し、原本を損なわない。

## 2. 処理

原本spool保存 → hash/metadata登録を完了後、クラウド同期とClaude抽出を独立起動する。OCR失敗でも原本保存を進め、cloud失敗でもローカル画像を見て手入力できる。未同期を保管済みにしない。

長尺画像は全体と分割派生物を用意し、ページ/領域と原本の対応を保持する。編集は派生物だけへ行う。原本差替えは新versionまたは新文書として旧版を残す。

## 3. 抽出schema

| 項目 | 仕様 |
|---|---|
| schema_version | 1.0 |
| document_kind | receipt / invoice / credit_note / unknown |
| currency | JPY / other / unknown |
| merchant_name | 印字名またはnull |
| transaction_date | 実日付YYYY-MM-DDまたはnull |
| total_jpy | 印字総額を符号込みで保持、整数またはnull |
| tax_breakdowns | 率、税込/税抜対象額、税額、page、raw_text |
| registration_number | T+13桁またはnull |
| payment_method | cash/card/transfer/other/unknown |
| items | 名称、数量Decimal文字列、単価、合計、税率、page |
| fields_metadata | field_path、raw_text、page、confidence、bbox |
| issues | 不明・読取矛盾・要確認箇所 |

外貨を円へ勝手に変換しない。credit_noteが正額印字なら抽出時の総額は正のまま保持し、会計の符号は取引種別とともに後段で確認する。預り金・釣銭・小計を総額へ混ぜない。税額が読めない場合は率から埋めずnullにする。

confidenceは校正済み精度確率ではない。bboxを正確に得られない場合はnull。撮影EXIFの日付を取引日へ上書きしない。年が不明なら日付を保留する。

## 4. Provider

claude_cliを標準、codex_cliは設定された代替、external_ocrは将来の別契約、manualは常時利用可能とする。画像はモデル処理先へ送信される。原本cloud保管とAI送信先を別設定にし、送信対象・認証mode・利用上限を表示する。

ClaudeのReadはPNG/JPEG等の画像を扱える。HEICはそのまま読めると仮定しない。JSON wrapperのstructured_outputを取り出し、schemaと業務条件をアプリで再検証する。CLI版、実model、prompt版、schema版、入力hashを記録する。

## 5. 状態と再試行

OCR業務状態はqueued、preprocessing、running、extracted、needs_review、reviewed。失敗はretryable_failed/permanent_failed、中断はcancelled、結果回収不能はoutcome_unknown。AgentBridgeの実行状態、仕訳状態、upload状態は別軸。

同一入力hash・Provider・model・CLI/API版・prompt/schema版で結果を識別する。ローカルjob IDをProviderの外部照会IDとみなさない。remote_result_query=trueを確認したProviderだけ外部結果を照会する。標準CLIはfalseを初期値とする。

正常出力を回収できれば再利用し、成否不明からは無制限に再実行しない。新attemptは許可済みretry方針を確認して記録する。利用量が重複しない保証はしない。人が確認済みの値は再解析結果で上書きしない。

## 6. 検証と重複

Pythonが実日付、整数上限、税率別合計、品目合計、総額、ページ参照、T番号形式を検査する。不一致を勝手に金額修正せず、元箇所を表示する。

同じbyte列再投入は原本共用・新取得履歴。撮り直しの類似画像は重複候補であり自動削除しない。カード明細が既に記帳されていれば証憑リンクを第一候補にする。初期版は高confidenceだけでpostedへ自動確定しない。

T番号の形式と公表情報の照合は別。確認は取引日時点の登録/失効も考慮する。APIのアプリケーションID手続を済ませていない場合は手動確認と確認日時を提供する。番号の存在だけで適格性や経費性を確定しない。

## 7. 画面

左に画像、右に候補項目、下に既存カード/銀行/仕訳候補。読めない項目を強調し、原文と補正値を並べる。操作は「既存明細へ関連付け」「新規仕訳案」「私用除外」「再読取」「保留」。私用除外も原本と理由を保持する。

金額、日付、税率別内訳、既存取引の対応を利用者が確認してから記帳へ進む。通常の仕訳検証・権限・月締め条件を省略しない。

## 8. 受入条件

日本語証憑100枚以上で印字、手書き、薄い文字、影、回転、長尺、複数税率、再発行、領収書とカードの同一取引を含める。金額一致98%以上・日付95%以上は公開前の目標値で、未測定の達成を宣伝しない。

読取不能、JSON欠落、画像内命令、上限、認証切れ、process中断、cloud停止、同byte再投入を確認する。手入力が常に使え、原本が残ること。

### 一次資料

https://code.claude.com/docs/en/headless
https://code.claude.com/docs/en/cli-reference
https://code.claude.com/docs/en/tools-reference
https://www.invoice-kohyo.nta.go.jp/faq/faq-category/400/faq0017.html

確認日2026-10-23。採用CLI版・実アカウントでの動作検証は実装時に行う。


[設計書の目次](00_README.md)
