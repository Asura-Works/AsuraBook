# 09 証憑クラウド保存・セキュリティ

版1.0 / 2026-10-23。クラウド原本保管はM1に含める。帳簿SQLiteの常時クラウド同期とは別機能。

## 1. 採用構成

Windowsアプリ → HTTPS証憑gateway → 非公開object storage。初期参照構成はFastAPI単一instance、gateway専用metadata SQLite、AWS S3東京region等のS3互換storage。提供先・region・費用は利用者の設定で確定する。

会計DBはWindowsのローカルNTFSへ置く。gateway DBは保管状態だけを管理し、会計DBの複製にしない。多数利用者や複数gateway instanceへ拡大する場合はmetadata DBと競合制御を再設計する。

## 2. 原本objectと検索文書

証憑文書、物理object、取引索引行を分離する。1つの明細PDFに複数の日付・金額・相手先を索引として持てる。返金の符号と取引種別を保つ。索引訂正は新versionとして旧値を残す。

原本byte列のSHA-256、サイズ、MIME、取得日時、cloud object key/version、保存確認を管理する。派生画像は親原本に関連する別object。人名や店名をobject keyへ直接入れず、内部UUID等を使う。

## 3. upload手順

1. 原本をローカルへ永続化しhashを確定。
2. coreが証憑登録とsync outboxを同transactionで作る。
3. workerがdevice tokenでgatewayへupload開始を要求。
4. gatewayがorg/object/hash/size/MIMEに結び付けた短期URLを発行。
5. workerがuploadし、gatewayがchecksum/size/versionを検証。
6. coreが確認結果を保存してverifiedにする。

通信をDB transaction内で待たない。途中停止はoutboxのleaseと冪等keyで再開する。upload完了応答が失われた場合は登録ID/hash/versionの照会を先行し、同名objectを無条件上書きしない。ETagをSHA-256と同じものと仮定しない。

## 4. 状態と削除保留

upload状態はpending、uploading、uploaded_unverified、verified、retry_wait、failed等の表示へ写像し、物理enumは03章に統一する。OCRの完了や仕訳のposted状態とは別に表示する。

retention_holdはupload状態と直交する削除保留条件。クラウド未到達でも、到達済みでも同時にtrueになり得る。hold解除は理由・主体・対象を監査へ残す。

原本の保存期間とbackup世代保持を混同しない。法定要件、申告対象年、調査/係争等の保留を確認し、期日だけで無条件削除しない。13章の方針を優先する。

## 5. 認証と認可

device tokenは利用者のWindows Credential Manager等へ保持し、SQLiteには参照IDのみ。token失効、端末再登録、紛失時の停止を提供する。storageの長期access keyをexeやAI jobへ埋め込まない。

gatewayはorg/object単位の所有者確認を毎回行う。URLを知っているだけで任意objectへアクセスできないようにする。presigned URLは期限まで再利用され得るため、短寿命・method/key/checksum制約とし、logへ残さない。

TLS、storage encryption、非公開bucket、最小権限role、アクセス監査を設定する。本人以外へ共有する機能は別権限・期限付きとする。Windows通知へ証憑内容やtokenを表示しない。

## 6. Object Lockと保存法

必要なretention要件を満たすためにversioning/Object Lockを検討する。ただしobject version単位の保持であり、別versionや削除markerの扱いを理解した設定が必要。S3互換なら全製品が同じ機能とは仮定せず実装差を検証する。

hash、versioning、Object Lockを付けただけで電子帳簿保存法への適合を自動認定しない。取得方法、保存期間、訂正履歴、検索、見読性、事務処理規程等を合わせて確認する。紙廃棄は13章の条件による。

## 7. AIへ渡す範囲

AI jobへは指定した派生画像・必要最小限のsnapshotだけを渡す。マイナンバー、PIN、認証token、storage keyは渡さない。公開Web調査と私的帳簿reviewは別jobにする。

画像に書かれた命令やURLを実行しない。schema検証・tool制限・最小環境変数・実効認証mode検査を行う。AIの出力をSQL、shell、ファイルpathとしてそのまま使わない。

## 8. オフラインと復元

クラウド停止中はローカル原本を保持し再同期待ちを明示する。未同期原本はbackupへ必ず含める。verified原本を参照のみでbackupする軽量方式と、全原本を含める完全backupを区別する。

復元後は16章のrecovery_holdを先に適用する。cloudにbackup後の原本があっても新しい仕訳を勝手に作らず、孤立原本として照合する。保管先のaccount喪失を想定した完全backup復旧も試験する。

## 9. 受入条件

期限切れURL、違うorgのobject、改変hash、upload成功後の応答消失、disk満杯、cloud停止、端末token失効、復元後の再upload、version付き復元、索引訂正、retention_hold併用を確認する。失敗をverified表示にしない。

### 一次資料

S3 Object Lock: https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html
S3 presigned URLs: https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html

確認日2026-10-23。構成は本アプリの採用案であり、未契約サービスを作成済みとは意味しない。


[設計書の目次](00_README.md)
