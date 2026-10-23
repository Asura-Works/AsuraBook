# 17 試験・受入条件

版1.0 / 2026-10-23。この章は製品実装時の試験計画。本設計書を作成しただけで試験済みにはならない。

## 1. 検証層

domainの数値/状態、DB制約とtransaction、Application Service/CLI、Provider adapter、GUI、実Windows配布、税年/公式帳票、復元の順に確認する。mockだけで実CLI画像入力や外部契約を検証済みにしない。

## 2. 会計のgolden fixtures

| ID | 入力 | 必須結果 |
|---|---|---|
| AC01 | 借方100/貸方99 | 下書き可、確定拒否 |
| AC02 | カード購入11,000→レシート→引落し | 費用11,000、未払0、根拠保持 |
| AC03 | 売上110,000、fee16,500、源泉10,210、振込料330、入金82,960 | 売上110,000、対象売掛0、源泉は経費外 |
| AC04 | 同日同店1,100円2回 | 正当2件を保持 |
| AC05 | 確定後のUPDATE/DELETE | DB防御拒否 |
| AC06 | 二重取消・再送 | 有効反対1件 |
| AC07 | 月次1件→詳細N件訂正 | 売上重複なし、source有効利用と消込一致 |
| AC08 | 12月売掛110,000、1月10日66,000回収 | 12月末110,000、1月10日44,000 |
| AC09 | AC08を1月15日付で割当取消 | 1月10日44,000を維持、15日110,000 |
| AC10 | 期首元入金100万+利益60万+事業主借10万−貸30万 | 翌期140万 |
| AC11 | 前年費用/未払2万を追加 | 翌期元入金138万、負債増2万、翌期費用増なし |
| AC12 | 税額null/既知0/対象外 | 別状態を保持 |

金額例は架空の試験資料。実際の手数料・源泉率や適用制度を示すものではない。

## 3. 取込・照合

各Providerで正常、encoding、列追加、引用符改行、空欄、返金、同ID変更、同親ID枝番、重複期間、全期間再取得、API途中失敗、account違い、net_only、coverage不足をfixture化する。

原本件数/合計→観測→構成子明細→仕訳→残高を追跡できること。異常行を黙って落とさず、成功/保留/除外の総数が説明できること。

## 4. 証憑・Claude/Codex

実日本語画像100枚以上を用い、複数税率、手書き、薄い印字、長尺、回転、影、PDF多ページ、T番号、年なし、再発行を含める。金額/日付正解率、修正率、Provider/model/CLI/prompt/schema版を記録する。

金額98%・日付95%は目標。未達を隠さず、読めない値がnullになり手入力できることを優先する。

JSON欠落/拒否、途中出力、上限、認証切れ、親環境API key、tool追加、画像内指示、MCP/plugin誤読込、同じ入力の再試行、process残留を試験する。実効認証modeが変われば停止する。

AI処理後に出力保存前停止するfixtureではoutcome_unknown、結果回収先行、新attempt追跡を期待する。重複利用ゼロの保証をtest名にしない。

## 5. CLI・認可

無token=assistant、owner自己申告拒否、reader出力制限、期限切れcap、内容変更plan、同key異payload、再ログイン同主体再送、cap消費とcommitの同時性、保存結果再返却を検証する。

dry-runは業務snapshotと原本の不変を確認する。並行jobがある場合の物理DB/WAL byte変化と、dry-run自身の業務変更を区別して検査する。未初期化dry-runでDBを作らないこと。

export root逸脱、junction、管理領域、既存file上書き、入力原本上書き、XML external entity、archive traversalを拒否する。

## 6. 障害・復旧

DB commit前/後、原本保存後DB登録前、upload成功後応答消失、process lease期限切れ、disk満杯、電源断相当、cloud停止を注入する。半端なposted、二重uploadの誤認、失われた原本を成功表示しない。

backupから新PCへ復旧し、schema/rule、未同期原本、cloud version、snapshotを確認する。復元後は新restore_generation、旧token失効、recovery_hold、公式提出状態確認を必須にする。

## 7. 税務・e-Tax

対象年の公式資料から正解fixtureを作る。適用開始前後、所得/控除の境界、端数、赤字、事業割合、資産、源泉、消費税方式、未対応条件を検証する。

計算一致、帳票合計、XSD、公式ソフト読込は別gate。実ユーザーの申告送信を開発試験で無断実行しない。受付/納付状態の誤認と再送を検証する。

## 8. Windows/UI/性能

Windows 11、Python3.13、実SQLite、採用Qt、実CLI版で実施。日本語/空白/長いpath、IME、DPI100〜200%、複数window、core自動起動、保守mutex、cancel、子process群を検証する。

10万仕訳、1万行CSV、原本多数を用いて読取・検索・出力・WAL・memoryを測定する。測定端末とデータ規模を付記する。GUIが通信待ちで停止しないこと。

## 9. リリース判定

G0 runtime、G1会計、G2取込/原本、G3外部/AI、G4対象税年、G5運用/配布を分ける。機能ごとにpassed/failed/not_run/blockedと根拠を保存する。

Linuxや設計用SQLの検査結果をWindows製品合格に読み替えない。契約未締結、CSV未受領、正式年版未公表はblockedとし、機能未達を隠さない。

## 10. 検証記録

run ID、commit、app/schema/rule/profile/CLI/model版、OS/runtime、fixture hash、期待値、実測、証跡、実施日、未解決を保持する。個人データをfixture公開物へ混入させず、匿名化/架空資料を使う。


[設計書の目次](00_README.md)
