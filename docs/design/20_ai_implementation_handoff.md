# 20 実装エージェントへの引継ぎ

版1.0 / 2026-10-23。この文書はClaude Code/Codex等へ開発を依頼するときの契約。ユーザーの実際の帳簿を変更する指示ではない。

## 1. 開発開始時に読むもの

01要件→02構成→03データモデル→04会計→15CLI→21AI連携を先に読む。その後、担当adapter、税務、保存、テストの章を読む。曖昧な部分は「公式仕様未確認」「利用者設定待ち」「設計判断」を区別して記録する。

既存のREADME/AGENTS/CI規則がある開発repoではそれも確認する。設計書の例示ID、架空金額、placeholder pathを本番値として埋め込まない。

## 2. 推奨パッケージ構成

| path | 責務 |
|---|---|
| src/taxdesk/domain | 金額、日付、仕訳、訂正、消込、税ルールの純粋ロジック |
| src/taxdesk/application | command/query、権限、transaction、idempotency |
| src/taxdesk/infrastructure/db | SQLite repository、migration、unit of work |
| src/taxdesk/infrastructure/ipc | named pipe framing、認証session、client/server |
| src/taxdesk/importers | Provider profile、parser、正規化、coverage |
| src/taxdesk/evidence | 原本、索引、派生物、cloud outbox |
| src/taxdesk/agents | AgentBridge、Claude/Codex adapter、policy、schema検証 |
| src/taxdesk/monitoring | 公式source取得、差分、finding、予定 |
| src/taxdesk/tax | 年版rule、snapshot、帳票mapping、e-Tax exporter |
| src/taxdesk/gui | PySide6 view/view model/table model |
| src/taxdesk/cli | taxdesk command、JSON応答、exit code |
| tests/fixtures | 匿名/架空原資料、期待仕訳、年跨ぎ・訂正例 |
| docs | 本設計、運用、制約、検証記録 |
| gateway | cloud証憑API。desktop会計DBとは別deploy |

GUIとCLIからdomain/DBへ抜け道を作らず、applicationを通す。domainへQt widget、HTTP client、CLI subprocessを依存させない。

## 3. 依存と実行

Pythonは3.13の検証済みpatch版、uv.lockで再現性を管理する。依存候補はPySide6、Pydantic/JSON Schema検証、Typer等CLI、httpx、画像/PDFライブラリ、必要に応じFastAPI。最終採用版はWindows wheel、license、配布、セキュリティ更新を確認する。

選定していないpackageの架空versionをlockfileへ書かない。標準sqlite3で足りる範囲とORMを採用する範囲を02章の単一writer契約に合わせる。DB migrationの正本を1系統にする。

## 4. 最初の実装タスク

1. org/year/accountと金額型・date型。
2. draft/validated/postedと貸借、revision、原子的確定。
3. audit/command_results/source利用枠と訂正グループ。
4. open items、dated movements/allocations、as_of集計。
5. named pipe core、CLI JSON、GUI最小画面。
6. 原本保全、観測、単一銀行CSV、取込preview。
7. Claude実画像→schema→確認待ち、cloud原本upload。
8. backup/restoreとrecovery_hold。

この縦通しが合格してから各社adapter・高度な画面・申告税務を広げる。M0の外部可否調査は並行して行う。

## 5. 変更ごとの完了定義

入力・出力・state・error・権限・永続化境界を説明できること。正しい例だけでなく、重複、競合、停止、取消、年度跨ぎのうちその変更が影響する具体的リスクを検証する。

実装をなぞるだけの形式的testを増やさず、貸借、残高、原本、時点再現、権限、外部結果不明を検証する。Windows以外で通ったtestをWindows3.13対応完了と報告しない。

## 6. 行ってはいけない実装判断

不明な金額を0で埋める、銀行入金を全て売上にする、同日同額を自動削除、源泉を経費へ混ぜる、postedを上書き、openingで債権を再生成、current完済フラグで過去残を求める、AIのJSONを無検証で実行、税率を固定enumへ埋め込む、DBをOneDrive/NASへ置く、といった実装を避ける。

未契約APIや未受領CSVを「動くはず」で完成扱いにしない。AI上限をAPI keyに切替えて回避しない。e-Tax認証/PINをAIに渡して無人送信しない。復元後の外部jobを一律再送しない。

## 7. 変更提案の返し方

何ができるようになったか、設計のどの契約に対応したか、試験入力と結果、未検証条件、利用者が必要とする次の具体設定を報告する。機能名を列挙するだけでなく、帳簿残高や保存状態への影響を説明する。

例: 「カード11,000円→レシート→銀行引落しで、費用は11,000円のまま、未払残0、証憑リンクが残ることをfixtureで確認。実カードAPIは未契約で未検証」。

## 8. 設計資料の優先関係

ユーザーが指定したWindows/Python/uv/PySide6/SQLite/Claude/Codex要件を優先する。共通enum/永続化は03、会計不変条件は04、税年条件は11、提出段階は12、保存要件は13、CLI権限は15、復元は16、AI実行は21を正本とする。

章間の矛盾に気付いたら都合のよい片方だけを実装せず、影響と提案を記録して契約を統一する。既存ユーザーデータの破壊的変換は実データbackupと移行計画を用意してから扱う。


[設計書の目次](00_README.md)
