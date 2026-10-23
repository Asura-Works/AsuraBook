# 15 CLI・AI操作権限

版1.0 / 2026-10-23。CLIはGUIと同じApplication Serviceを呼び、SQLite直接操作や任意SQLを公開しない。

## 1. 起動

開発時 uv run taxdesk、配布時 taxdesk.exe。GUIはtaxdesk gui。coreはstatus/start/stopを持つ。必要時の自動起動は保守mutex・migration・復元gateを先に検査する。

内部AIを起動する方向は21章。本章は外部Claude/Codex等がTaxDeskを操作する契約。内部AIに無制限のtaxdesk ai run権限を渡さない。

## 2. 共通option

| option | 意味 |
|---|---|
| --org / --year | 対象。暗黙の別年度へ切り替えない |
| --json / --format json | 機械可読応答 |
| --input FILE / --input - | JSON fileまたはstdin |
| --dry-run | 副作用を起こさず検証 |
| --request-id UUID | 追跡ID |
| --idempotency-key KEY | 成功済み再送の重複防止 |
| --expected-revision N | 更新対象の版 |
| --no-input | 対話を禁止し不足はerror |
| --timeout-seconds N | clientの待機時間。job cancelとは別 |

金額は円整数、率はDecimal文字列。actor/ownerの自己申告optionを認証に使わない。秘密をargvへ載せない。

## 3. command

| 分類 | command |
|---|---|
| 診断 | doctor、version、capabilities、schema show |
| 設定参照 | accounts list、profiles list、settings show |
| 取込 | import inspect/stage/review |
| 接続 | connections list、sync run/status |
| 仕訳 | journal list/show/draft/update/validate/post/reverse |
| 消込 | reconcile suggest/apply、bank reconcile |
| 証憑 | evidence add/list/show/link/read |
| 帳票 | report trial-balance/profit-loss/balance-sheet/export |
| 決算 | year-end check/preview/close/reopen |
| 税務 | tax snapshot create、tax validate、tax guide export |
| e-Tax | etax capability/validate/export/handoff/status/receipt |
| AI | ai status、ai jobs list/show/cancel、ai run |
| 監視 | monitor run/status/findings/ack/resolve |
| 保全 | backup create/verify、restore preview/apply |

schema show COMMANDでpayloadとscopeを取得できる。表示aliasは内部UUIDへ一意に解決し、曖昧ならerror。

## 4. 応答

protocol_version、request_id、ok、data、warnings、error、retryable、observed_revisionを必須とする。ok=trueならerror=null。ok=falseならcode/message/detailsを持つ。stdoutは最終JSON1個、進捗はstderr。明示的なstream modeだけJSONLを使う。

exit codeは0成功、2入力/schema、3業務検証、4認証/権限、5revision/idempotency競合、6外部一時障害、7timeout/cancel、8未対応、9内部整合性。自動再試行はretryableとjob状態の両方を確認する。

非同期受付はjob_id/status=queuedで返す。受付成功をjob成功とみなさない。client timeout後に新keyで同じ要求を送らず、保存済み結果/状態を先に照会する。

## 5. dry-runとplan

dry-runは業務DB、原本、outbox、command_resultsを変更しない。外部取得やtoken refreshも行わない。既存データのread-only snapshotで検証し、未初期化ならDBを作らず未初期化を返す。一時fileはprivate tempに限り削除する。

planはplan_id、対象ID/revision、前後差分、検証結果、ledger_revision、rule版、内容hash、期限を返す。適用時はすべて再検証する。previewとapplyの間に変更されたplanを黙って適用しない。

## 6. 冪等性

正本キーはorg_id+actor_scope+command_name+idempotency_key。actor_scopeは認証した永続principalであり、session、PID、CLI起動IDを含めない。

同key/同payload/同主体は保存結果だけを返す。同key/異payloadはIDEMPOTENCY_CONFLICT。業務更新、capability消費、command結果を同transactionに保存する。未commitの失敗ではどれも残さない。

GUIから委任されたAIはcapabilityに束縛した正本主体で照合する。呼出し側がactor_scopeを差し替えられない。成功要求の再送では消費済tokenを使って再実行せず、現在の参照権限を確認して保存結果だけを返す。

## 7. 権限profile

| profile | 許可 |
|---|---|
| reader | 許可範囲の検索・検証・帳票参照。個人情報は既定mask |
| assistant | 候補取込・下書き・関連候補・monitor。posted/締め/復元/提出は不可 |
| bookkeeper_scoped | ownerが指定した口座・期間・金額・件数・時間内の確定/消込 |
| owner | 重要設定、締め解除、復元、最終提出等の本人操作 |

通常CLIの無token接続はassistant。同一Windows SIDであることは接続条件でありownerの証明ではない。明示reader sessionをassistantへ引き上げない。

## 8. owner承認とcapability

coreが固定した製品GUI entry pointを起動し、その子processにだけ継承するprivate pipeで承認sessionを確立する。path/版、PID/作成時刻、nonceを検査する。公開CLIはGUIを開けるが、owner tokenの発行やprivate channelを自己申告で取得できない。

GUIが対象、金額、期間、前後差分を表示し、人が承認したplanだけに一回用capabilityを発行する。起動だけで永続ownerにしない。同じWindows管理者の意図的攻撃まで完全に防ぐOS境界とは位置付けず、製品CLI/AIの誤操作を抑える。

tokenは256bit以上の暗号学的乱数。DBにはhash、principal、承認session、org、scope、対象revision、plan hash、expiry、restore_generation、消費requestを保存する。token本文は残さずstdin等で扱う。

対象変更、期限、締め変更、owner取消、復元で失効する。既承認batchは1伝票ごとに同じ承認を繰り返さない。

## 9. ファイルI/O

出力はowner設定のexport root配下へ限定。GUIで選択した他の保存先は操作単位の許可にできる。canonical pathと親directory、junction/reparse/symlink、検査後の置換を考慮する。

DB/WAL/SHM、原本、job、設定、認証、実行fileへの出力を拒否。既定no-clobber。安全な一時fileへ完成後atomic renameする。readerに任意path書込み能力を与えない。

入力fileも任意の秘密fileをAIへ転送する口実にせず、明示選択された取込scope、MIME、size、path、引用先を検査する。XML/ZIP等は外部entity/path traversalを無効化する。

## 10. 代表例

uv run taxdesk doctor --json
uv run taxdesk import inspect bank.csv --profile bank-a-v1 --json
uv run taxdesk import stage bank.csv --profile bank-a-v1 --account BANK-A --idempotency-key bank-202610 --json
uv run taxdesk journal draft --input journal-draft.json --idempotency-key sale-001 --json
uv run taxdesk journal validate JOURNAL-ID --json
uv run taxdesk report profit-loss --year 2026 --json
uv run taxdesk monitor run --topic filing --year 2026 --provider claude_cli --json

実装後の例。ID/path/profileは利用者設定へ置換し、この設計書だけでcommandが導入済みとはしない。

## 11. paginationと中断

一覧はstable sort/cursor、既定100件・上限1000件。大量処理はjob IDで進行を照会する。バッチ全体検証を既定とし、明示的部分成功だけ行別結果を返す。半端なposted伝票は作らない。

cancelは短transactionの安全点で止め、完了/未実行を識別する。AI内部jobの再帰起動を拒否し、auditへactor/provider/job/request/対象/hash/revisionを残す。

## 12. 受入条件

GUI/CLI一致、異常JSON、schema取得、期限切れplan、競合、同key異payload、同主体再ログイン再送、消費済cap再送、owner自己申告拒否、export overwrite/path逸脱、dry-run副作用なし、復元後token失効を検証する。


[設計書の目次](00_README.md)
