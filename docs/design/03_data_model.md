# 03 SQLiteデータモデル・不変条件

版1.0 / 2026-10-23。本章は永続化の正本契約。全テーブルの製品migrationは本契約から実装し、Windows配布runtimeで検証する。

## 1. 共通規約

内部PKはUUID文字列、金額はINTEGER円で*_jpy、率はDecimal文字列または基準点の整数、booleanは0/1、UTC時刻はISO8601、業務日はYYYY-MM-DD。金額が不明な欄はNULL。0と対象外を区別する。

各業務表はorg/yearの帰属を外部キーまたは関連先から確定する。foreign_keysを全connectionでONにする。表示aliasと内部ID、provider raw IDと正規化identityは別。主要表はSTRICTを採用する。

## 2. マスターと年度

| 表 | 主要列・制約 |
|---|---|
| org | id、display_name、timezone=Asia/Tokyo、currency=JPY、ledger_revision。1DB1事業者 |
| fiscal_years | id、org_id、year、start/end、workflow_state、revision。org/year UNIQUE |
| period_closes | year、期間、workflow_state、reason、actor、revision。期間重複をserviceで検査 |
| accounts | id、org、code、name、type、normal_side、active、revision。org/code UNIQUE |
| subaccounts | id、account_id、code、name、active。account/code UNIQUE、複合FKで行の科目と整合 |
| counterparties | id、org、display_name、registration_number、revision |
| source_accounts | provider、本人account、口座/カード識別、通貨、補助科目、用途 |
| provider_profiles | provider/data_kind/version、列/符号/日付規則、対象期間、検証資料、immutable payload |
| rule_versions | rule_key/version、effective_from/to、algorithm_version、payload/hash、source URL、確認/承認日 |

fiscal_year.workflow_stateはopen/preclose/adjusted/reviewed/frozen。計上gateのclosedはfrozenから導出する。月はopen/reviewing/soft_closedで、soft_closedが通常計上を止める。reopenは履歴付き操作であり独立した永続状態名にしない。

## 3. 仕訳

journal_entriesはid、org/year、journal_date、number、kind、description、status、revision、validation_hash、created/posted actor/timeを持つ。statusはdraft/validated/posted。伝票番号はyear/number UNIQUEで確定transaction内に採番する。

journal_linesはid、entry_id、line_no、account/subaccount、side、amount_jpy>0、tax_code、tax_amount_jpy、tax_amount_status、business_ratio、counterparty、description。entry/line_no UNIQUE。

tax_amount_statusはunknown/known/not_applicable。unknownとnot_applicableはtax_amount_jpy=NULL、knownは0以上の確認済み額。未指定をDEFAULT 0にしない。仕訳のsideと税額の方向を分け、返金は逆sideで表現する。

確定に2行以上・貸借一致・年度/月開放・有効科目・税情報条件を必須にする。postedヘッダーと明細のUPDATE/DELETEをDB triggerで防ぎ、draft→validated→posted以外の不正遷移を拒否する。serviceは利用者に意味のあるエラーを返す。

## 4. 訂正とsource利用

correction_groupsはid、original_entry_id、reversal_entry_id、status、reason、actor/time。replacement entriesを1:Nで関連付ける。元1件へ有効な完全反対は1件。置換N件の合計・意味はserviceで検証する。

source_transactionsをeconomic_eventの永続化名とする。id、source_account、event_type、raw_provider_id、normalized_identity_key、IDの枝番/有効範囲、transaction/recognition/settlement date、period、grain、recognition_scope、currency、provider_statusを持つ。

| 論理名 | 永続化名 | 責務 |
|---|---|---|
| source_object | source_files | 原本byte参照、hash、MIME、取得履歴 |
| source_observation | source_observations | raw JSON、元行/ページ、外部生ID、観測時刻、解析版 |
| economic_event | source_transactions | 同じ経済的取引の安定identity |
| event_observation_link | event_observation_links | 観測N:Mイベントと根拠 |
| amount component | source_components | gross/fee/withholding/refund/net/tax等の子明細 |
| recognition capacity | source_usage_slots | event+会計役割+scope別の利用可能額 |
| journal origin | source_links | 仕訳行と利用枠へのsigned effect、support参照 |

source_linksのroleはrecognition/settlement/support/historyを正本とする。fee/withholding/refund等の意味はsource_components.kindへ置く。反対/置換の属性はcorrection_groupとsigned effectで表し、履歴参照を正額利用として二重計算しない。

利用枠は同じeconomic_eventと会計役割について有効signed effectを合計し、0〜上限に保つ。元+反対+置換の履歴総額を単純累計しない。support/historyはcapacityを消費しない。訂正の相殺と新利用を同じtransactionで採用する。

raw外部IDは書換えず、normalized_identity_keyでaccount/ID/枝番/有効範囲を表現する。IDのないfingerprintにはUNIQUEを付けない。月次と明細のrecognition_scope重複はserviceで拒否する。

## 5. open itemと日付付き消込

open_itemsはid、org、origin_line_id、type=receivable/payable、account/subaccount、counterparty、currency、created_business_date。IDは年度を越えて同じ。opening仕訳から同じ債権を再生成しない。

open_item_movementsはid、open_item_id、kind、signed_amount_jpy、effective_business_date、source_journal_line、correction_group、recorded_audit_seq。元本発生・調整・取消は不変eventとして追加する。

reconciliation_allocationsはid、open_item_id、settlement_line_id、amount_jpy、effect=allocate/cancel、reverses_allocation_id、effective_business_date、recorded_audit_seq、actor/time。割当取消は元割当を参照する新eventで、UPDATE/DELETEしない。

as_of残高は、対象日以前に効く元本増減−有効割当。凍結時にはさらにaudit_cutoff_seq/固定ID集合を適用する。取消効力日前の過去残を変えない。元本・割当・決済capacity・通貨を同一transactionで検証する。

## 6. 証憑

evidence_documentsは文書のidentity、受領区分、取得元、保存方針。evidence_objectsは物理byte列、SHA-256、MIME、size、parent object、local ref、cloud key/version、sync状態。

evidence_index_rowsはdocumentごとの取引索引identity、evidence_index_versionsは不変の版。日付、signed amount_jpy、取引先、取引種別、page範囲、原文、proposed/confirmed/withdrawn、理由、actor/timeを持つ。

1PDFに複数index rowを許す。返金の負額をCHECKで禁止しない。最新の有効確認版を検索し、過去版を再現できる。evidence_linksはdocument/object/indexと仕訳をN:Mで関連付ける。

retention_hold、retain_untilはsync状態とは別。hash一致だけで法令適合にしない。

## 7. 取込・queue・AI

import_batches/attemptsは原本、account、profile/parser版、受付key、状態、coverage、件数、errorを持つ。source_observationsとcomponentsは不変。parse失敗も原位置で追える。

tasksはkind、state、payload ref、attempt、lease token/until、deadline、result/error。sync_outboxは対象objectと操作、冪等key、lease、再試行時刻。外部通信前にoutboxがcommitされていること。

agent_jobsはprovider=claude_cli/codex_cli、job_kind=receipt.extract/journal.suggest/law.discover/law.assess/filing.review/filing.followup、実model、CLI版、prompt/schema/policy版、input hash、snapshot、process identity、status、usage、result hash/errorを持つ。

agent statusはqueued/preparing/running/validating/succeeded/auth_required/quota_wait/timed_out/cancelled/failed/invalid_output/outcome_unknown。OCR業務状態は別のocr_jobsにqueued/preprocessing/running/extracted/needs_review/reviewed/retryable_failed/permanent_failed/cancelled/outcome_unknownを保持する。

monitor_runs、findings、proposed_rule_changesにsource/hash、topic、適用期間、根拠、status、severity、snapshot revision、対応/承認/試験を保持する。通知の既読とrule承認を分ける。

## 8. 税務snapshot・繰越

tax_profilesは年度、申告区分、青色承認、消費税、インボイス、経理方式、確認状態を版管理。tax_snapshotsはledger_revision、rule/profile版、作成日時、入力manifest、計算結果、hashを不変保存する。

input_manifestはaudit_cutoff_seq、仕訳ID、movement ID、allocation/cancel ID集合、原本参照、mapping/algorithm版を含む。ledger_revisionだけで後日のbackdated割当を除外できるとは扱わない。

tax_packagesはsnapshot、対象税目/書類/年版、schema、content hash、検証結果、export状態。submission_attempts/receipts/paymentsは提出、受付、納付を分離する。

carryforward_revisionsはsource snapshot、target year、opening entry、revision、旧/新の差額、opening_adjustment、stale状態。前期訂正は翌期opening_staleとし、承認後に差額調整を追加する。

## 9. 監査・認可・再送

audit_eventsはsequence_no、actor、request/command/job、対象ID、前後revision、入力/結果hash、理由、UTCをappend-only保存する。hash chainは改変検知補助であり、同一OS管理者への絶対的改ざん防止としない。

command_resultsのキーはorg+actor_scope+command+idempotency_key。actor_scopeは永続principalでsession/PIDを含めない。payload hashと保存結果を業務更新と同じtransactionへ置く。

approval_capabilitiesはtoken hash、principal、GUI承認session、org、scope、対象revision、plan hash、expiry、restore_generation、消費request。token本文を保存せず、業務更新と原子的に消費する。再送は同主体の保存結果だけを返す。

復元gateはDB外runtime_control.json。旧DBのsession/capability/leaseをそのまま再開させず、16章のrecovery_holdを優先する。

## 10. 索引とmigration

主要索引はyear/date/number、journal_line account/entry、source account/identity、observation file/locator、open_item account/date、allocation item/effective date、evidence index date/amount/counterparty、task state/next_run、finding year/status。

新規作成はDDL＋service validation＋invariant testを一体で行う。schema_versionは単調に上げ、未知の新版を旧appで書かない。backup後に排他migrationし、途中停止・外部キー・整合を確認する。

DB triggerはposted不変、状態遷移、正額、FK、不正削除等の防御層。日付の実在、N:Mの総量、期間重複、権限、rule適用はserviceでも検査する。DDLが読めるだけで本番検証完了にしない。

技術資料: https://docs.python.org/3.13/library/sqlite3.html 、https://www.sqlite.org/foreignkeys.html 、https://www.sqlite.org/stricttables.html （確認日2026-10-23）。


[設計書の目次](00_README.md)
