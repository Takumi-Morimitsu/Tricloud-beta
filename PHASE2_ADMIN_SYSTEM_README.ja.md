# Tricloud フェーズ2「管理システム」導入ガイド

## 1. 実装の位置づけ

本成果物は、添付された最新 `backend.zip` と、テスト済みのフェーズ1データ保全基盤を土台にしたフェーズ2実装です。一般ユーザー向けElectronアプリへ管理機能を混在させず、次の3層に分離しています。

```text
管理Web (admin-web / 5174)
  ↓ HTTPS + 短期管理JWT
管理API (backend/admin_api.py / 8010)
  ↓ サーバー側DB接続
PostgreSQL / フェーズ1保守ジョブ
```

管理WebがPostgreSQLへ直接接続することはありません。既存Control APIと管理APIは別アプリ、別ポート、別CORS設定、別管理セッションです。

管理画面の見た目は、当面の利用者が1名であることに合わせ、装飾より一覧性と操作確認を優先しています。

## 2. 実装済みの範囲

| 区分 | 実装内容 |
|---|---|
| 管理認証 | DBの`admin`ロールを毎リクエスト確認、5～60分の短期セッション、セッション失効、管理用秘密鍵 |
| 操作保護 | 危険操作で管理者パスワードを再確認し、操作別の固定確認文を要求 |
| 操作監査 | 認証成否、全認証済み管理API要求、変更前後、対象、IP、User-Agent、Request ID、結果をDBへ保存 |
| ダッシュボード | ユーザー、ノード、容量、レプリカ健全性、コピー不足、監査・修復、請求、未承認報酬、送金失敗 |
| データ保全 | オブジェクト検索、レプリカ・監査・修復履歴、コピー不足、強制監査、修復作成・中止・再試行 |
| ノード管理 | 所有者、地域、容量、heartbeat、稼働率、監査・転送成功率、エラー、保持レプリカ、配置・支払停止 |
| ユーザー管理 | 検索、契約、保存量、転送量、停止、不正利用フラグ、共有・DL制限、管理履歴 |
| 課金表示 | Stripe Customer、Subscription、Invoice、Webhook結果、再処理依頼、プラン対応表 |
| 報酬表示 | 月次報酬、調整額、監査・稼働率、承認・保留・取消、送金履歴と失敗理由 |
| リリース台帳 | バージョン、チャンネル、状態、最低対応、強制更新、段階配信率、ノート |
| 実効制御 | ユーザー制限、停止ノードの新規配置・修復先除外、ノード支払停止、承認前支払禁止 |

ノード信頼性スコアは、依頼どおり一切実装していません。画面でも「方針保留」と明示しています。

## 3. 安全上の初期値

次の設定はデフォルトで無効です。

```dotenv
ADMIN_AUTO_MIGRATE=0
PHASE2_ADMIN_CONTROLS_ENABLED=0
```

- 管理API起動だけでDDLを自動適用しません。
- ユーザー停止・共有・ダウンロード制限は、移行と動作確認後に明示的に有効化します。
- 既存フェーズ1の自動監査・自動修復フラグは変更しません。
- 管理画面からStripe送金、GitHub更新、ビルド、配布を直接実行しません。
- Webhookの「再処理」は、後続ワーカー向け依頼レコードの作成だけです。

## 4. 主なファイル

### 新規バックエンド

- `backend/admin_api.py`: 分離された管理API
- `backend/admin_auth.py`: 管理セッション、DBロール確認、再認証
- `backend/admin_service.py`: 管理照会・操作・監査ログ
- `backend/admin_schema.py`: 追加型DBマイグレーション
- `backend/admin_controls.py`: Control API側のユーザー制限制御
- `backend/migrate_phase2_admin.py`: 冪等マイグレーション
- `backend/verify_phase2_admin_schema.py`: 読み取り専用スキーマ検証
- `backend/grant_admin_role.py`: 既存ユーザーへのadminロール付与
- `backend/disable_phase2_controls.py`: 緊急ロールバック補助
- `backend/requirements-dev.txt`: HTTP統合テスト用依存関係

### 管理Web

- `admin-web/`: React + TypeScript + Viteの独立Webアプリ

### 既存コードへの接続

- `backend/control_api_integrated_improved.py`: ユーザー制限、報酬承認・ノード支払停止の強制
- `backend/server.py`: 配置停止ノードをアップロード先から除外
- `backend/replica_repair_service.py`: 配置停止ノードを修復元・修復先から除外
- `backend/auth_util.py`: 本番環境のデフォルトJWT秘密鍵を禁止
- `.env.example`: フェーズ2設定例

## 5. 検証環境への導入順序

必ず本番ではなく、バックアップ済みの検証DBから始めてください。

### 5.1 Python依存関係

PowerShell例:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Linux例:

```bash
cd backend
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements-dev.txt
```

### 5.2 環境変数

`.env.example`を参考に、シェルまたはサービス管理側で設定します。`.env`や秘密情報をZIP、GitHub、ブラウザへ含めないでください。

最低限必要な値:

```dotenv
DATABASE_URL=postgresql://...
TRICLOUD_ENV=development
JWT_SECRET=<十分に長い既存API用秘密値>
ADMIN_JWT_SECRET=<JWT_SECRETとは異なる十分に長い管理用秘密値>
ADMIN_SESSION_TTL_SEC=900
ADMIN_AUTO_MIGRATE=0
PHASE2_ADMIN_CONTROLS_ENABLED=0
```

本番では`JWT_SECRET`または`ADMIN_JWT_SECRET`が未設定・デフォルト値の場合、起動を拒否します。

### 5.3 DBマイグレーション

```powershell
$env:DATABASE_URL = "postgresql://..."
.\.venv\Scripts\python.exe migrate_phase2_admin.py
.\.venv\Scripts\python.exe verify_phase2_admin_schema.py
```

マイグレーションは追加型かつ冪等です。検証DBでは同じコマンドを2回実行し、2回目も成功することを確認してください。`verify_phase2_admin_schema.py`が`"ok": true`になるまで管理APIを公開しないでください。

この処理は監査・修復・送金・リリース配信を開始しません。

### 5.4 管理者ロール

先に通常のTricloudアカウントを1つ作成し、そのメールを十分に確認してから実行します。

```powershell
.\.venv\Scripts\python.exe grant_admin_role.py --email "確認済みメール" --yes
```

メールに一致する既存ユーザーがいない場合は何も変更しません。

### 5.5 管理API

```powershell
.\.venv\Scripts\python.exe -m uvicorn admin_api:app --host 127.0.0.1 --port 8010
```

確認URL:

- `GET http://127.0.0.1:8010/health/live`
- `GET http://127.0.0.1:8010/health/ready`

インターネットへ直接公開せず、TLSリバースプロキシまたはVPNの背後に置いてください。CORSは実際の管理ドメインだけに絞ります。

### 5.6 管理Web

```powershell
cd ..\admin-web
Copy-Item .env.example .env.local
npm ci
npm run dev
```

既定URLは`http://127.0.0.1:5174`です。本番用静的ファイルは`npm run build`で作成します。`vite preview`を本番公開サーバーとして使用しないでください。

### 5.7 制御の有効化

管理画面の読み取り、監査ログ、危険操作の検証が終わるまで、Control APIでは次を維持します。

```dotenv
PHASE2_ADMIN_CONTROLS_ENABLED=0
```

停止制御の統合テスト完了後だけ`1`へ変更し、Control APIを通常の手順で再起動します。管理API自体はこのフラグに依存しません。

## 6. 管理APIの主要パス

| 分類 | パス |
|---|---|
| セッション | `/admin/v1/session` |
| ダッシュボード | `/admin/v1/dashboard` |
| データ保全 | `/admin/v1/integrity/objects` |
| 修復 | `/admin/v1/repairs` |
| ノード | `/admin/v1/nodes` |
| ユーザー | `/admin/v1/users` |
| 課金 | `/admin/v1/billing` |
| 報酬 | `/admin/v1/rewards` |
| リリース | `/admin/v1/releases` |
| 管理監査 | `/admin/v1/audit-logs` |

開発環境では`/docs`からOpenAPIを確認できます。本番ではドキュメントURLを無効化します。

## 7. テスト

```powershell
cd backend\..
.\.venv-test\Scripts\python.exe -m unittest discover -s tests -v
cd admin-web
npm ci
npm run build
```

今回の作成環境では次を確認済みです。

- Pythonコンパイル: 成功
- 既存フェーズ1回帰＋フェーズ2単体: 37件成功
- 管理API HTTP統合・修復境界回帰: 計7件成功（全体44件、既存40件を含む）
- 管理Web TypeScript/Vite本番ビルド: 成功
- 管理Web `npm audit`: 既知の脆弱性0件
- 実PostgreSQL strict repair race / GC race: 4件成功
- ロールバック後の実DataServer/Node再破損・再監査・再修復: 成功（最終3 healthy replicas）

本番Stripeへの通信と実送金は安全上実施していません。請求・報酬・送金失敗の表示と状態遷移は、ローカル合成データだけで検証しています。具体的な検証項目は`docs/PHASE2_TEST_PLAN.ja.md`にあります。

## 8. ロールバック

### 8.1 通常の停止

1. Control APIで`PHASE2_ADMIN_CONTROLS_ENABLED=0`へ戻す。
2. 管理Webへのアクセスを停止する。
3. 管理APIを停止する。
4. 既存Control APIとDataServerが正常であることを確認する。

### 8.2 制御を使用済みの場合

配置停止などが残っている場合は、バックアップと対象件数を確認してから次を実行します。

```powershell
.\.venv\Scripts\python.exe disable_phase2_controls.py --yes
```

この処理は、ユーザー・ノード制限を解除し、管理セッションを失効させ、操作を監査ログへ残します。管理テーブル・リリース台帳・監査履歴は削除しません。

### 8.3 DB

今回のDDLは列・テーブル追加だけです。緊急時に`DROP TABLE`や列削除を行うより、コードと機能フラグを戻して追加テーブルを休眠状態で残す方が安全です。物理削除は、別途バックアップと保持要件を確認した専用メンテナンスとして扱ってください。

## 9. 次フェーズへ残したもの

- ノード信頼性スコア: 方針保留のため未実装
- MFA: 仕組みを追加する前に、当面はVPN・TLS・アクセス元制限を推奨
- アプリ利用バージョン分布、更新障害、実際の強制・段階更新: フェーズ3
- 課金プランの完全なAPI強制、Webhook再処理ワーカー: フェーズ4
- 報酬計算、最低支払額繰越、Transfer/Payout状態分離、自動再試行: フェーズ5

管理画面には既存DBで取得できる課金・報酬情報を表示しますが、未完成の後続ロジックを作ったように見せる処理は入れていません。報酬については、このフェーズで「管理承認済みだけが既存支払APIへ進める」ガードを先に強制しています。

## 10. 参考にした公式資料

- FastAPI: OAuth2/JWT認証、CORS、APIRouterによる分割
- OWASP: Logging Cheat Sheet、REST Security Cheat Sheet、Authorization Cheat Sheet

これらに沿い、短期セッション、毎回のDB権限確認、限定CORS、危険操作の再認証、秘密値を含まない監査ログ、管理APIの分離を採用しています。
