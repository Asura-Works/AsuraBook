# TaxDesk 詳細設計追補：Claude / Codexによる会計支援

版: 1.0 / 確認日: 2026-10-23

## 1. 要件の確定

Windows 11、Python 3.13（uv）、PySide6、SQLiteの会計アプリに、Claude CodeとCodexのCLIを呼び出すAgentBridgeを設ける。レシート画像の認識はClaudeを標準とする。Codexのコマンドは codex exec とし、Windows実行ファイルを使う場合は codex.exe exec となる。

ユーザーの「新しい商法」は、商法・税法等の制度変更の監視を主に想定する設計上の仮定とする。「新しい商売の方法」を意図する場合は、公開情報の調査トピックとして別に扱える。会計ルールや税務ルールへ自動反映する指示とは解釈しない。

対象プラットフォームはランサーズ、ココナラ、MENTA、クラウドワークス、note。クラウドワークスはユーザー確認済み。

## 2. 2方向のCLI連携

| 向き | 入口 | 責務 |
|---|---|---|
| 利用者のAIエージェント → アプリ | taxdesk CLI | 取込・検索・仕訳候補・検証・許可済み操作 |
| アプリ → AIエージェント | claude -p / codex exec | 画像認識、科目候補、制度調査、申告点検 |

どちらも会計DBを直接書き換えない。GUIとtaxdesk CLIは同じApplication Serviceを利用し、SQLiteへの書込みは単一coreに集約する。内部AI jobがtaxdesk ai runを再び呼び出す権限は与えず、再帰起動を防ぐ。

## 3. AI jobの種類

| job_kind | 入力 | 出力 | 採用処理 |
|---|---|---|---|
| receipt.extract | 指定画像、抽出schema | 日付、店名、金額、税額、読めない項目 | schema・数値検査後に確認待ち候補 |
| journal.suggest | 正規化明細、科目、適用ルール | 科目、摘要、関連候補、根拠 | 有効科目と貸借を検証して下書き |
| law.discover | 公開の監視トピック、公式資料 | 変更候補、出典、適用日 | 原文取得と差分保存 |
| law.assess | 確認済み公式差分、最小限の税務属性 | 影響、対象条件、必要な作業 | 更新提案として記録 |
| filing.review | 帳簿snapshot、決定的検査の結果 | 不足資料、異常候補、説明 | 根拠付きfindingを保存 |
| filing.followup | 提出・受付・納付の保存記録 | 未完了項目の説明 | 実証済みの状態のみ採用 |

合計・貸借・税率適用・消込・税額計算・申告ファイル生成はPythonの決定的処理で行う。AIの数値説明を計算の正本にしない。

## 4. レシート処理

1. スマホ撮影画像をPCへ渡し、ドラッグ＆ドロップまたは指定フォルダから受付。
2. 原本byte列をアプリのspoolへ保存し、SHA-256・サイズ・受付履歴を登録。
3. 原本のクラウド保存とClaudeの読取りを独立したjobで開始。
4. 回転・縮小・HEIC変換・PDFページ画像は派生物として作成し、原本を変更しない。
5. Claudeが画像を読み、固定JSON Schemaへ抽出。
6. Pythonが日付、整数円、税率別合計、T番号形式、既存明細との重複候補を検査。
7. 利用者が画像と項目を確認し、既存仕訳に証憑を付けるか新規下書きを作る。
8. 通常の仕訳検証・確定手順へ進む。

カード利用が既に経費計上済みなら、レシート追加で経費を再計上しない。銀行のカード引落しは未払金の決済として扱う。

読めない値はnull。不明な年を撮影年から補完しない。モデルのconfidenceを校正済み正解確率とみなさない。bboxは根拠のある座標がなければnull。返金伝票の印字金額は原文の符号を保持し、会計上の方向は別に確認する。

画像をClaudeで読む方式は、画像をモデル提供先へ送る処理を含む。利用者の既存CLI認証を使用し、送信対象と利用モードを設定する。クラウド保管先とAI処理先は別の送信先として表示する。

## 5. AgentBridge実行契約

AgentAdapterは capabilities / start / poll / cancel / normalize_output を提供する。workerがCLIを起動し、coreが結果の検証と保存を行う。

JobSpecには job_id、kind、provider、model、入力manifest/hash、snapshot/revision、prompt/schema版、policy、deadline、出力上限、retry方針を含める。provider名は claude_cli / codex_cli。利用者表示名と混同しない。

起動は引数配列・shell=False相当とし、固定した実体exeを使う。ファイル名、摘要、AI回答をshell文字列へ連結しない。.cmd/.batのshimを使う場合は実体を解決して検証し、任意文字列をcmd.exeへ渡さない。promptはstdinへ渡す。

### Claude画像job

設計例は次のargv構成とする。実装時に採用CLI版の対応をdoctorで検査する。

- claude.exe
- -p
- --restricted
- --tools Read
- --allowedTools Read
- --disallowedTools mcp__*
- --output-format json
- --json-schema アプリが生成したcompact schema
- --max-turns 4
- --no-session-persistence

cwdはjob専用フォルダ。Read対象は内部名でコピーした必要画像だけ。Bash、Edit、Write、WebFetch、WebSearch、MCPを画像抽出jobへ渡さない。allowedToolsは確認省略の指定であり、tool集合制限の代わりにはしない。

ClaudeのJSON wrapperの structured_output を取り出す。process終了コード0だけでは採用せず、エラー・中断・拒否・schema欠落を判定する。自然文から無理にJSONらしい部分を切り出さない。画像内の命令はデータとして扱い、実行しない。

### Codex job

設計例は codex.exe exec に --sandbox read-only、--skip-git-repo-check、--ephemeral、--ignore-user-config、--json、--output-schema、--output-last-message、設定override features.shell_tool=false と web_search="disabled"、stdin指定 - を渡す構成。画像代替時だけ --image と許可画像pathを追加する。

JSONLの進行イベントと最終業務JSONを別々にparseする。read-only sandboxだけで全ファイルの読取りを禁止できるとは考えず、shell、MCP、app、plugin、外部設定の有効範囲を採用版で検査する。managed policyを無効化して動作させない。

## 6. 認証と意図しないAPI課金の防止

subscription利用とAPI key利用を別モードにする。既存CLIログインを使っていても、親processにAPI keyがあれば実効認証が変わる可能性がある。

Claude公式資料は、非対話 -p で ANTHROPIC_API_KEY が存在すれば使われ、subscription認証より優先すると説明している。従って、子環境はmode別allowlistから構成する。subscription modeでは未承認のAPI key、bearer token、cloud-provider選択、認証profile、endpoint override、apiKeyHelperを引き継がない。利用者の他のshell設定自体は書き換えない。

jobと同じexe・環境・settings・policyで実効認証を確認する。Claudeでは採用版の claude auth status のauthMethod等を検証入口にする。設定mode・許可送信先と違えば AUTH_MODE_MISMATCH で停止する。Codexも採用版の実効auth/provider確認を必須とする。

既存認証のための設定ディレクトリは維持するが、アプリが認証ファイルの秘密を抽出・コピーしない。managed policyや既存gatewayが別経路を要求する場合は不一致として知らせ、policyを変更して迂回しない。

Claudeの --bare はsubscription loginを使わないため、標準起動には入れない。利用上限時にAPI課金へ切り替えず、事前に設定されたときだけCodexへの代替を許す。利用料金・モデル・利用上限は契約に依存し、無料・無制限と断定しない。

## 7. 状態・停止・結果不明

通常は queued → preparing → running → validating → succeeded。
分岐は auth_required、quota_wait、timed_out、cancelled、failed、invalid_output、outcome_unknown。

time-outやcancelは停止理由であり、Provider側の処理・利用量が未発生だったことを意味しない。正常出力がなく成否不明なら outcome_unknown とする。標準CLIのremote_result_query能力はfalseを初期値とし、ローカルjob IDを外部照会IDと混同しない。

回復は当該processの終了確認、保存済みstdout・最終結果の回収、schema検証を先行する。外部operation IDによる照会を確認したProviderだけ結果照会を行う。回収不能からの再実行は新attemptとして記録し、許可済みの再実行方針と利用上限を検査する。重複利用がないとは保証しない。

初期の設計値は同時実行1、画像180秒、法令調査600秒、申告review300秒、stdout10MiB、stderr2MiB。再試行可能と確認した失敗だけ最大2回。ユーザー環境で調整する。

Windows Job Object等で起動した子process群だけを管理し、利用者が別に起動したClaude/Codex/Nodeを一括終了しない。

## 8. 法令・制度の監視

国税庁、e-Tax、e-Gov、必要に応じ財務省・中小企業庁、各サービス公式ヘルプを登録する。アプリfetcherがURL、取得日時、原文hash、本文差分、版を保存し、AIが変更点と影響を説明する。

公開Web調査jobへ帳簿・氏名・証憑画像を混在させない。AIが挙げたURLをアプリが実際に取得して確認する。HTTPS、許可domain、redirect先、MIME、サイズ、timeoutを検査し、localhost/private IP/file URLを取得しない。

findingには公表日・成立日・公布日・施行日・対象年分・適用取引期間・適用条件・一次出典を持つ。proposal / enacted / promulgated / effective / guidance / unknownを区別する。

検出 → 出典確認 → 影響評価 → 更新提案 → 人の確認 → 実装/設定差分 → 境界値試験 → 版公開の順にする。AIが本番税率、控除表、帳票マッピングを書き換える機能は設けない。通知を既読にしてもルール承認とは扱わない。

「新しい商売手法」の調査は別topicとして利用できるが、税務適用の根拠にはしない。

## 9. 申告モニタリング

| 点検 | 検出例 | 次の操作 |
|---|---|---|
| 取得範囲 | 銀行の欠落月、カード未取得、売上資料の範囲不足 | 該当月・アカウントの取込 |
| 記帳 | 重複候補、未消込売掛、カード残高差 | 根拠明細・伝票を開く |
| 証憑 | 画像なし、読取り未確認、クラウド未同期、検索索引不足 | 証憑の補完 |
| 決算 | 家事按分、減価償却、年跨ぎ、未払・前払 | 決算整理 |
| 個人情報 | 給与等の他所得、控除証明、源泉の未入力 | 申告情報入力 |
| 年版 | 未対応税務ルール、帳票案、古いe-Tax仕様 | 年度対応状態の確認 |
| 提出後 | 受付記録なし、結果不明、納付未確認 | 公式結果と納付の確認 |

決定的検査のerrorをAIが解除できないようにする。AI独自の指摘はsuggestionと明示する。snapshot_revisionが変われば点検結果をstaleにする。マイナンバー、PIN、認証tokenをAIへ渡さない。

## 10. 定期実行

完成後のアプリ内機能として、法令監視は毎日09:00 JST、数値検査は取込・確定後、申告reviewは指定提出予定日の60日前から毎日を初期候補とする。有効化、時間、topic、利用上限は利用者が選ぶ。

Windowsタスクスケジューラから taxdesk monitor run --due --json を同一利用者sessionで起動する。PC停止中の回数をまとめて大量再実行せず、起動後に必要な点検を1回行う。停止中も24時間監視できるとは表示しない。

通知はアプリ内とWindows通知を標準とし、外部メール・チャットは別途設定された場合だけ送る。この設計書を作ることで現在のChatGPTに定期監視を登録することはない。

## 11. 外部AIの操作権限

taxdesk CLIの無token接続はassistant。Windows SID一致だけでownerにしない。reader、assistant、bookkeeper_scoped、ownerを区別する。

確定権限は、利用者がGUIで確認したplanに限りcoreが一回用capabilityを発行する。principal、org、操作、対象ID、revision、plan hash、期限、restore_generationへ束縛する。業務更新・token消費・command結果を同じtransactionで保存し、同じ要求の再送は保存済み結果だけを返す。

idempotencyキーのactor_scopeは永続principal。session IDやPIDを入れず、再ログインでも二重作成を防ぐ。dry-runは業務DB・原本・outbox・command結果を変更せず、外部認証更新や取得も行わない。

帳票の出力先は許可export root配下とし、canonical pathとjunction等を検査。既定で上書きせず、DB・設定・原本・認証fileへの書込みを拒否する。

## 12. 復元後の特別扱い

backup後に外部だけ処理済みの場合に備え、復元開始前にDB外のruntime_control.jsonへ新restore_generationとrecovery_holdを保存する。restorerが保守mutexを保持し、置換・検証中のcore自動起動を拒否する。

旧lease・session・capabilityを失効し、外部処理jobを一律queuedへ戻さない。uploadはcloud登録ID/hash/versionを照会、AIは結果回収またはoutcome_unknown、申告は公式受付結果を確認する。backupに送信attempt自体がない場合もあるため、対象年・税目の提出状態を確認するまで再送は禁止する。

## 13. 受入条件

実画像のClaude抽出、JSON欠落・拒否、画像内命令、ログイン期限切れ、利用上限、親環境API key、tool追加によるCLI版差、子process停止、外部結果不明、改正案と施行済みの分離、snapshot変更、復元後の再送保留を試験する。

これらは実装時の受入仕様。設計作成時点でユーザーのWindows 11/Python 3.13環境・実アカウントで動作確認済みという意味ではない。

## 一次資料

- Claude programmatic execution: https://code.claude.com/docs/en/headless
- Claude CLI reference: https://code.claude.com/docs/en/cli-reference
- Claude Read tool: https://code.claude.com/docs/en/tools-reference
- Claude authentication: https://code.claude.com/docs/en/authentication
- Codex noninteractive: https://developers.openai.com/codex/noninteractive
- Codex CLI reference: https://developers.openai.com/codex/cli/reference
- Codex configuration: https://developers.openai.com/codex/config-reference

確認日: 2026-10-23。CLI仕様、対応option、認証経路は採用する実行版で再確認する。


[設計書の目次](00_README.md)
