# -*- coding: utf-8 -*-
"""Replica health schema, observations, and shortage detection.

Phase 1 deliberately separates three counts:

* logical replicas recorded in ``replicas``;
* replicas still eligible within the long-offline grace period;
* replicas recently verified as healthy.

Rollout can initially use the eligible count and later require recent audits.
This avoids queueing every existing beta object before the audit inventory has
been populated, while allowing the completed Phase 1 policy to be enabled.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional, Sequence

from meta_db_pg import db_conn, now_ts


TARGET_REPLICA_COUNT = int(os.environ.get("TARGET_REPLICA_COUNT", "3"))
REPLICA_ONLINE_WINDOW_SEC = int(os.environ.get("REPLICA_ONLINE_WINDOW_SEC", "20"))
REPLICA_LONG_OFFLINE_SEC = int(os.environ.get("REPLICA_LONG_OFFLINE_SEC", str(7 * 24 * 3600)))
REPLICA_AUDIT_VALID_SEC = int(os.environ.get("REPLICA_AUDIT_VALID_SEC", str(72 * 3600)))

REPLICA_STATUSES = {
    "pending",
    "healthy",
    "suspect",
    "missing",
    "corrupt",
    "repairing",
    "deleting",
    "deleted",
}
TERMINAL_OR_UNUSABLE_STATUSES = {"missing", "corrupt", "repairing", "deleting", "deleted"}
ACTIVE_REPAIR_STATUSES = {
    "queued",
    "selecting_source",
    "selecting_target",
    "copying",
    "verifying",
    "retry_wait",
}


STORAGE_MAINTENANCE_DDL = [
    """
    CREATE TABLE IF NOT EXISTS replica_health (
        file_object_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        last_verified_at INTEGER,
        last_success_at INTEGER,
        last_failure_at INTEGER,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (file_object_id, node_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_replica_health_status ON replica_health(status, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_replica_health_node ON replica_health(node_id, status)",
    """
    CREATE TABLE IF NOT EXISTS repair_jobs (
        repair_job_id TEXT PRIMARY KEY,
        file_object_id TEXT NOT NULL,
        source_node_id TEXT,
        target_node_id TEXT,
        reason TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        attempt_count INTEGER NOT NULL DEFAULT 0,
        next_retry_at INTEGER,
        last_error TEXT,
        worker_id TEXT,
        idempotency_key TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL,
        started_at INTEGER,
        finished_at INTEGER
    )
    """,
    "ALTER TABLE repair_jobs ADD COLUMN IF NOT EXISTS idempotency_key TEXT",
    "UPDATE repair_jobs SET idempotency_key=repair_job_id WHERE idempotency_key IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_repair_jobs_idempotency ON repair_jobs(idempotency_key)",
    "CREATE INDEX IF NOT EXISTS idx_repair_jobs_status ON repair_jobs(status, next_retry_at, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_repair_jobs_object ON repair_jobs(file_object_id, created_at)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_repair_jobs_active_object
    ON repair_jobs(file_object_id)
    WHERE status IN ('queued','selecting_source','selecting_target','copying','verifying','retry_wait')
    """,
    """
    CREATE TABLE IF NOT EXISTS node_transfer_metrics (
        id BIGSERIAL PRIMARY KEY,
        node_id TEXT NOT NULL,
        file_object_id TEXT,
        transfer_id TEXT,
        operation TEXT NOT NULL,
        success BOOLEAN NOT NULL,
        bytes BIGINT NOT NULL DEFAULT 0,
        latency_ms INTEGER,
        error_code TEXT,
        created_at INTEGER NOT NULL
    )
    """,
    "ALTER TABLE node_transfer_metrics ADD COLUMN IF NOT EXISTS file_object_id TEXT",
    "ALTER TABLE node_transfer_metrics ADD COLUMN IF NOT EXISTS transfer_id TEXT",
    "CREATE INDEX IF NOT EXISTS idx_node_transfer_metrics_node_ts ON node_transfer_metrics(node_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_node_transfer_metrics_object_ts ON node_transfer_metrics(file_object_id, created_at)",
]


def init_storage_maintenance_schema(cur=None, *, backfill: bool = True) -> None:
    """Create additive Phase 1 tables and optionally backfill pending rows."""
    if cur is not None:
        _apply_schema(cur, backfill=backfill)
        return

    with db_conn() as conn:
        with conn.cursor() as local_cur:
            _apply_schema(local_cur, backfill=backfill)
        conn.commit()


def _apply_schema(cur, *, backfill: bool) -> None:
    for stmt in STORAGE_MAINTENANCE_DDL:
        cur.execute(stmt)
    if backfill:
        backfill_replica_health(cur)


def backfill_replica_health(cur, *, ts: Optional[int] = None) -> int:
    """Add ``pending`` health rows for existing logical replicas.

    Existing beta replicas are intentionally not marked healthy without a
    successful commit, download, or future audit observation.
    """
    timestamp = int(now_ts() if ts is None else ts)
    cur.execute(
        """
        INSERT INTO replica_health(
            file_object_id,node_id,status,last_verified_at,last_success_at,
            last_failure_at,consecutive_failures,last_error,created_at,updated_at
        )
        SELECT r.file_object_id,r.node_id,'pending',NULL,NULL,NULL,0,NULL,%s,%s
        FROM replicas r
        ON CONFLICT (file_object_id,node_id) DO NOTHING
        """,
        (timestamp, timestamp),
    )
    return max(0, int(cur.rowcount or 0))


def mark_replica_healthy(
    cur,
    *,
    file_object_id: str,
    node_id: str,
    verified: bool = True,
    ts: Optional[int] = None,
) -> None:
    timestamp = int(now_ts() if ts is None else ts)
    verified_at = timestamp if verified else None
    cur.execute(
        """
        INSERT INTO replica_health(
            file_object_id,node_id,status,last_verified_at,last_success_at,
            last_failure_at,consecutive_failures,last_error,created_at,updated_at
        ) VALUES (%s,%s,'healthy',%s,%s,NULL,0,NULL,%s,%s)
        ON CONFLICT (file_object_id,node_id) DO UPDATE SET
          status='healthy',
          last_verified_at=COALESCE(EXCLUDED.last_verified_at,replica_health.last_verified_at),
          last_success_at=EXCLUDED.last_success_at,
          consecutive_failures=0,
          last_error=NULL,
          updated_at=EXCLUDED.updated_at
        """,
        (file_object_id, node_id, verified_at, timestamp, timestamp, timestamp),
    )


def mark_replicas_healthy(
    cur,
    *,
    file_object_id: str,
    node_ids: Iterable[str],
    verified: bool = True,
    ts: Optional[int] = None,
) -> None:
    timestamp = int(now_ts() if ts is None else ts)
    for node_id in node_ids:
        mark_replica_healthy(
            cur,
            file_object_id=str(file_object_id),
            node_id=str(node_id),
            verified=verified,
            ts=timestamp,
        )


def mark_replica_failure(
    cur,
    *,
    file_object_id: str,
    node_id: str,
    status: str = "suspect",
    error: str,
    verified_failure: bool = False,
    ts: Optional[int] = None,
) -> None:
    normalized_status = str(status)
    if normalized_status not in REPLICA_STATUSES:
        raise ValueError(f"unsupported replica status: {normalized_status}")
    if normalized_status == "healthy":
        raise ValueError("mark_replica_failure cannot set healthy")

    timestamp = int(now_ts() if ts is None else ts)
    verified_at = timestamp if verified_failure else None
    cur.execute(
        """
        INSERT INTO replica_health(
            file_object_id,node_id,status,last_verified_at,last_success_at,
            last_failure_at,consecutive_failures,last_error,created_at,updated_at
        ) VALUES (%s,%s,%s,%s,NULL,%s,1,%s,%s,%s)
        ON CONFLICT (file_object_id,node_id) DO UPDATE SET
          status=EXCLUDED.status,
          last_verified_at=COALESCE(EXCLUDED.last_verified_at,replica_health.last_verified_at),
          last_failure_at=EXCLUDED.last_failure_at,
          consecutive_failures=replica_health.consecutive_failures+1,
          last_error=EXCLUDED.last_error,
          updated_at=EXCLUDED.updated_at
        """,
        (
            file_object_id,
            node_id,
            normalized_status,
            verified_at,
            timestamp,
            str(error)[:1000],
            timestamp,
            timestamp,
        ),
    )


def record_node_transfer_metric(
    cur,
    *,
    node_id: str,
    operation: str,
    success: bool,
    bytes_count: int = 0,
    latency_ms: Optional[int] = None,
    error_code: Optional[str] = None,
    file_object_id: Optional[str] = None,
    transfer_id: Optional[str] = None,
    ts: Optional[int] = None,
) -> None:
    timestamp = int(now_ts() if ts is None else ts)
    cur.execute(
        """
        INSERT INTO node_transfer_metrics(
            node_id,file_object_id,transfer_id,operation,success,bytes,latency_ms,error_code,created_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            node_id,
            file_object_id,
            transfer_id,
            operation,
            bool(success),
            max(0, int(bytes_count)),
            None if latency_ms is None else max(0, int(latency_ms)),
            None if error_code is None else str(error_code)[:200],
            timestamp,
        ),
    )


def fetch_download_candidates(
    cur,
    *,
    file_object_id: str,
    online_after: int,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """Return usable replicas ordered without a composite reliability score."""
    cur.execute(
        """
        SELECT r.node_id,
               r.created_at,
               n.last_seen,
               COALESCE(h.status,'pending') AS health_status,
               h.last_success_at,
               h.last_failure_at,
               COALESCE(h.consecutive_failures,0) AS consecutive_failures
        FROM replicas r
        JOIN nodes n ON n.node_id=r.node_id
        LEFT JOIN replica_health h
          ON h.file_object_id=r.file_object_id AND h.node_id=r.node_id
        WHERE r.file_object_id=%s
          AND n.last_seen >= %s
          AND COALESCE(h.status,'pending') NOT IN ('missing','corrupt','repairing','deleting','deleted')
        ORDER BY
          CASE COALESCE(h.status,'pending')
            WHEN 'healthy' THEN 0
            WHEN 'pending' THEN 1
            WHEN 'suspect' THEN 2
            ELSE 3
          END,
          COALESCE(h.last_failure_at,0) ASC,
          r.created_at ASC,
          r.node_id ASC
        LIMIT %s
        """,
        (str(file_object_id), int(online_after), max(1, int(limit))),
    )
    return [dict(row) for row in cur.fetchall()]


def _table_exists(cur, table_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
    row = cur.fetchone()
    if not row:
        return False
    if isinstance(row, dict):
        return next(iter(row.values())) is not None
    return row[0] is not None


def _tracked_object_union(cur) -> str:
    """Build a UNION from existing reference tables using fixed SQL fragments."""
    fragments: List[str] = []
    if _table_exists(cur, "items"):
        fragments.append("SELECT file_object_id FROM items WHERE file_object_id IS NOT NULL")
    if _table_exists(cur, "item_parts"):
        fragments.append("SELECT file_object_id FROM item_parts")
    if _table_exists(cur, "item_versions"):
        fragments.append("SELECT file_object_id FROM item_versions WHERE file_object_id IS NOT NULL")
    if _table_exists(cur, "item_version_parts"):
        fragments.append("SELECT file_object_id FROM item_version_parts")

    # ``objects`` is only used as a fallback for installations whose item
    # schema has not yet been initialized.  In a normal installation, active
    # uploads are excluded until an item or version references them.
    if not fragments:
        fragments.append("SELECT file_object_id FROM objects")
    return " UNION ".join(fragments)


def detect_under_replicated_objects(
    cur,
    *,
    target_replicas: int = TARGET_REPLICA_COUNT,
    online_window_sec: int = REPLICA_ONLINE_WINDOW_SEC,
    long_offline_sec: int = REPLICA_LONG_OFFLINE_SEC,
    audit_valid_sec: int = REPLICA_AUDIT_VALID_SEC,
    require_recent_audit: bool = False,
    limit: int = 1000,
    ts: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Read replica counts and return objects below the configured target.

    With ``require_recent_audit=False`` (the safe rollout default), ``pending`` and
    ``suspect`` replicas remain eligible while they are inside the long-offline
    grace period.  ``verified_healthy_count`` is still returned for visibility.
    """
    timestamp = int(now_ts() if ts is None else ts)
    target = max(1, int(target_replicas))
    online_after = timestamp - max(1, int(online_window_sec))
    long_offline_after = timestamp - max(1, int(long_offline_sec))
    audit_after = timestamp - max(1, int(audit_valid_sec))
    tracked_union = _tracked_object_union(cur)

    query = f"""
        WITH tracked_objects AS (
            SELECT DISTINCT file_object_id FROM ({tracked_union}) tracked
        )
        SELECT o.file_object_id,
               o.owner_user_id,
               o.size_bytes,
               COUNT(DISTINCT r.node_id) AS logical_replica_count,
               COUNT(DISTINCT r.node_id) FILTER (
                 WHERE COALESCE(h.status,'pending') NOT IN ('missing','corrupt','repairing','deleting','deleted')
                   AND COALESCE(n.last_seen,0) >= %s
               ) AS online_replica_count,
               COUNT(DISTINCT r.node_id) FILTER (
                 WHERE COALESCE(h.status,'pending') NOT IN ('missing','corrupt','repairing','deleting','deleted')
                   AND COALESCE(n.last_seen,0) >= %s
               ) AS eligible_replica_count,
               COUNT(DISTINCT r.node_id) FILTER (
                 WHERE h.status='healthy'
                   AND COALESCE(h.last_verified_at,0) >= %s
                   AND COALESCE(n.last_seen,0) >= %s
               ) AS verified_healthy_count,
               COUNT(DISTINCT r.node_id) FILTER (
                 WHERE h.status IN ('missing','corrupt')
               ) AS known_bad_replica_count
        FROM tracked_objects t
        JOIN objects o ON o.file_object_id=t.file_object_id
        LEFT JOIN replicas r ON r.file_object_id=o.file_object_id
        LEFT JOIN nodes n ON n.node_id=r.node_id
        LEFT JOIN replica_health h
          ON h.file_object_id=r.file_object_id AND h.node_id=r.node_id
        GROUP BY o.file_object_id,o.owner_user_id,o.size_bytes
        ORDER BY o.created_at ASC,o.file_object_id ASC
        LIMIT %s
    """
    cur.execute(query, (online_after, long_offline_after, audit_after, long_offline_after, max(1, int(limit))))

    shortages: List[Dict[str, Any]] = []
    for raw in cur.fetchall():
        row = dict(raw)
        logical = int(row.get("logical_replica_count") or 0)
        online = int(row.get("online_replica_count") or 0)
        eligible = int(row.get("eligible_replica_count") or 0)
        verified = int(row.get("verified_healthy_count") or 0)
        known_bad = int(row.get("known_bad_replica_count") or 0)
        effective = verified if require_recent_audit else eligible
        if effective >= target:
            continue

        if logical < target:
            reason = "logical_replica_shortage"
        elif known_bad > 0:
            reason = "missing_or_corrupt_replica"
        elif eligible < target:
            reason = "long_offline_or_unusable_replica"
        else:
            reason = "recent_audit_shortage"

        shortages.append(
            {
                "file_object_id": str(row["file_object_id"]),
                "owner_user_id": str(row["owner_user_id"]),
                "size_bytes": int(row.get("size_bytes") or 0),
                "target_replica_count": target,
                "logical_replica_count": logical,
                "online_replica_count": online,
                "eligible_replica_count": eligible,
                "verified_healthy_count": verified,
                "known_bad_replica_count": known_bad,
                "effective_replica_count": effective,
                "deficit": max(0, target - effective),
                "reason": reason,
                "audit_required_for_decision": bool(require_recent_audit),
            }
        )
    return shortages


def enqueue_queued_repair_jobs(
    cur,
    shortages: Sequence[Dict[str, Any]],
    *,
    ts: Optional[int] = None,
) -> List[str]:
    """Backward-compatible wrapper for the full repair job service."""
    from replica_repair_service import enqueue_repair_job

    timestamp = int(now_ts() if ts is None else ts)
    created_ids: List[str] = []
    for shortage in shortages:
        file_object_id = str(shortage.get("file_object_id") or "")
        if not file_object_id or int(shortage.get("deficit") or 0) <= 0:
            continue
        reason = str(shortage.get("reason") or "under_replicated")
        repair_job_id = enqueue_repair_job(
            cur,
            file_object_id=file_object_id,
            reason=reason,
            ts=timestamp,
        )
        if repair_job_id:
            created_ids.append(str(repair_job_id))
    return created_ids
