# -*- coding: utf-8 -*-
"""Additive PostgreSQL schema for the Tricloud Phase 2 admin system.

This module only creates metadata/control tables.  It never starts audits,
repairs, payouts, or release deployment.
"""

from __future__ import annotations

from typing import Dict, Iterable, List

from meta_db_pg import db_conn, now_ts


MIGRATION_ID = "002_admin_system"

PHASE2_ADMIN_DDL = [
    """
    CREATE TABLE IF NOT EXISTS tricloud_schema_migrations (
        migration_id TEXT PRIMARY KEY,
        applied_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_sessions (
        session_id TEXT PRIMARY KEY,
        admin_user_id TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        last_seen_at INTEGER NOT NULL,
        revoked_at INTEGER,
        ip_address TEXT,
        user_agent TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_admin_sessions_user ON admin_sessions(admin_user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_admin_sessions_expiry ON admin_sessions(expires_at, revoked_at)",
    """
    CREATE TABLE IF NOT EXISTS admin_audit_logs (
        log_id TEXT PRIMARY KEY,
        admin_user_id TEXT NOT NULL,
        action TEXT NOT NULL,
        target_type TEXT,
        target_id TEXT,
        before_json JSONB,
        after_json JSONB,
        ip_address TEXT,
        user_agent TEXT,
        request_id TEXT,
        result_status TEXT NOT NULL DEFAULT 'success',
        error_code TEXT,
        created_at INTEGER NOT NULL
    )
    """,
    "ALTER TABLE admin_audit_logs ADD COLUMN IF NOT EXISTS request_id TEXT",
    "ALTER TABLE admin_audit_logs ADD COLUMN IF NOT EXISTS result_status TEXT NOT NULL DEFAULT 'success'",
    "ALTER TABLE admin_audit_logs ADD COLUMN IF NOT EXISTS error_code TEXT",
    "CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_created ON admin_audit_logs(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_admin ON admin_audit_logs(admin_user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_target ON admin_audit_logs(target_type, target_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS admin_user_controls (
        user_id TEXT PRIMARY KEY,
        suspended BOOLEAN NOT NULL DEFAULT FALSE,
        abuse_flag BOOLEAN NOT NULL DEFAULT FALSE,
        sharing_disabled BOOLEAN NOT NULL DEFAULT FALSE,
        downloads_disabled BOOLEAN NOT NULL DEFAULT FALSE,
        reason TEXT,
        updated_by TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_admin_user_controls_flags ON admin_user_controls(suspended, abuse_flag)",
    """
    CREATE TABLE IF NOT EXISTS admin_node_controls (
        node_id TEXT PRIMARY KEY,
        placement_paused BOOLEAN NOT NULL DEFAULT FALSE,
        payouts_paused BOOLEAN NOT NULL DEFAULT FALSE,
        reason TEXT,
        updated_by TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_admin_node_controls_flags ON admin_node_controls(placement_paused, payouts_paused)",
    "ALTER TABLE nodes ADD COLUMN IF NOT EXISTS placement_paused BOOLEAN NOT NULL DEFAULT FALSE",
    "CREATE INDEX IF NOT EXISTS idx_nodes_placement_paused ON nodes(placement_paused, last_seen DESC)",
    """
    CREATE TABLE IF NOT EXISTS admin_release_registry (
        version TEXT PRIMARY KEY,
        channel TEXT NOT NULL DEFAULT 'stable',
        status TEXT NOT NULL DEFAULT 'draft',
        minimum_supported BOOLEAN NOT NULL DEFAULT FALSE,
        force_update BOOLEAN NOT NULL DEFAULT FALSE,
        rollout_percent INTEGER NOT NULL DEFAULT 0,
        release_notes TEXT,
        created_by TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        CHECK (rollout_percent >= 0 AND rollout_percent <= 100)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_admin_release_registry_status ON admin_release_registry(channel, status, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS admin_billing_retry_requests (
        request_id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'requested',
        requested_by TEXT NOT NULL,
        reason TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        UNIQUE(event_id, status)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_admin_billing_retry_status ON admin_billing_retry_requests(status, created_at)",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS created_at INTEGER",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS provider TEXT",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS provider_ref TEXT",
    "CREATE INDEX IF NOT EXISTS idx_subscriptions_provider_ref ON subscriptions(provider, provider_ref)",
]


REQUIRED_COLUMNS: Dict[str, List[str]] = {
    "users": ["user_id", "email", "password_hash"],
    "user_roles": ["user_id", "role"],
    "subscriptions": ["user_id", "plan_id", "status", "updated_at", "created_at"],
    "invoices": ["invoice_id", "user_id", "total", "status", "created_at"],
    "nodes": [
        "node_id", "last_seen", "capacity_bytes", "reserved_bytes",
        "failure_domain", "placement_paused",
    ],
    "node_profiles": [
        "node_id", "owner_user_id", "payout_enabled", "payouts_paused",
    ],
    "node_heartbeat_hourly": ["node_id", "hour_start"],
    "node_transfer_metrics": ["node_id", "success", "error_code", "created_at"],
    "objects": ["file_object_id", "owner_user_id", "size_bytes", "chunk_size", "created_at"],
    "items": ["item_id", "name", "file_object_id", "owner_user_id", "created_at"],
    "replicas": ["file_object_id", "node_id", "created_at"],
    "replica_health": ["file_object_id", "node_id", "status", "updated_at"],
    "chunk_audit_slices": ["file_object_id", "chunk_id", "byte_offset", "length", "hash_hex"],
    "audit_jobs": [
        "audit_job_id", "file_object_id", "node_id", "status", "purpose",
        "failure_kind", "created_at", "updated_at",
    ],
    "repair_jobs": [
        "repair_job_id", "file_object_id", "status", "attempt_count", "updated_at",
    ],
    "repair_job_events": ["id", "repair_job_id", "status", "event", "created_at"],
    "transfer_events": ["user_id", "bytes", "ts"],
    "stripe_customers": ["user_id", "stripe_customer_id"],
    "stripe_webhook_events": ["event_id", "event_type", "processed_at", "status"],
    "stripe_plan_prices": ["plan_id", "stripe_price_id", "active"],
    "node_earnings": [
        "earning_id", "node_id", "net_amount_yen", "status", "created_at", "updated_at",
    ],
    "node_payouts": ["payout_id", "node_id", "amount_yen", "status", "created_at"],
    "admin_sessions": ["session_id", "admin_user_id", "expires_at", "revoked_at"],
    "admin_audit_logs": ["log_id", "admin_user_id", "action", "created_at"],
    "admin_user_controls": ["user_id", "suspended", "sharing_disabled", "downloads_disabled"],
    "admin_node_controls": ["node_id", "placement_paused", "payouts_paused"],
    "admin_release_registry": ["version", "status", "rollout_percent"],
    "admin_billing_retry_requests": ["request_id", "event_id", "status", "requested_by"],
}


def _execute_all(cur, statements: Iterable[str]) -> None:
    for statement in statements:
        cur.execute(statement)


def init_phase2_admin_schema(cur=None) -> None:
    """Apply the additive Phase 2 schema and record its migration id."""
    if cur is not None:
        _execute_all(cur, PHASE2_ADMIN_DDL)
        cur.execute(
            """
            INSERT INTO tricloud_schema_migrations(migration_id, applied_at)
            VALUES (%s, %s)
            ON CONFLICT (migration_id) DO NOTHING
            """,
            (MIGRATION_ID, int(now_ts())),
        )
        return

    with db_conn() as conn:
        with conn.cursor() as local_cur:
            init_phase2_admin_schema(local_cur)
        conn.commit()


def inspect_phase2_schema(cur) -> Dict[str, object]:
    """Return a read-only inventory of missing Phase 1/2 tables and columns."""
    missing_tables: List[str] = []
    missing_columns: Dict[str, List[str]] = {}

    for table_name, expected_columns in REQUIRED_COLUMNS.items():
        cur.execute("SELECT to_regclass(%s) AS relation", (f"public.{table_name}",))
        row = cur.fetchone()
        relation = None
        if isinstance(row, dict):
            relation = row.get("relation")
        elif row:
            relation = row[0]
        if not relation:
            missing_tables.append(table_name)
            continue

        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%s
            """,
            (table_name,),
        )
        actual = {
            str(item.get("column_name") if isinstance(item, dict) else item[0])
            for item in cur.fetchall()
        }
        absent = [column for column in expected_columns if column not in actual]
        if absent:
            missing_columns[table_name] = absent

    return {
        "ok": not missing_tables and not missing_columns,
        "migration_id": MIGRATION_ID,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
    }
