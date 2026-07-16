# -*- coding: utf-8 -*-
"""
meta_db_pg.py

Phase1 テストモデル用のPostgreSQL基本スキーマ。
各追加パッチ（database_extensions.py / items_phase2_patch.py など）は、
この init_schema() 実行後に拡張DDLを適用する前提です。
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator

import psycopg

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


def now_ts() -> int:
    return int(time.time())


@contextmanager
def db_conn() -> Iterator[psycopg.Connection]:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required. Copy .env.example to a local .env file and set a private database connection string.")
    conn = psycopg.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()


def init_schema() -> None:
    """バックエンド全体の土台になる最小スキーマを作成する。"""
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            last_name TEXT,
            first_name TEXT,
            country_code TEXT,
            terms_version TEXT,
            privacy_policy_version TEXT,
            terms_accepted_at INTEGER,
            privacy_policy_accepted_at INTEGER
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_users_email_lower ON users(lower(email))",
        "CREATE INDEX IF NOT EXISTS idx_users_country_code ON users(country_code)",
        """
        CREATE TABLE IF NOT EXISTS user_roles (
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY(user_id, role)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS billing_plans (
            plan_id TEXT PRIMARY KEY,
            monthly_base INTEGER NOT NULL DEFAULT 0,
            storage_yen_per_gb_month REAL NOT NULL DEFAULT 0,
            ingress_yen_per_gb REAL NOT NULL DEFAULT 0,
            egress_yen_per_gb REAL NOT NULL DEFAULT 0,
            daily_egress_gb_limit REAL,
            daily_shared_egress_gb_limit REAL,
            created_at INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            current_period_start INTEGER,
            current_period_end INTEGER,
            updated_at INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS nodes (
            node_id TEXT PRIMARY KEY,
            last_seen INTEGER NOT NULL DEFAULT 0,
            capacity_bytes BIGINT NOT NULL DEFAULT 0,
            reserved_bytes BIGINT NOT NULL DEFAULT 0,
            meta_json TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS node_profiles (
            node_id TEXT PRIMARY KEY,
            owner_user_id TEXT,
            created_at INTEGER,
            node_name TEXT,
            desired_capacity_bytes BIGINT NOT NULL DEFAULT 0,
            updated_at INTEGER,
            node_api_key TEXT,
            stripe_connected_account_id TEXT,
            payout_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            payouts_paused BOOLEAN NOT NULL DEFAULT FALSE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_node_profiles_owner_created ON node_profiles(owner_user_id, created_at)",
        """
        CREATE TABLE IF NOT EXISTS objects (
            file_object_id TEXT PRIMARY KEY,
            owner_user_id TEXT NOT NULL,
            size_bytes BIGINT NOT NULL,
            chunk_size INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_objects_owner_created ON objects(owner_user_id, created_at)",
        """
        CREATE TABLE IF NOT EXISTS replicas (
            file_object_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY(file_object_id, node_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_replicas_node ON replicas(node_id)",
        """
        CREATE TABLE IF NOT EXISTS upload_sessions (
            session_id TEXT PRIMARY KEY,
            file_object_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_size BIGINT NOT NULL,
            chunk_size INTEGER NOT NULL,
            node_ids TEXT NOT NULL,
            status TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            shared_mode TEXT NOT NULL DEFAULT 'normal',
            shared_parent_id TEXT,
            shared_replace_item_id TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_upload_sessions_file ON upload_sessions(file_object_id)",
        """
        CREATE TABLE IF NOT EXISTS items (
            item_id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            parent_id TEXT,
            name TEXT NOT NULL,
            size_bytes BIGINT NOT NULL DEFAULT 0,
            file_object_id TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER,
            trashed_at INTEGER,
            trash_batch_id TEXT,
            owner_user_id TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_items_owner_parent ON items(owner_user_id, parent_id)",
        "CREATE INDEX IF NOT EXISTS idx_items_owner_trash ON items(owner_user_id, trashed_at)",
        """
        CREATE TABLE IF NOT EXISTS shares (
            share_id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL,
            owner_user_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'viewer',
            expires_at INTEGER,
            revoked_at INTEGER,
            created_at INTEGER NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_shares_item ON shares(item_id)",
        """
        CREATE TABLE IF NOT EXISTS download_tokens (
            token TEXT PRIMARY KEY,
            file_object_id TEXT NOT NULL,
            owner_user_id TEXT NOT NULL,
            charge_user_id TEXT NOT NULL,
            is_shared BOOLEAN NOT NULL DEFAULT FALSE,
            expires_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_download_tokens_object ON download_tokens(file_object_id)",
        """
        CREATE TABLE IF NOT EXISTS transfer_events (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            direction TEXT NOT NULL,
            bytes BIGINT NOT NULL,
            ts INTEGER NOT NULL,
            file_object_id TEXT,
            is_shared BOOLEAN NOT NULL DEFAULT FALSE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_transfer_events_user_ts ON transfer_events(user_id, ts)",
        """
        CREATE TABLE IF NOT EXISTS object_lifetimes (
            file_object_id TEXT NOT NULL,
            owner_user_id TEXT NOT NULL,
            size_bytes BIGINT NOT NULL,
            start_ts INTEGER NOT NULL,
            end_ts INTEGER,
            PRIMARY KEY(file_object_id, start_ts)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_object_lifetimes_owner ON object_lifetimes(owner_user_id, start_ts, end_ts)",
        """
        CREATE TABLE IF NOT EXISTS invoices (
            invoice_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            period_start INTEGER NOT NULL,
            period_end INTEGER NOT NULL,
            subtotal INTEGER NOT NULL,
            total INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_invoices_user_period ON invoices(user_id, period_start)",
        """
        CREATE TABLE IF NOT EXISTS invoice_lines (
            line_id BIGSERIAL PRIMARY KEY,
            invoice_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            quantity REAL NOT NULL,
            unit_yen REAL NOT NULL,
            amount_yen INTEGER NOT NULL
        )
        """,
    ]
    with db_conn() as conn:
        with conn.cursor() as cur:
            for stmt in ddl:
                cur.execute(stmt)
        conn.commit()


def ensure_default_plan() -> None:
    """開発用の標準プランを必ず用意する。"""
    init_schema()
    now = now_ts()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO billing_plans(
                    plan_id, monthly_base, storage_yen_per_gb_month,
                    ingress_yen_per_gb, egress_yen_per_gb,
                    daily_egress_gb_limit, daily_shared_egress_gb_limit, created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (plan_id) DO NOTHING
                """,
                ("dev-standard", 0, 0.0, 0.0, 0.0, 50.0, 10.0, now),
            )
        conn.commit()
