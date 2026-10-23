# 19 出典・設計判断・未確定事項

版1.0 / 2026-10-23。外部仕様は確認日の情報。正式な対象年・契約・採用runtimeで再検証する。各章の末尾に該当する一次資料を掲載している。

## 1. 判断の区分

「ユーザー確定」は変更しない要件。「採用設計」は本書で決めた構成。「仮定」は設定で確認する条件。「実装gate」は設計を進めても本番対応を宣言する前に必要な証拠。

| ID | 判断 | 区分 | 根拠・理由 |
|---|---|---|---|
| D01 | Windows11/Python3.13/uv/PySide6/SQLite | ユーザー確定 | 指定環境 |
| D02 | 対象の5社にクラウドワークスを含む | ユーザー確定 | 明示訂正を反映 |
| D03 | Claudeで画像を読み、claude -p/codex execを利用 | ユーザー確定 | 追加要件 |
| D04 | 個人事業・一般用青色を中心にする | 仮定 | 申告区分は初期設定で確認 |
| D05 | ローカル会計DBとcloud原本を分離 | 採用設計 | offline操作と原本保全 |
| D06 | coreが唯一のSQLite writer | 採用設計 | GUI/CLIの整合と競合管理 |
| D07 | 金額INTEGER円、比率Decimal | 採用設計 | 浮動小数誤差と不明値混同を防ぐ |
| D08 | posted不変、元1→反対1→置換N | 採用設計 | 訂正履歴・集計から明細への移行 |
| D09 | 観測とeconomic eventと仕訳を分ける | 採用設計 | 再取込と二重計上を防ぐ |
| D10 | open_item IDは年度継続 | 採用設計 | opening二重債権と過去残誤計算を防ぐ |
| D11 | 文書/物理object/取引索引を分ける | 採用設計 | 複数取引PDFと索引履歴 |
| D12 | Claude/Codexの認証modeを起動前検証 | 採用設計 | 意図しないAPI課金/送信先変更を防ぐ |
| D13 | AIは抽出・提案・説明、数値はPython | 採用設計 | 再現可能な帳簿・税務計算 |
| D14 | 制度監視の「新しい商法」の解釈 | 仮定 | 法令変更を中心、商売手法は別topic |
| D15 | カードCSVはAPI取得の代替と明示 | 採用設計 | 自動取得要件の達成を誤認しない |
| D16 | E1→E2を先に、E3は追加gate | 採用設計 | 公式読込経路からオンライン申告へ |
| D17 | 紙撮影は初期に紙保管を継続 | 採用設計 | scanner保存要件の確認前に廃棄しない |
| D18 | 復元後に外部jobを保留 | 採用設計 | backup後の外部成功と二重実行対策 |

## 2. 実装前に具体化する事項

| 項目 | 必要な情報 | 確定する段階 |
|---|---|---|
| 銀行 | 銀行/商品/口座、匿名CSV複数月 | M0〜M1 profile |
| カード | 発行会社/契約、API対象、利用履歴範囲 | M0調査、M3本番 |
| 売上5社 | 契約区分、匿名CSV/PDF/画面、返金/源泉例 | M0〜M2 |
| 税務 | 対象年、所得区分、青色承認、消費税/インボイス、他所得 | 初期設定とM4 |
| 移行 | 開始残高、未回収/未払、前期申告資料 | M1開始前 |
| AI | 導入CLI版、本人ログインmode、利用可能model、利用上限 | M0 |
| cloud | 提供先/region、保存条件、費用上限、完全backup | M0〜M1 |
| e-Tax | 対象税目/帳票/年度、正式schema、公式読込 | M0調査、M4本番 |
| 配布 | 採用runtime/packager、license、署名、更新経路 | M0〜G5 |
| 保存運用 | 規程、責任者、保管年限、scannerへの切替条件 | M1運用前 |

これらは設計作成を止める質問ではない。該当機能の具体的設定・契約・実証のgateとして残す。

## 3. 特に確認した外部仕様

### AI

Claudeは非対話実行・構造化出力・画像Readの公式仕様を持つ。--bareの認証経路、--toolsと--allowedToolsの違い、MCPの制限、API keyの認証優先を考慮する。Codexはexec、JSONLと最終出力、schema、画像、sandbox/configの採用版を確認する。

- https://code.claude.com/docs/en/headless
- https://code.claude.com/docs/en/cli-reference
- https://code.claude.com/docs/en/tools-reference
- https://code.claude.com/docs/en/authentication
- https://developers.openai.com/codex/noninteractive
- https://developers.openai.com/codex/cli/reference
- https://developers.openai.com/codex/config-reference

### e-Tax

民間作成.xtxのWEB版読込は公式案内がある。公開APIと受付システムinterfaceは役割が異なる。年版と対象帳票を検証してから有効化する。

- https://www.e-tax.nta.go.jp/toiawase/qa/e-taxweb/49.htm
- https://www.e-tax.nta.go.jp/shiyo/api.htm
- https://www.e-tax.nta.go.jp/shiyo/index.htm
- https://www.e-tax.nta.go.jp/shiyo/ksk2/ksk2_shiyo3.htm

### 税務の適用日

2027年分の青色控除と2026-10-01の仕入経過措置など、現在の資料は古い説明から変更されている。対象年・取引日・本人条件をruleへ保持する。検討案と施行済みを混同しない。

- https://www.nta.go.jp/taxes/shiraberu/shinkoku/kojin_jigyo/0026007-003_02.pdf
- https://www.nta.go.jp/taxes/shiraberu/zeimokubetsu/shohi/keigenzeiritsu/pdf/qa/113.pdf
- https://www.nta.go.jp/taxes/shiraberu/taxanswer/shotoku/2100.htm
- https://www.nta.go.jp/taxes/shiraberu/taxanswer/shotoku/1199.htm

### 取込

Moneytree LINKはclient情報・redirect登録等が必要。個人開発やWindows条件は契約確認事項。MENTAのCSV/API、ランサーズ年間CSVの全列、各社の過去分網羅性を推測で埋めない。note pro向けAPIの存在を、個人会計売上APIの確認と同一視しない。

- https://docs.link.getmoneytree.com/docs/getting-started
- https://www.lancers.jp/faq/l1037/497
- https://help.coconala.com/hc/ja/articles/900000064326
- https://intercom.help/mentajp/ja/articles/3025557
- https://blog.crowdworks.jp/archives/5636/
- https://www.help-note.com/hc/ja/articles/360010430173
- https://www.help-note.com/hc/ja/articles/46643492548121

### 参照製品

TKCの仕訳、金融データ、証憑連携を機能整理の参考とした。自作アプリがTKCの連携契約・認証・法令適合を継承するものではない。

- https://www.tkc.jp/fx/
- https://www.tkc.jp/fx/fintech/
- https://www.tkc.jp/fx/tds/

## 4. 仕様の優先順位

ユーザー確定要件 → 各分野の正本契約 → 対応する対象年の公式資料 → 検証済みProvider profile → 画面/CLIの例、の順で矛盾を解消する。

共通永続化は03、会計は04、税務は11、提出は12、保存制度は13、CLIは15、復元は16、AIは21。表示上の短いstate名をDB enumへ暗黙に追加しない。

## 5. 確認済みと未検証

設計の主要な相互矛盾は、訂正のsource利用、消込原子性、年跨ぎ、証憑多索引、AI結果不明、認証mode、復元後再送を中心にレビューした。

ユーザーPC上の完成アプリ、実カード連携、正式年版の税計算、e-Tax送信、法令適合認証は未実施。本書の例と試験計画を実装の受入条件として用いる。

本設計は依頼機能を章ごとに読める文書として保存した。Markdown/JSONの内容と出典URLをまとめた配布用データも同じ本文から作成する。環境障害前の未検証SQL draftを、この版の正式な製品migrationとして混在させない。


[設計書の目次](00_README.md)
