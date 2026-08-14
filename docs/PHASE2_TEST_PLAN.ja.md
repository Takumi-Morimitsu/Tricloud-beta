# フェーズ2 管理システム 検証DBテスト計画

## 1. 前提

- 本番ではなく、バックアップ済みの検証用PostgreSQLを使う。
- Stripeを確認する場合はテストモードだけを使う。
- `ADMIN_AUTO_MIGRATE=0`、`PHASE2_ADMIN_CONTROLS_ENABLED=0`から開始する。
- フェーズ1の監査・修復フラグは、現在テスト済みの設定を維持する。
- DB URL、JWT、Stripe、ノードAPIキーをテストログへ出さない。

## 2. 自動テスト

```powershell
cd <展開先>
python -m venv .venv-test
.\.venv-test\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
.\.venv-test\Scripts\python.exe -m unittest discover -s tests -v
cd admin-web
npm ci
npm run build
```

strict repair raceを実PostgreSQLのtransaction境界で確認する場合は、専用ローカル検証DBを明示して次を実行する。テストはlocalhost以外や許可されていないDB名を拒否する。

```powershell
$env:RUN_PG_INTEGRATION="1"
$env:TRICLOUD_PG_TEST_DATABASE_URL="postgresql://<USER>:<PASSWORD>@127.0.0.1:5432/<TEST_DB>"
.\.venv-test\Scripts\python.exe -m unittest integration_tests.test_repair_race_postgres -v
```

期待結果:

- 全Pythonテスト44件成功、skip 0
- 管理APIの未認証アクセスが401
- 危険操作の誤った確認文が400
- 正しい再認証・確認文でサービス呼び出しが1回だけ行われる
- `REPLICA_REPAIR_QUEUE_ENABLED=0`では監査結果からrepairをenqueueせず、ONでは必要時にenqueueする
- healthy replicaが2件ならmanual repairを許可し、3件以上なら拒否する
- 同一objectのrepair完了とmanual admissionが競合しても、commit済みreplicaを不要な4コピーにしない
- repair publishとobject GCが競合してもorphan replicaを残さない
- 管理WebのTypeScript検査とViteビルド成功

## 3. マイグレーション

1. DBバックアップを取得する。
2. `migrate_phase2_admin.py`を実行する。
3. `verify_phase2_admin_schema.py`が`ok: true`を返す。
4. マイグレーションをもう一度実行する。
5. 2回目も成功し、行や制約が重複しないことを確認する。
6. 既存フェーズ1のオブジェクト、レプリカ、監査、修復件数が意図せず変化していないことを確認する。

## 4. 認証・権限

| テスト | 期待結果 |
|---|---|
| adminロールなしでログイン | 401 |
| adminロールあり、正しい認証情報 | 短期管理トークン発行 |
| 一般APIのJWTを管理APIへ送る | 401 |
| 管理JWTの署名を改変 | 401 |
| セッション期限切れ | 401 |
| ログイン後にDBからadminロールを外す | 次の要求から401 |
| ログアウト済みセッションを再利用 | 401 |
| 管理者自身を停止しようとする | 拒否 |
| 許可していないOrigin | CORSで拒否 |
| パスワード再確認なしの危険操作 | 422または403 |
| 確認文が不一致 | 400 |

管理APIをインターネットへ公開する前に、VPNまたはIP制限も確認する。

## 5. 監査ログ

1. ログイン成功・失敗を発生させる。
2. 各画面を1回ずつ開く。
3. ノード・ユーザー制御を変更して元へ戻す。
4. 監査投入、修復作成・中止・再試行をテスト対象で行う。
5. `admin_audit_logs`へ管理者、操作、対象、変更前後、IP、User-Agent、Request ID、結果が残ることを確認する。
6. JWT、パスワード、DB URL、Stripe秘密鍵、ノードAPIキー、暗号鍵が保存されていないことを確認する。

## 6. データ保全管理

1. 正常、pending、missing、corruptを含む検証データを用意する。
2. オブジェクト検索結果と詳細のレプリカ状態がDBと一致する。
3. コピー不足一覧がフェーズ1検出結果と一致する。
4. 強制監査を2回続けて実行しても、同一レプリカの有効ジョブが重複しない。
5. 手動修復を同一オブジェクトへ連続作成しても、有効ジョブが重複しない。
6. 修復中止と再試行の状態・イベント履歴が正しい。
7. DataServer停止中の投入でもジョブが失われず、復旧後に処理される。

## 7. ノード制御

1. 検証ノードAを`placement_paused=true`にする。
2. 新規アップロード先にAが選ばれない。
3. 修復先にもAが選ばれない。
4. 既存レプリカは削除されない。
5. 配置を再開すると、容量・地域など他条件を満たす場合だけ候補へ戻る。
6. `payouts_paused=true`のノードで支払要求が403になる。
7. 強制監査は配置停止中でも投入できる。

## 8. ユーザー制御

`PHASE2_ADMIN_CONTROLS_ENABLED=1`へ切り替えた専用検証環境で行う。

| 制御 | 期待結果 |
|---|---|
| suspended | 新規ログインと既存Bearer要求を403 |
| sharing_disabled | 共有作成・送信・受取系を403、通常一覧は利用可能 |
| downloads_disabled | ダウンロードトークン・UIダウンロードを403、通常一覧は利用可能 |
| abuse_flagのみ | 管理上の表示だけで、自動停止しない |
| DB一時停止 | 制御を確認できない要求は503で失敗閉鎖 |

解除後に通常利用へ戻ることも確認する。

## 9. 課金・報酬

- Subscription、Invoice、Webhook、プラン対応表がDBと一致する。
- 同一Webhookへ再処理依頼を2回行っても、`requested`が重複しない。
- 再処理依頼だけではStripe APIが呼ばれない。
- `calculated`または`held`の報酬は既存支払APIで拒否される。
- `approved`かつ`payout_enabled=true`、`payouts_paused=false`のテスト報酬だけがStripeテストモードへ進む。
- 支払済み報酬の状態変更が拒否される。
- 失敗理由とprovider referenceが管理画面へ表示される。

実送金、本番Stripe、フェーズ5の自動支払は試験対象外。

## 10. 障害テスト

- PostgreSQL停止中: `/health/ready`が503、管理API操作が成功扱いにならない。
- 管理監査ログ書込み失敗: 管理要求が成功扱いにならない。
- 管理WebからAPI到達不能: 明示的なエラーを表示し、無限再試行しない。
- DataServer停止: 管理画面はジョブ状態を表示し、投入済みジョブを失わない。
- セッション途中でadminロール剥奪: 次要求で失効。
- 同一危険操作をブラウザで再送: DB側の冪等性が保たれる。

## 11. ロールバック演習

1. `PHASE2_ADMIN_CONTROLS_ENABLED=0`へ戻す。
2. `disable_phase2_controls.py --yes`を検証DBで実行する。
3. 配置・支払・ユーザー制限が解除され、管理セッションが失効する。
4. 管理APIと管理Webを停止する。
5. 既存Control API、DataServer、Node、フェーズ1機能が動作する。
6. 管理テーブルと監査履歴を削除せずに復旧できる。

## 12. 合格条件

- 上記の認証・権限・監査・保全・ノード・ユーザー・障害・ロールバック試験がすべて成功する。
- 重大操作が監査ログなしで完了しない。
- 管理者承認前の支払が不可能。
- 既存Electronアプリの日本語・英語・スペイン語画面とフェーズ1動作に回帰がない。
- ノード信頼性スコアがDB、API、候補選定、画面へ混入していない。
