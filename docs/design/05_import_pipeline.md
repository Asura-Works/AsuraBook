# 05 取込共通基盤

版1.0 / 2026-10-23。ここで定める状態・形式・上限は本アプリの設計値。各社の実際の取得仕様は06/07章の確認範囲に従う。

## 1. 段階と完了条件

受付 → 原本保全 → 形式判定 → 解析 → 正規化 → 同一性判定 → 照合/仕訳候補 → 確認 → 記帳を分ける。取込成功は資料と確認待ち候補を保存した状態であり、postedへの確定を意味しない。

通信、ファイル読取、画像処理はDBのwrite transaction外で実行し、短い登録transactionだけを単一coreで行う。Providerのパーサーが仕訳へ直接INSERTする実装は禁止する。

原本の永続化が完了しなければ後段へ進めない。ファイル保存後DB登録前に停止した場合は、未登録原本を回収する。監視フォルダではサイズ/更新時刻の安定と読取後再確認を行い、コピー元を削除しない。

## 2. 論理モデル

| 要素 | 責務 |
|---|---|
| provider_profile | 列対応、文字コード、日付/符号意味、対象期間、版、検証サンプル |
| source_account | 銀行/カード/サービスの本人アカウント、通貨、補助科目 |
| source_object | 不変byte列、SHA-256、MIME、サイズ、保存先 |
| import_batch / attempt | 受付単位と再解析履歴 |
| source_observation | 元行/ページ、raw値、生の外部ID、取得時刻 |
| economic_event | 実際の入出金・利用・売上・手数料・源泉・返金 |
| event_observation_link | 同じ事象を裏付ける複数資料 |
| source_components | gross、fee、withholding、refund、net等の意味付き子明細 |
| source_usage_slots | 同じ経済イベント・会計役割の有効利用枠 |
| match_candidate | 照合候補、根拠、競合、手動判定 |
| settlement_allocation | open itemと決済のN:M割当 |
| import_issue | 元位置、項目、エラー、修正履歴 |
| coverage_manifest | 期間、全件/差分、件数/合計、完全性、確認者 |

economic_eventの永続化名は03章のsource_transactionsとする。観測はsource_observationsへ分離する。生provider IDと内部正規化identity keyを別保存し、枝番や有効範囲を生IDへ暗黙連結しない。

## 3. 原本と版

CSV・PDF・画像の受領byte列を保存する。文字修正、回転、OCR派生物は親原本を参照する別object。同名別内容は別原本、同内容再取得は同じ内容を再利用して取得履歴を追加する。

APIは許可されたresponse本文と取得範囲、ページ、request ID等を保全する。Authorization、Cookie、token、認証コード、秘密queryは保全対象に入れない。秘密の除外箇所を仕様化する。

再解析は新attemptとprofile/parser版で行い、日付・金額・件数・リンクの差を表示する。既記帳の変更は訂正案を作り、postedを自動置換しない。

## 4. CSVの正規化

UTF-8、BOM付きUTF-8、CP932を初期候補とし、実サンプルでprofileへ固定する。文字化けを置換文字で通さない。引用符内の改行/カンマ、空欄、重複列名、前置き、末尾合計、複数段見出しを検査する。

全角数字、桁区切り、和暦は明示ルールで変換し、原値を保持する。「－」の空欄記号と負数を区別し、全体置換で符号を消さない。取引日・売上確定日・処理日・支払日を分ける。時刻のない資料に架空の時刻を付けない。

会計金額はJPY整数。外貨原額は通貨・原文字列で保持し、円換算確定前は記帳を保留。率はDecimal文字列。0と未確認nullは別。

## 5. 汎用CSV v1

| 区分 | 項目 |
|---|---|
| 共通 | row_id、source_account、event_type、currency |
| 日付 | transaction_date、recognition_date、settlement_date、period_start/end |
| 金額 | amount_jpy、gross_jpy、fee_jpy、withholding_jpy、refund_jpy、net_jpy |
| 税務 | tax_status、tax_rule_id、tax_jpy、invoice_registration_no、business_ratio |
| 参照 | external_id、parent_external_id、order_id、settlement_id、statement_id、original_event_id、evidence_ref、description |

row_idは一度決めた識別子を保持し、行番号で作り直さない。銀行amountは入金正・出金負。カードはcharge/refund/paymentの種別と正額。売上はgrossと構成要素を使用する。空欄を0へ埋めるtemplateは配布しない。netだけ判明するものはnet_onlyとして総売上確定を保留する。

CSVプレビューで数式を実行しない。表計算向けに出力するCSVの数式注入を防ぐ一方、原本は変更しない。

## 6. 同一性と重複

優先順位は同一原本/解析版、安定した外部ID、同IDの変更観測、IDなし資料の重複期間比較、類似fingerprintの順。

外部IDはProvider、本人account、ID、枝番、ID有効範囲で解釈する。親売上IDだけで追加購入やおひねりを統合しない。同IDで金額が変われば観測差分を審査する。

IDなしの重複期間は、期間、出現数、行順、残高連続性、件数、金額を多重集合として比較する。同日同店1,100円が2件あれば正当な2件を保持する。fingerprintは検索候補でありUNIQUE制約にしない。曖昧なら利用者確認へ残す。

API、CSV、レシートが同じ購入を表しても経費認識は1回。後から銀行引落しが来た場合はカード債務の決済。

## 7. 照合・消込

精算ID、口座、通貨、金額、日付、相手先、未決済残から候補を作る。初期の許容差は0円。差額を資料なしに手数料や雑費へ逃がさない。

確定時はopen itemと精算残を再読込し、revision・上限をtransaction内で検査する。銀行入金と手数料/源泉控除と売掛消込額が一致するまで完了にしない。取消は不変eventとして保存する。

月次集計から詳細取引へ切替える場合はrecognition_scopeの重複を検出し、04章の元1→反対1→置換Nの訂正で移行する。月次と明細を併記して二重売上にしない。

## 8. CLIとエラー

taxdesk import inspect FILE は純粋preview。import stageは原本と候補登録。import reviewで差分を確認し、journal validate/postで確定する。reconcile apply PLAN --expected-revision Nで消込を適用する。

エラーはcode、severity、batch、元位置、field、mask済raw値、説明、修正方法を返す。ENCODING_AMBIGUOUS、SCHEMA_CHANGED、ACCOUNT_MISMATCH、AMOUNT_MEANING_UNKNOWN、DUPLICATE_AMBIGUOUS、ALLOCATION_OVERFLOW、SOURCE_REVISION_CONFLICT、COVERAGE_GAPを用いる。黙って行を読み飛ばさない。

## 9. 受入条件

同時の同一file投入、全期間CSV再取得、同IDの変更、同日同額2件、API/CSV/レシート重複、分割/一括精算、年末とJST日付、文字化け、途中停止、ページ取得失敗、過剰割当を試験する。成功件数と保留件数の総和が原資料の対象範囲を説明できること。

参考: 三井住友銀行CSV案内 https://www.smbc.co.jp/direct/sousa/help_kouza/10.html （確認日2026-10-23）。その他は本アプリの設計判断。


[設計書の目次](00_README.md)
