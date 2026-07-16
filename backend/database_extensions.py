# -*- coding: utf-8 -*-
"""
既存データベース_pg.py に追加で適用する拡張スキーマ（ノード報酬 + Stripe連携）
- 既存の init_schema() を呼んだ後に init_rewards_and_stripe_schema() を呼ぶ運用を想定
"""
from __future__ import annotations

try:
    from meta_db_pg import db_conn, init_schema, now_ts
except Exception:
    from database_pg import db_conn, init_schema, now_ts  # type: ignore


def init_rewards_and_stripe_schema() -> None:
    init_schema()
    ddl = [
        # node_profiles 拡張（Stripe Connectの接続先アカウント保存用）
        "ALTER TABLE node_profiles ADD COLUMN IF NOT EXISTS stripe_connected_account_id TEXT",
        "ALTER TABLE node_profiles ADD COLUMN IF NOT EXISTS payout_enabled BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE node_profiles ADD COLUMN IF NOT EXISTS payouts_paused BOOLEAN NOT NULL DEFAULT FALSE",

        # replica_lifetimes（将来的な正確なノード報酬計算用。未使用でも作成しておく）
        """
        CREATE TABLE IF NOT EXISTS replica_lifetimes (
            id BIGSERIAL PRIMARY KEY,
            file_object_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            size_bytes BIGINT NOT NULL,
            start_ts INTEGER NOT NULL,
            end_ts INTEGER,
            UNIQUE (file_object_id, node_id, start_ts)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_replica_lifetimes_node ON replica_lifetimes(node_id, start_ts)",
        "CREATE INDEX IF NOT EXISTS idx_replica_lifetimes_obj ON replica_lifetimes(file_object_id, start_ts)",

        # ノード報酬台帳
        """
        CREATE TABLE IF NOT EXISTS node_earnings (
            earning_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            period_start INTEGER NOT NULL,
            period_end INTEGER NOT NULL,
            gb_month REAL NOT NULL,
            share_ratio REAL NOT NULL,
            pool_amount_yen INTEGER NOT NULL,
            gross_amount_yen INTEGER NOT NULL,
            adjustments_yen INTEGER NOT NULL DEFAULT 0,
            net_amount_yen INTEGER NOT NULL,
            status TEXT NOT NULL, -- calculated|approved|paid|void
            note TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_node_earnings_period ON node_earnings(node_id, period_start, period_end)",
        "CREATE INDEX IF NOT EXISTS idx_node_earnings_status ON node_earnings(status)",

        # ノード支払い実行履歴
        """
        CREATE TABLE IF NOT EXISTS node_payouts (
            payout_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            amount_yen INTEGER NOT NULL,
            currency TEXT NOT NULL DEFAULT 'jpy',
            provider TEXT NOT NULL, -- stripe_connect|manual
            provider_ref TEXT,
            status TEXT NOT NULL, -- pending|paid|failed|canceled
            created_at INTEGER NOT NULL,
            paid_at INTEGER,
            failure_reason TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_node_payouts_node ON node_payouts(node_id, created_at)",

        # Stripe顧客マッピング（クライアント課金用）
        """
        CREATE TABLE IF NOT EXISTS stripe_customers (
            user_id TEXT PRIMARY KEY,
            stripe_customer_id TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL
        )
        """,

        # Stripe price マッピング（アプリの plan_id と Stripe Price を紐づけ）
        """
        CREATE TABLE IF NOT EXISTS stripe_plan_prices (
            plan_id TEXT PRIMARY KEY,
            stripe_price_id TEXT NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at INTEGER NOT NULL
        )
        """,

        # Webhook idempotency 用
        """
        CREATE TABLE IF NOT EXISTS stripe_webhook_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            processed_at INTEGER NOT NULL,
            status TEXT NOT NULL,
            detail TEXT
        )
        """,
    ]
    with db_conn() as conn:
        with conn.cursor() as cur:
            for s in ddl:
                cur.execute(s)
        conn.commit()


def ensure_reward_defaults() -> None:
    # 将来必要ならここでデフォルト設定テーブル等を初期化
    return


# ---- multipart (複数オブジェクト) + ユーザー借用リソース記録 ----

def init_multipart_and_quota_schema() -> None:
    """複数オブジェクト（multipart）対応の追加テーブルと、ユーザー借用リソース記録を作成"""
    init_schema()
    now = now_ts()
    ddls = [
        # ユーザーが借りている論理ストレージ量（レプリカ分は含めない）
        """
        CREATE TABLE IF NOT EXISTS user_storage_allocations (
            user_id TEXT PRIMARY KEY,
            allocated_bytes BIGINT NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL
        );
        """,
        # multipartアップロード（論理ファイル）
        """
        CREATE TABLE IF NOT EXISTS multipart_uploads (
            upload_id TEXT PRIMARY KEY,
            owner_user_id TEXT NOT NULL,
            file_name TEXT NOT NULL,
            total_size BIGINT NOT NULL,
            chunk_size INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            finalized_item_id TEXT
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_multipart_uploads_owner ON multipart_uploads(owner_user_id, created_at);",
        # multipartの各パート（=各オブジェクト/各セッション）
        """
        CREATE TABLE IF NOT EXISTS multipart_parts (
            upload_id TEXT NOT NULL,
            part_index INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            file_object_id TEXT NOT NULL,
            part_offset BIGINT NOT NULL,
            part_size BIGINT NOT NULL,
            node_ids TEXT NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY (upload_id, part_index)
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_multipart_parts_session ON multipart_parts(session_id);",
        # 完成した論理ファイル item と、そのパート構成
        """
        CREATE TABLE IF NOT EXISTS item_parts (
            item_id TEXT NOT NULL,
            part_index INTEGER NOT NULL,
            file_object_id TEXT NOT NULL,
            part_offset BIGINT NOT NULL,
            part_size BIGINT NOT NULL,
            PRIMARY KEY (item_id, part_index)
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_item_parts_item ON item_parts(item_id);",
    ]
    with db_conn() as conn:
        with conn.cursor() as cur:
            for s in ddls:
                cur.execute(s)
        conn.commit()



def init_auth_profile_schema() -> None:
    """users テーブルへ認証プロフィール列を追加する。
    country_code は ISO 3166-1 alpha-2 の 2文字コードを想定し、
    新規/更新時の形式を DB 側でも制約する。"""
    init_schema()
    ddl = [
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='users') THEN
                EXECUTE 'ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name TEXT';
                EXECUTE 'ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT';
                EXECUTE 'ALTER TABLE users ADD COLUMN IF NOT EXISTS country_code TEXT';
                EXECUTE 'ALTER TABLE users ADD COLUMN IF NOT EXISTS terms_version TEXT';
                EXECUTE 'ALTER TABLE users ADD COLUMN IF NOT EXISTS privacy_policy_version TEXT';
                EXECUTE 'ALTER TABLE users ADD COLUMN IF NOT EXISTS terms_accepted_at INTEGER';
                EXECUTE 'ALTER TABLE users ADD COLUMN IF NOT EXISTS privacy_policy_accepted_at INTEGER';
            END IF;
        END $do$;
        """,
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='users') THEN
                EXECUTE 'CREATE INDEX IF NOT EXISTS idx_users_country_code ON users(country_code)';
            END IF;
        END $do$;
        """,
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='users') THEN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname='chk_users_country_code_iso_alpha2'
                ) THEN
                    EXECUTE 'ALTER TABLE users ADD CONSTRAINT chk_users_country_code_iso_alpha2 CHECK (country_code IS NULL OR country_code ~ ''^[A-Z]{2}$'') NOT VALID';
                END IF;
            END IF;
        END $do$;
        """,
    ]
    with db_conn() as conn:
        with conn.cursor() as cur:
            for s in ddl:
                cur.execute(s)
        conn.commit()
