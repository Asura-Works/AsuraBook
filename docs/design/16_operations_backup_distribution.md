# 16 Windows運用・復元・配布

版1.0 / 2026-10-23。runtimeと外部CLIの実機検証を製品リリース条件にする。

## 1. 環境

Windows 11 x64、CPython3.13の検証済patch版、uv、PySide6。uv.lockを固定し、uv sync --lockedで不整合を更新して隠さない。Python、SQLite、Qt、Claude/Codexの実行版をdoctorとbuild情報へ残す。

uv python install 3.13、uv python pin 3.13、uv sync --locked、uv run taxdesk doctor --json、uv run taxdesk guiは実装済projectでの例。本設計書一式は起動できるアプリprojectではない。

## 2. 配置

製品実行file、利用者データ、log、job、export、backupを分ける。DBとspoolはLOCALAPPDATAのTaxDesk/orgs配下を基本にする。原本投入フォルダは別。DB正本をNAS/OneDrive等へ移さない。

日本語、空白、長いpath、DPI、複数disk、ウイルス対策環境を確認する。coreは本人のログインsessionで動き、LocalSystemから本人CLI認証を同じように使えるとは仮定しない。

## 3. 耐久性

単一writer、WAL、FK、FULL同期、短transactionを採用する。disk満杯/I/O errorで成功応答を返さず、新規更新を止める。長期reader、WAL肥大、checkpoint失敗を診断する。

稼働中DBの本体1個をコピーしてbackup完了にしない。SQLite online backup等の一貫snapshotを使う。採用SQLiteの修正状況を確認する。

## 4. backup内容

DB、科目/profile/rule版、audit、申告snapshot/package/receipt、原本manifest、未同期原本を含める。token、PIN、passwordは通常含めず復元後に本人が再認証する。

manifestにbackup ID、org/year、app/schema/rule版、ledger_revision、記録境界、日時、DB hash、原本hash、cloud version、未同期範囲、暗号化方式、復旧要件を保存する。

軽量backupはcloud verified原本のversion参照、完全backupは原本byteも含む。参照だけのbackupをcloud account喪失に耐えるものと呼ばない。

## 5. 保持・暗号

日次、migration前、年度凍結前、提出package確定前を実行候補とする。日次30世代・月次24世代は運用初期値であり法定原本の保存期間ではない。

認証付き暗号の実績ある形式を選び、key導出・nonce・破損検出を検証する。独自暗号を実装しない。復旧keyはPC固有保護だけに依存させず、別媒体で復旧できる方法を用意する。

RPO24時間以内、通常の小規模データのRTO2時間以内は目標。実際の前回backup時刻と復旧試験時間を表示し、未測定を保証値にしない。

## 6. 復元手順

backup/manifest/hash/暗号を検査→一時領域へ復号→integrity_check/FK/残高/schema/ruleを検証→原本versionを確認→保守mutex取得→core停止→現データ退避→DB置換→read-only確認→復元専用gate解除の順。

restorerがcore停止から置換・read-only検証完了まで保守mutexを保持する。他CLI/GUIのon-demand core起動を拒否する。現在DBへ単純mergeしない。backup後の変更が失われる可能性をpreviewへ表示する。

## 7. 復元専用gate

DB置換前に、復元対象外の利用者別runtime_control.jsonへ新restore_generationとrecovery_hold=trueをdurable保存する。新PCでも復元開始時に作る。古いbackupのruntime controlを上書き復元しない。壊れたgate記録なら副作用処理を止める。

旧session、capability、process leaseを全失効し、tasks/outbox/agent jobsを一律queuedへ戻さない。recovery_holdは業務statusと直交する実行gate。read-only帳簿参照を先に可能にし、scopeごとに解除する。

| 処理 | 再開前の確認 |
|---|---|
| cloud upload | 登録ID、hash、version、外部保存結果 |
| 銀行/カード読取 | cursorと取得範囲。必要なら重複期間を再取得して統合 |
| AI | 最終出力/processを回収。回収不能ならoutcome_unknown |
| e-Tax | 公式受付/送信結果。未送信と推定して再送しない |
| 通知 | 外部送信ID、宛先policy、重複影響 |

backupに送信attempt自体がない場合もあるため、対象年/税目の公式受付状況とbackup後操作を確認するまで提出機能全体を保留する。通常の期限切れlease retryよりこのgateを優先する。

復旧sessionへ対象job、照会日時、根拠、採用結果、解除scope、ownerを記録する。全体gate解除後も成否不明の個別jobは保留を維持する。

## 8. migrationと更新

schema_versionの互換表を持ち、未知の新版を旧appで書かない。backup、排他、transaction可能な変更、整合検査、版更新の順。失敗時のrollback/旧版復旧を準備する。

税務ruleとimport profileは独立した版package。AI監視だけで自動公開しない。過去版を保持しsnapshot再現に使う。CLI版更新ではhelp/versionだけでなく画像・JSON・認証mode・tool制限のcontract testを行う。

## 9. 配布

まずuv環境で実証し、Python3.13/PySide6対応を確認したpackagerでexe化する。PyInstaller等は候補で、採用版、license、Qt plugins、decoder、font、証明書を検証する。

GUIとCLIのentry pointを分け、CLIのstdout/exit codeを保持する。Python/uv未導入のクリーンWindowsで試験する。Claude/Codexは利用者が公式導入した実体を設定できるようにする。

## 10. 診断と受入

logはtrace ID、種別、ID、status、件数、時間、error中心。秘密、画像base64、全文帳簿は既定で含めない。診断bundleの内容をpreviewできる。

日次backup、新PC復旧、cloud喪失、未同期原本、disk満杯、migration途中停止、CLI更新、未ログインtask、restore後外部成功を試験する。検証日時と結果を表示する。

一次資料: https://docs.astral.sh/uv/concepts/projects/sync/ 、https://www.sqlite.org/backup.html 、https://www.sqlite.org/wal.html （確認日2026-10-23）。


[設計書の目次](00_README.md)
