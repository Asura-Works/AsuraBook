# 02 全体アーキテクチャ

版1.0 / 2026-10-23。Windowsローカル会計を中心に、外部取得・証憑保管・AIを接続する。

## 1. 採用構成

PySide6 GUIとtaxdesk CLIは、同じ利用者sessionのTaxDesk coreへ接続する。coreのApplication Serviceが業務規則、権限、transactionを統括し、SQLiteの唯一のwriterになる。coreは各workerの結果を検証して短transactionで採用する。

domainは会計と税務の純粋ロジック、applicationはcommand/query、infrastructureはDB/IPC/外部接続、presentationはGUI/CLIに分離する。Qt型、HTTP response、AI出力をdomainの値型にしない。

## 2. processの責務

| component | 責務 | 禁止する依存 |
|---|---|---|
| GUI | 表示、入力、進行、owner承認 | SQLite直接更新 |
| CLI | 機械可読入力、出力、exit code | SQL直接公開、自己申告昇格 |
| core | 認可、検証、書込、監査、queue | 外部通信中のwrite transaction |
| import worker | file/API読取、解析、正規化候補 | posted直接作成 |
| evidence worker | 派生物、upload、原本照会 | 原本上書き |
| AgentBridge worker | Claude/Codex起動・結果回収 | 業務DB書込、内部AI再帰 |
| monitor fetcher | 公式資料取得、差分保全 | 私的帳簿を公開検索へ送信 |
| report worker | 固定snapshotから帳票生成 | 異なるrevisionの集計混在 |

初期はcoreとworkerを同一製品配布内に置く。CLI subprocessはジョブごとの子processとして管理する。gatewayは証憑保存の別deployで会計DBを持たない。

## 3. Windows IPC

初期IPCはWindows named pipe。利用者SIDとorg IDを含む名前、明示DACL、current userへの制限、remote client拒否を採用する。mutexでcoreを1つに限定する。既存pipeの所有者やprotocolが不明なら勝手に接続しない。

framingは4byte big-endian長＋UTF-8 JSON。protocol_version、request_id、command、org、payload、expected_revision、idempotency_key、session/capability参照を持つ。最大frameは設計値8MiBとし、画像や巨大CSV本文を流さず管理object参照を渡す。

handshakeでprotocol版、app版、機能、認証主体を確認する。同一SIDは接続条件でありownerの証明ではない。通常CLIはassistant。owner承認channelは15章のprivate GUI経路に分ける。

coreが未起動ならGUI/CLIが起動を要求できる。ただしmigration、restore、recovery_hold中は保守mutexと実行gateを先に検査し、自動起動を拒否/読取限定にする。

## 4. command transaction

認証主体を解決→同じidempotency結果を確認→入力とrevisionを検証→BEGIN IMMEDIATE→最新の締め/残高/権限を再検証→業務変更→監査→outbox/task→capability消費→command結果保存→COMMIT。

途中失敗はrollbackする。外部送信そのものはcommit前に行わず、outboxをcommitしてworkerへ渡す。これによりDB成功と外部失敗を区別できる。外部側で成功・応答消失の場合は冪等ID/結果照会、照会不能ならoutcome_unknownで扱う。

## 5. SQLite

1事業者1DB・複数年度。ローカルNTFSへ置き、NAS/同期フォルダへ正本を移さない。WAL、foreign_keys=ON、synchronous=FULL、busy_timeout=5000msを初期値とする。各connectionでPRAGMAを確認する。

writerはcore専用thread/connection。readerは短いsnapshotを使い、長時間read transactionによるWAL肥大を監視する。帳票は必要データを固定snapshotに切り出す。Python sqlite3のautocommitとBEGIN/COMMIT方式を明示し、APIの既定挙動へ依存しない。

実際に配布するPythonが使うsqlite3.sqlite_versionを検査する。Python3.13と宣言するだけで最新のSQLite修正が入っているとみなさない。STRICT等の必要機能、WAL関連修正、backup APIを採用runtimeで確認する。

## 6. 訂正・消込の原子性

元1件、完全反対1件、置換N件をcorrection_groupで扱う。sourceのsigned effectと利用枠、依存消込の取消、再割当、監査を同じtransactionで確定する。訂正途中に元帳だけ変わり未回収が古いままになる状態を公開しない。

open_item_idは年度を跨いで維持し、openingは残高投影にする。dated movements/allocationsをas_ofと記録境界の両方で集計する。凍結snapshotは仕訳revisionだけでなく不変eventのID集合を保持する。

## 7. 非同期task

taskはqueued/running/retry_wait/succeeded/failed/cancel_requested/cancelled/dead_letter/outcome_unknownを基本とする。lease token、lease_until、attempt、deadline、入力hash、結果refを持つ。

at-least-once実行を前提に結果採用を冪等化し、exactly-onceの外部実行を保証しない。期限切れleaseはprocess状態と結果を確認して回収する。復元後のrecovery_holdがある場合は通常retryより優先して保留する。

原本のローカル保存完了後、cloud uploadとOCRを独立起動する。片方の失敗で他方の成功を取り消さない。cloud verifiedをOCR開始の必須条件にしない。

## 8. AI実行境界

採用exeと固定argv、stdin prompt、job専用cwd、mode別環境変数allowlistを用いる。本人CLI認証を使うが実効auth/providerを確認し、API課金へ無断変更しない。managed policyを維持する。

AIのJSONを構文・schema・業務規則で検証し、candidate/findingとして保存する。会計金額計算、rule公開、申告送信はAIの自由出力で実行しない。CLI版が変わったらdoctorとcontract testを行う。

## 9. 時刻・金額・ID

業務日はAsia/Tokyoの日付、監査はUTC timestamp。日だけの資料に時刻を捏造しない。金額はINTEGER円、比率と中間計算はDecimal。内部IDはUUID、提供元IDと表示伝票番号は別。

actor_scopeは永続principal。session/PIDをidempotencyキーに含めない。request ID、job ID、correlation IDを横断して追跡するが、秘密や全文帳簿をlogへ複製しない。

## 10. 一次資料と実装gate

Python sqlite3: https://docs.python.org/3.13/library/sqlite3.html
uv lock/sync: https://docs.astral.sh/uv/concepts/projects/sync/
Qt for Python: https://doc.qt.io/qtforpython-6/gettingstarted.html
SQLite WAL: https://www.sqlite.org/wal.html
SQLite backup: https://www.sqlite.org/backup.html
Named pipe security: https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights

確認日2026-10-23。Windows実機、配布Python3.13、実CLI認証、API契約の動作確認は製品実装時のgateであり、本書の作成で完了したとはしない。


[設計書の目次](00_README.md)
