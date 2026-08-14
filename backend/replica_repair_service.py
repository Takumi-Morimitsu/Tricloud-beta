# -*- coding: utf-8 -*-
"""Database state transitions for encrypted replica repair jobs.

ZeroMQ transfer is performed by ``server.py``.  This module owns atomic job
claiming, capacity reservation/release, retry scheduling, completion, cleanup,
and operator cancellation/requeue operations.  Node reliability scores are
deliberately not referenced by any candidate query.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List, Optional, Sequence

from meta_db_pg import db_conn, now_ts
from object_gc import init_object_gc_schema
from replica_health_service import mark_replica_failure, mark_replica_healthy
from repair_object_lock import (
    lock_repair_object,
    repair_block_reason,
    repair_replica_counts,
)
from repair_transfer import retry_delay_seconds


REPAIR_MAX_ATTEMPTS = int(os.environ.get("REPAIR_MAX_ATTEMPTS", "4"))
REPAIR_RETRY_DELAYS_SEC = [
    int(value.strip())
    for value in os.environ.get("REPAIR_RETRY_DELAYS_SEC", "30,300,1800").split(",")
    if value.strip()
]
REPAIR_LEASE_SEC = int(os.environ.get("REPAIR_LEASE_SEC", "60"))
REPAIR_SOURCE_AUDIT_VALID_SEC = int(os.environ.get("REPAIR_SOURCE_AUDIT_VALID_SEC", str(72 * 3600)))
TARGET_REPLICA_COUNT = int(os.environ.get("TARGET_REPLICA_COUNT", "3"))

ACTIVE_REPAIR_STATUSES = {
    "queued",
    "selecting_source",
    "selecting_target",
    "copying",
    "verifying",
    "retry_wait",
}
RUNNING_REPAIR_STATUSES = {"selecting_source", "selecting_target", "copying", "verifying"}
TERMINAL_REPAIR_STATUSES = {"completed", "failed", "canceled"}


REPLICA_REPAIR_DDL = [
    "ALTER TABLE repair_jobs ADD COLUMN IF NOT EXISTS updated_at INTEGER",
    "ALTER TABLE repair_jobs ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 4",
    "ALTER TABLE repair_jobs ADD COLUMN IF NOT EXISTS lease_expires_at INTEGER",
    "ALTER TABLE repair_jobs ADD COLUMN IF NOT EXISTS transfer_id TEXT",
    "ALTER TABLE repair_jobs ADD COLUMN IF NOT EXISTS reserved_bytes BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE repair_jobs ADD COLUMN IF NOT EXISTS copied_bytes BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE repair_jobs ADD COLUMN IF NOT EXISTS total_chunks INTEGER",
    "ALTER TABLE repair_jobs ADD COLUMN IF NOT EXISTS verified_at INTEGER",
    "ALTER TABLE repair_jobs ADD COLUMN IF NOT EXISTS canceled_at INTEGER",
    "ALTER TABLE repair_jobs ADD COLUMN IF NOT EXISTS failure_code TEXT",
    "ALTER TABLE nodes ADD COLUMN IF NOT EXISTS failure_domain TEXT",
    "CREATE INDEX IF NOT EXISTS idx_repair_jobs_lease ON repair_jobs(status,lease_expires_at)",
    """
    CREATE TABLE IF NOT EXISTS repair_job_events (
        id BIGSERIAL PRIMARY KEY,
        repair_job_id TEXT NOT NULL,
        status TEXT NOT NULL,
        event TEXT NOT NULL,
        source_node_id TEXT,
        target_node_id TEXT,
        error_code TEXT,
        detail TEXT,
        created_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_repair_job_events_job ON repair_job_events(repair_job_id,created_at,id)",
    """
    CREATE TABLE IF NOT EXISTS repair_cleanup_queue (
        cleanup_id TEXT PRIMARY KEY,
        repair_job_id TEXT NOT NULL,
        file_object_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        transfer_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_retry_at INTEGER,
        last_error TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        UNIQUE(node_id,transfer_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_repair_cleanup_due ON repair_cleanup_queue(status,next_retry_at,created_at)",
]


def init_replica_repair_schema(cur=None) -> None:
    if cur is not None:
        _apply_schema(cur)
        return
    with db_conn() as conn:
        with conn.cursor() as local_cur:
            _apply_schema(local_cur)
        conn.commit()


def _apply_schema(cur) -> None:
    init_object_gc_schema(cur)
    for statement in REPLICA_REPAIR_DDL:
        cur.execute(statement)
    cur.execute("UPDATE repair_jobs SET updated_at=COALESCE(updated_at,created_at) WHERE updated_at IS NULL")
    cur.execute("UPDATE repair_jobs SET max_attempts=%s WHERE max_attempts IS NULL", (max(1, REPAIR_MAX_ATTEMPTS),))


def _row_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    raise TypeError("repair service requires a dict-row cursor")


def _table_exists(cur, table_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) AS to_regclass", (f"public.{table_name}",))
    row = cur.fetchone()
    return bool(row and _row_dict(row).get("to_regclass"))


def _event(
    cur,
    *,
    repair_job_id: str,
    status: str,
    event: str,
    source_node_id: Optional[str] = None,
    target_node_id: Optional[str] = None,
    error_code: Optional[str] = None,
    detail: Optional[str] = None,
    ts: Optional[int] = None,
) -> None:
    timestamp = int(now_ts() if ts is None else ts)
    cur.execute(
        """
        INSERT INTO repair_job_events(
            repair_job_id,status,event,source_node_id,target_node_id,error_code,detail,created_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            str(repair_job_id),
            str(status),
            str(event),
            source_node_id,
            target_node_id,
            error_code,
            None if detail is None else str(detail)[:2000],
            timestamp,
        ),
    )


def enqueue_repair_job(
    cur,
    *,
    file_object_id: str,
    reason: str,
    ts: Optional[int] = None,
) -> Optional[str]:
    """Create at most one active repair for an object that is still short."""
    timestamp = int(now_ts() if ts is None else ts)
    object_id = str(file_object_id)
    if not lock_repair_object(cur, file_object_id=object_id):
        return None
    counts = repair_replica_counts(cur, file_object_id=object_id)
    if repair_block_reason(counts, target_replicas=TARGET_REPLICA_COUNT):
        return None
    cur.execute(
        """
        SELECT repair_job_id FROM repair_jobs
        WHERE file_object_id=%s
          AND status IN ('queued','selecting_source','selecting_target','copying','verifying','retry_wait')
        LIMIT 1
        """,
        (object_id,),
    )
    if cur.fetchone():
        return None
    repair_job_id = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO repair_jobs(
            repair_job_id,file_object_id,source_node_id,target_node_id,reason,status,
            attempt_count,next_retry_at,last_error,worker_id,idempotency_key,
            created_at,started_at,finished_at,updated_at,max_attempts,lease_expires_at,
            transfer_id,reserved_bytes,copied_bytes,total_chunks,verified_at,canceled_at,failure_code
        ) VALUES (%s,%s,NULL,NULL,%s,'queued',0,NULL,NULL,NULL,%s,%s,NULL,NULL,%s,%s,
                  NULL,NULL,0,0,NULL,NULL,NULL,NULL)
        ON CONFLICT DO NOTHING
        RETURNING repair_job_id
        """,
        (
            repair_job_id,
            object_id,
            str(reason or "under_replicated")[:200],
            str(uuid.uuid4()),
            timestamp,
            timestamp,
            max(1, REPAIR_MAX_ATTEMPTS),
        ),
    )
    inserted = cur.fetchone()
    if not inserted:
        return None
    job_id = str(_row_dict(inserted)["repair_job_id"])
    _event(cur, repair_job_id=job_id, status="queued", event="queued", ts=timestamp)
    return job_id


def claim_due_repair_jobs(
    cur,
    *,
    worker_id: str,
    limit: int,
    lease_sec: int = REPAIR_LEASE_SEC,
    ts: Optional[int] = None,
) -> List[Dict[str, Any]]:
    timestamp = int(now_ts() if ts is None else ts)
    cur.execute(
        """
        SELECT * FROM repair_jobs
        WHERE status IN ('queued','retry_wait')
          AND COALESCE(next_retry_at,0) <= %s
          AND attempt_count < COALESCE(max_attempts,%s)
        ORDER BY created_at
        LIMIT %s
        FOR UPDATE SKIP LOCKED
        """,
        (timestamp, max(1, REPAIR_MAX_ATTEMPTS), max(1, int(limit))),
    )
    rows = [_row_dict(row) for row in cur.fetchall()]
    claimed: List[Dict[str, Any]] = []
    for row in rows:
        cur.execute(
            """
            UPDATE repair_jobs
            SET status='selecting_source',attempt_count=attempt_count+1,
                next_retry_at=NULL,last_error=NULL,failure_code=NULL,
                worker_id=%s,lease_expires_at=%s,
                started_at=COALESCE(started_at,%s),updated_at=%s
            WHERE repair_job_id=%s AND status IN ('queued','retry_wait')
            RETURNING *
            """,
            (
                str(worker_id),
                timestamp + max(5, int(lease_sec)),
                timestamp,
                timestamp,
                str(row["repair_job_id"]),
            ),
        )
        updated = _row_dict(cur.fetchone())
        if updated:
            counts = repair_replica_counts(
                cur,
                file_object_id=str(updated["file_object_id"]),
            )
            block_reason = repair_block_reason(counts, target_replicas=TARGET_REPLICA_COUNT)
            if block_reason:
                cur.execute(
                    """
                    UPDATE repair_jobs
                    SET status='canceled',last_error=%s,
                        failure_code=%s,worker_id=NULL,
                        lease_expires_at=NULL,canceled_at=%s,finished_at=%s,updated_at=%s
                    WHERE repair_job_id=%s AND status='selecting_source'
                    """,
                    (
                        "target replica count already satisfied"
                        if block_reason == "target_already_satisfied"
                        else "no known-bad replica can be retired safely",
                        block_reason,
                        timestamp,
                        timestamp,
                        timestamp,
                        str(updated["repair_job_id"]),
                    ),
                )
                _event(
                    cur,
                    repair_job_id=str(updated["repair_job_id"]),
                    status="canceled",
                    event=block_reason,
                    detail="claim skipped before transfer",
                    ts=timestamp,
                )
                continue
            _event(
                cur,
                repair_job_id=str(updated["repair_job_id"]),
                status="selecting_source",
                event="claimed",
                ts=timestamp,
            )
            claimed.append(updated)
    return claimed


def select_source_candidates(
    cur,
    *,
    file_object_id: str,
    online_after: int,
    audit_after: int,
    limit: int = 3,
) -> List[str]:
    """Return recently verified online sources; no reliability score is used."""
    cur.execute(
        """
        SELECT r.node_id
        FROM replicas r
        JOIN nodes n ON n.node_id=r.node_id
        JOIN replica_health h
          ON h.file_object_id=r.file_object_id AND h.node_id=r.node_id
        WHERE r.file_object_id=%s
          AND h.status='healthy'
          AND COALESCE(h.last_verified_at,0) >= %s
          AND n.last_seen >= %s
          AND COALESCE(n.placement_paused,FALSE)=FALSE
        ORDER BY h.last_verified_at DESC,n.last_seen DESC,r.node_id
        LIMIT %s
        """,
        (str(file_object_id), int(audit_after), int(online_after), max(1, int(limit))),
    )
    return [str(_row_dict(row)["node_id"]) for row in cur.fetchall()]


def select_and_reserve_target(
    cur,
    *,
    repair_job_id: str,
    file_object_id: str,
    file_size: int,
    source_node_ids: Sequence[str],
    online_after: int,
    lease_sec: int = REPAIR_LEASE_SEC,
    ts: Optional[int] = None,
) -> Optional[str]:
    """Atomically reserve a non-replica target with enough free capacity."""
    timestamp = int(now_ts() if ts is None else ts)
    excluded_sources = [str(value) for value in source_node_ids if value]
    cur.execute(
        """
        SELECT n.node_id
        FROM nodes n
        LEFT JOIN node_profiles np ON np.node_id=n.node_id
        LEFT JOIN users target_owner ON target_owner.user_id=np.owner_user_id
        JOIN objects o ON o.file_object_id=%s
        LEFT JOIN users object_owner ON object_owner.user_id=o.owner_user_id
        WHERE n.last_seen >= %s
          AND COALESCE(n.placement_paused,FALSE)=FALSE
          AND n.capacity_bytes-n.reserved_bytes >= %s
          AND NOT (n.node_id = ANY(%s::text[]))
          AND NOT EXISTS (
              SELECT 1 FROM replicas r
              WHERE r.file_object_id=%s AND r.node_id=n.node_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM object_gc_queue g
              WHERE g.file_object_id=%s AND g.node_id=n.node_id AND g.status <> 'done'
          )
          AND NOT EXISTS (
              SELECT 1 FROM repair_cleanup_queue c
              WHERE c.file_object_id=%s AND c.node_id=n.node_id AND c.status <> 'done'
          )
          AND (object_owner.country_code IS NULL OR target_owner.country_code=object_owner.country_code)
          AND (
              n.failure_domain IS NULL OR NOT EXISTS (
                  SELECT 1
                  FROM replicas existing
                  JOIN nodes existing_node ON existing_node.node_id=existing.node_id
                  WHERE existing.file_object_id=%s
                    AND existing_node.failure_domain IS NOT NULL
                    AND existing_node.failure_domain=n.failure_domain
              )
          )
        ORDER BY (n.failure_domain IS NOT NULL) DESC,
                 (n.capacity_bytes-n.reserved_bytes) DESC,
                 n.last_seen DESC,n.node_id
        LIMIT 20
        FOR UPDATE OF n SKIP LOCKED
        """,
        (
            str(file_object_id),
            int(online_after),
            max(0, int(file_size)),
            excluded_sources,
            str(file_object_id),
            str(file_object_id),
            str(file_object_id),
            str(file_object_id),
        ),
    )
    for raw in cur.fetchall():
        node_id = str(_row_dict(raw)["node_id"])
        cur.execute(
            """
            UPDATE nodes
            SET reserved_bytes=reserved_bytes+%s
            WHERE node_id=%s AND capacity_bytes-reserved_bytes >= %s
            RETURNING node_id
            """,
            (max(0, int(file_size)), node_id, max(0, int(file_size))),
        )
        if not cur.fetchone():
            continue
        cur.execute(
            """
            UPDATE repair_jobs
            SET status='selecting_target',target_node_id=%s,reserved_bytes=%s,
                lease_expires_at=%s,updated_at=%s
            WHERE repair_job_id=%s AND status='selecting_source'
            RETURNING repair_job_id
            """,
            (
                node_id,
                max(0, int(file_size)),
                timestamp + max(5, int(lease_sec)),
                timestamp,
                str(repair_job_id),
            ),
        )
        if cur.fetchone():
            cur.execute(
                """
                INSERT INTO replica_health(
                    file_object_id,node_id,status,last_verified_at,last_success_at,
                    last_failure_at,consecutive_failures,last_error,created_at,updated_at
                ) VALUES (%s,%s,'repairing',NULL,NULL,NULL,0,NULL,%s,%s)
                ON CONFLICT (file_object_id,node_id) DO UPDATE SET
                  status='repairing',last_error=NULL,updated_at=EXCLUDED.updated_at
                """,
                (str(file_object_id), node_id, timestamp, timestamp),
            )
            _event(
                cur,
                repair_job_id=str(repair_job_id),
                status="selecting_target",
                event="target_reserved",
                target_node_id=node_id,
                ts=timestamp,
            )
            return node_id
        # The job was canceled between selection and update.  Undo this row's
        # reservation inside the same transaction.
        cur.execute(
            "UPDATE nodes SET reserved_bytes=GREATEST(0,reserved_bytes-%s) WHERE node_id=%s",
            (max(0, int(file_size)), node_id),
        )
        return None
    return None


def mark_repair_copying(
    cur,
    *,
    repair_job_id: str,
    source_node_id: str,
    target_node_id: str,
    transfer_id: str,
    total_chunks: int,
    lease_sec: int = REPAIR_LEASE_SEC,
    ts: Optional[int] = None,
) -> bool:
    timestamp = int(now_ts() if ts is None else ts)
    cur.execute(
        """
        UPDATE repair_jobs
        SET status='copying',source_node_id=%s,target_node_id=%s,transfer_id=%s,
            total_chunks=%s,copied_bytes=0,lease_expires_at=%s,updated_at=%s
        WHERE repair_job_id=%s AND status IN ('selecting_source','selecting_target','copying')
        RETURNING repair_job_id
        """,
        (
            str(source_node_id),
            str(target_node_id),
            str(transfer_id),
            max(0, int(total_chunks)),
            timestamp + max(5, int(lease_sec)),
            timestamp,
            str(repair_job_id),
        ),
    )
    changed = cur.fetchone() is not None
    if changed:
        _event(
            cur,
            repair_job_id=str(repair_job_id),
            status="copying",
            event="source_started",
            source_node_id=str(source_node_id),
            target_node_id=str(target_node_id),
            ts=timestamp,
        )
    return changed


def mark_repair_target_started(
    cur,
    *,
    repair_job_id: str,
    target_node_id: str,
    transfer_id: str,
    lease_sec: int = REPAIR_LEASE_SEC,
    ts: Optional[int] = None,
) -> bool:
    timestamp = int(now_ts() if ts is None else ts)
    cur.execute(
        """
        UPDATE repair_jobs
        SET transfer_id=%s,lease_expires_at=%s,updated_at=%s
        WHERE repair_job_id=%s AND target_node_id=%s
          AND status IN ('selecting_target','copying')
        RETURNING repair_job_id
        """,
        (
            str(transfer_id),
            timestamp + max(5, int(lease_sec)),
            timestamp,
            str(repair_job_id),
            str(target_node_id),
        ),
    )
    return cur.fetchone() is not None


def update_repair_progress(
    cur,
    *,
    repair_job_id: str,
    copied_bytes: int,
    lease_sec: int = REPAIR_LEASE_SEC,
    ts: Optional[int] = None,
) -> None:
    timestamp = int(now_ts() if ts is None else ts)
    cur.execute(
        """
        UPDATE repair_jobs
        SET copied_bytes=GREATEST(copied_bytes,%s),lease_expires_at=%s,updated_at=%s
        WHERE repair_job_id=%s AND status='copying'
        """,
        (
            max(0, int(copied_bytes)),
            timestamp + max(5, int(lease_sec)),
            timestamp,
            str(repair_job_id),
        ),
    )


def mark_repair_verifying(
    cur,
    *,
    repair_job_id: str,
    copied_bytes: int,
    lease_sec: int = REPAIR_LEASE_SEC,
    ts: Optional[int] = None,
) -> bool:
    timestamp = int(now_ts() if ts is None else ts)
    cur.execute(
        """
        UPDATE repair_jobs
        SET status='verifying',copied_bytes=%s,lease_expires_at=%s,updated_at=%s
        WHERE repair_job_id=%s AND status='copying'
        RETURNING source_node_id,target_node_id
        """,
        (
            max(0, int(copied_bytes)),
            timestamp + max(5, int(lease_sec)),
            timestamp,
            str(repair_job_id),
        ),
    )
    row = _row_dict(cur.fetchone())
    if row:
        _event(
            cur,
            repair_job_id=str(repair_job_id),
            status="verifying",
            event="copy_acknowledged",
            source_node_id=row.get("source_node_id"),
            target_node_id=row.get("target_node_id"),
            ts=timestamp,
        )
    return bool(row)


def note_source_failure(
    cur,
    *,
    repair_job_id: str,
    file_object_id: str,
    node_id: str,
    error_code: str,
    replica_status: str = "suspect",
    verified_failure: bool = False,
    ts: Optional[int] = None,
) -> None:
    timestamp = int(now_ts() if ts is None else ts)
    mark_replica_failure(
        cur,
        file_object_id=str(file_object_id),
        node_id=str(node_id),
        status=str(replica_status),
        error=str(error_code),
        verified_failure=bool(verified_failure),
        ts=timestamp,
    )
    _event(
        cur,
        repair_job_id=str(repair_job_id),
        status="copying",
        event="source_failed",
        source_node_id=str(node_id),
        error_code=str(error_code),
        ts=timestamp,
    )


def _queue_cleanup(
    cur,
    *,
    repair_job_id: str,
    file_object_id: str,
    node_id: str,
    transfer_id: str,
    ts: int,
) -> None:
    if not node_id or not transfer_id:
        return
    cur.execute(
        """
        INSERT INTO repair_cleanup_queue(
            cleanup_id,repair_job_id,file_object_id,node_id,transfer_id,status,
            attempts,next_retry_at,last_error,created_at,updated_at
        ) VALUES (%s,%s,%s,%s,%s,'pending',0,NULL,NULL,%s,%s)
        ON CONFLICT (node_id,transfer_id) DO NOTHING
        """,
        (
            str(uuid.uuid4()),
            str(repair_job_id),
            str(file_object_id),
            str(node_id),
            str(transfer_id),
            ts,
            ts,
        ),
    )


def queue_repair_cleanup(
    cur,
    *,
    repair_job_id: str,
    file_object_id: str,
    node_id: str,
    transfer_id: str,
    ts: Optional[int] = None,
) -> None:
    timestamp = int(now_ts() if ts is None else ts)
    _queue_cleanup(
        cur,
        repair_job_id=str(repair_job_id),
        file_object_id=str(file_object_id),
        node_id=str(node_id),
        transfer_id=str(transfer_id),
        ts=timestamp,
    )


def renew_repair_lease(
    cur,
    *,
    repair_job_id: str,
    worker_id: str,
    lease_sec: int = REPAIR_LEASE_SEC,
    ts: Optional[int] = None,
) -> None:
    timestamp = int(now_ts() if ts is None else ts)
    cur.execute(
        """
        UPDATE repair_jobs
        SET lease_expires_at=%s,updated_at=%s
        WHERE repair_job_id=%s AND worker_id=%s
          AND status IN ('selecting_source','selecting_target','copying','verifying')
        """,
        (
            timestamp + max(5, int(lease_sec)),
            timestamp,
            str(repair_job_id),
            str(worker_id),
        ),
    )


def _release_job_reservation(cur, job: Dict[str, Any]) -> None:
    target_node_id = str(job.get("target_node_id") or "")
    reserved_bytes = max(0, int(job.get("reserved_bytes") or 0))
    if target_node_id and reserved_bytes:
        cur.execute(
            "UPDATE nodes SET reserved_bytes=GREATEST(0,reserved_bytes-%s) WHERE node_id=%s",
            (reserved_bytes, target_node_id),
        )


def schedule_repair_retry(
    cur,
    *,
    repair_job_id: str,
    error_code: str,
    detail: Optional[str] = None,
    retry_delays: Sequence[int] = tuple(REPAIR_RETRY_DELAYS_SEC),
    ts: Optional[int] = None,
) -> Dict[str, Any]:
    """Release capacity and move a running job to retry_wait or failed."""
    timestamp = int(now_ts() if ts is None else ts)
    cur.execute("SELECT * FROM repair_jobs WHERE repair_job_id=%s FOR UPDATE", (str(repair_job_id),))
    job = _row_dict(cur.fetchone())
    if not job:
        return {"applied": False, "reason": "not_found"}
    if str(job.get("status")) in TERMINAL_REPAIR_STATUSES:
        return {"applied": False, "reason": "already_terminal", **job}

    _release_job_reservation(cur, job)
    _queue_cleanup(
        cur,
        repair_job_id=str(repair_job_id),
        file_object_id=str(job["file_object_id"]),
        node_id=str(job.get("target_node_id") or ""),
        transfer_id=str(job.get("transfer_id") or ""),
        ts=timestamp,
    )
    cur.execute(
        """
        UPDATE audit_jobs
        SET status='canceled',last_error='repair attempt ended',updated_at=%s,completed_at=%s
        WHERE repair_job_id=%s AND status IN ('queued','sent','retry_wait')
        """,
        (timestamp, timestamp, str(repair_job_id)),
    )
    if job.get("target_node_id"):
        cur.execute(
            """
            UPDATE replica_health h
            SET status='deleted',last_failure_at=%s,last_error=%s,updated_at=%s
            WHERE h.file_object_id=%s AND h.node_id=%s
              AND NOT EXISTS (
                  SELECT 1 FROM replicas r
                  WHERE r.file_object_id=h.file_object_id AND r.node_id=h.node_id
              )
            """,
            (
                timestamp,
                str(error_code)[:1000],
                timestamp,
                str(job["file_object_id"]),
                str(job["target_node_id"]),
            ),
        )

    attempt_count = int(job.get("attempt_count") or 0)
    max_attempts = max(1, int(job.get("max_attempts") or REPAIR_MAX_ATTEMPTS))
    terminal = attempt_count >= max_attempts
    status = "failed" if terminal else "retry_wait"
    next_retry_at = None
    if not terminal:
        next_retry_at = timestamp + retry_delay_seconds(attempt_count, [int(x) for x in retry_delays])
    cur.execute(
        """
        UPDATE repair_jobs
        SET status=%s,source_node_id=NULL,target_node_id=NULL,next_retry_at=%s,
            last_error=%s,failure_code=%s,worker_id=NULL,lease_expires_at=NULL,
            transfer_id=NULL,reserved_bytes=0,copied_bytes=0,total_chunks=NULL,
            updated_at=%s,finished_at=CASE WHEN %s THEN %s ELSE NULL END
        WHERE repair_job_id=%s
        """,
        (
            status,
            next_retry_at,
            str(detail or error_code)[:1000],
            str(error_code)[:200],
            timestamp,
            terminal,
            timestamp,
            str(repair_job_id),
        ),
    )
    _event(
        cur,
        repair_job_id=str(repair_job_id),
        status=status,
        event="failed" if terminal else "retry_scheduled",
        source_node_id=job.get("source_node_id"),
        target_node_id=job.get("target_node_id"),
        error_code=str(error_code),
        detail=detail,
        ts=timestamp,
    )
    return {
        "applied": True,
        "status": status,
        "terminal": terminal,
        "next_retry_at": next_retry_at,
        "target_node_id": job.get("target_node_id"),
        "transfer_id": job.get("transfer_id"),
    }


def _retire_bad_replicas(
    cur,
    *,
    file_object_id: str,
    keep_node_id: str,
    size_bytes: int,
    target_replicas: int,
    ts: int,
) -> List[str]:
    cur.execute("SELECT COUNT(*) AS replica_count FROM replicas WHERE file_object_id=%s", (str(file_object_id),))
    count = int(_row_dict(cur.fetchone()).get("replica_count") or 0)
    excess = max(0, count - max(1, int(target_replicas)))
    if excess <= 0:
        return []
    cur.execute(
        """
        SELECT r.node_id
        FROM replicas r
        JOIN replica_health h
          ON h.file_object_id=r.file_object_id AND h.node_id=r.node_id
        WHERE r.file_object_id=%s AND r.node_id<>%s
          AND h.status IN ('missing','corrupt','deleted')
        ORDER BY CASE h.status WHEN 'missing' THEN 0 WHEN 'corrupt' THEN 1 ELSE 2 END,
                 h.updated_at ASC,r.node_id
        LIMIT %s
        """,
        (str(file_object_id), str(keep_node_id), excess),
    )
    retired = [str(_row_dict(row)["node_id"]) for row in cur.fetchall()]
    for node_id in retired:
        cur.execute(
            """
            INSERT INTO object_gc_queue(
                gc_id,file_object_id,node_id,status,reason,attempts,created_at,updated_at,last_error
            ) VALUES (%s,%s,%s,'pending','replaced_bad_replica',0,%s,NULL,NULL)
            ON CONFLICT (file_object_id,node_id) DO UPDATE SET
              status='pending',reason='replaced_bad_replica',attempts=0,
              updated_at=NULL,last_error=NULL
            """,
            (str(uuid.uuid4()), str(file_object_id), node_id, ts),
        )
        if _table_exists(cur, "replica_lifetimes"):
            cur.execute(
                """
                UPDATE replica_lifetimes SET end_ts=COALESCE(end_ts,%s)
                WHERE file_object_id=%s AND node_id=%s AND end_ts IS NULL
                """,
                (ts, str(file_object_id), node_id),
            )
        cur.execute("DELETE FROM replicas WHERE file_object_id=%s AND node_id=%s", (str(file_object_id), node_id))
        cur.execute(
            "UPDATE nodes SET reserved_bytes=GREATEST(0,reserved_bytes-%s) WHERE node_id=%s",
            (max(0, int(size_bytes)), node_id),
        )
        cur.execute(
            """
            UPDATE replica_health
            SET status='deleted',last_error='replaced_after_verified_repair',updated_at=%s
            WHERE file_object_id=%s AND node_id=%s
            """,
            (ts, str(file_object_id), node_id),
        )
    return retired


def complete_repair_job(
    cur,
    *,
    repair_job_id: str,
    target_node_id: str,
    target_replicas: int = TARGET_REPLICA_COUNT,
    ts: Optional[int] = None,
) -> Dict[str, Any]:
    """Publish a verified target and only then retire known-bad excess rows."""
    timestamp = int(now_ts() if ts is None else ts)
    # Discover the object without taking the job row lock, then use the common
    # object -> repair_job lock order shared by admission paths.  Re-read the
    # job after both locks are held because its state may have changed.
    cur.execute("SELECT file_object_id FROM repair_jobs WHERE repair_job_id=%s", (str(repair_job_id),))
    job_ref = _row_dict(cur.fetchone())
    if not job_ref:
        return {"applied": False, "reason": "not_found"}
    file_object_id = str(job_ref["file_object_id"])
    if not lock_repair_object(cur, file_object_id=file_object_id):
        return {"applied": False, "reason": "object_missing"}
    cur.execute("SELECT * FROM repair_jobs WHERE repair_job_id=%s FOR UPDATE", (str(repair_job_id),))
    job = _row_dict(cur.fetchone())
    if not job:
        return {"applied": False, "reason": "not_found"}
    if str(job.get("status")) == "completed":
        return {"applied": False, "reason": "already_completed", **job}
    if str(job.get("status")) != "verifying" or str(job.get("target_node_id") or "") != str(target_node_id):
        return {"applied": False, "reason": "invalid_state", **job}

    cur.execute("SELECT size_bytes,created_at FROM objects WHERE file_object_id=%s", (file_object_id,))
    object_row = _row_dict(cur.fetchone())
    if not object_row:
        return {"applied": False, "reason": "object_missing"}
    size_bytes = int(object_row.get("size_bytes") or 0)

    target_count = max(1, int(target_replicas))
    counts = repair_replica_counts(
        cur,
        file_object_id=file_object_id,
        target_node_id=str(target_node_id),
    )
    block_reason = repair_block_reason(
        counts,
        target_replicas=target_count,
        publishing_new_target=int(counts.get("target_published_count") or 0) == 0,
    )
    if block_reason:
        target_is_published = int(counts.get("target_published_count") or 0) > 0
        if not target_is_published:
            _release_job_reservation(cur, job)
            _queue_cleanup(
                cur,
                repair_job_id=str(repair_job_id),
                file_object_id=file_object_id,
                node_id=str(job.get("target_node_id") or ""),
                transfer_id=str(job.get("transfer_id") or ""),
                ts=timestamp,
            )
        cur.execute(
            """
            UPDATE replica_health h
            SET status='deleted',last_failure_at=%s,
                last_error='repair target no longer required',updated_at=%s
            WHERE h.file_object_id=%s AND h.node_id=%s
              AND NOT EXISTS (
                  SELECT 1 FROM replicas r
                  WHERE r.file_object_id=h.file_object_id AND r.node_id=h.node_id
              )
            """,
            (timestamp, timestamp, file_object_id, str(target_node_id)),
        )
        cur.execute(
            """
            UPDATE repair_jobs
            SET status='canceled',last_error=%s,
                failure_code=%s,worker_id=NULL,
                lease_expires_at=NULL,reserved_bytes=0,canceled_at=%s,
                finished_at=%s,updated_at=%s
            WHERE repair_job_id=%s
            """,
            (
                "target replica count already satisfied"
                if block_reason == "target_already_satisfied"
                else "no known-bad replica can be retired safely",
                block_reason,
                timestamp,
                timestamp,
                timestamp,
                str(repair_job_id),
            ),
        )
        _event(
            cur,
            repair_job_id=str(repair_job_id),
            status="canceled",
            event=block_reason,
            source_node_id=job.get("source_node_id"),
            target_node_id=str(target_node_id),
            detail="verified target discarded before publication",
            ts=timestamp,
        )
        return {
            "applied": True,
            "status": "canceled",
            "reason": block_reason,
            "published": False,
            "file_object_id": file_object_id,
            "target_node_id": str(target_node_id),
            "retired_node_ids": [],
        }

    cur.execute(
        """
        INSERT INTO replicas(file_object_id,node_id,created_at)
        VALUES (%s,%s,%s)
        ON CONFLICT (file_object_id,node_id) DO NOTHING
        """,
        (file_object_id, str(target_node_id), timestamp),
    )
    mark_replica_healthy(
        cur,
        file_object_id=file_object_id,
        node_id=str(target_node_id),
        verified=True,
        ts=timestamp,
    )
    if _table_exists(cur, "replica_lifetimes"):
        cur.execute(
            """
            INSERT INTO replica_lifetimes(file_object_id,node_id,size_bytes,start_ts,end_ts)
            VALUES (%s,%s,%s,%s,NULL)
            ON CONFLICT (file_object_id,node_id,start_ts) DO NOTHING
            """,
            (file_object_id, str(target_node_id), size_bytes, timestamp),
        )

    retired = _retire_bad_replicas(
        cur,
        file_object_id=file_object_id,
        keep_node_id=str(target_node_id),
        size_bytes=size_bytes,
        target_replicas=target_count,
        ts=timestamp,
    )
    cur.execute(
        """
        UPDATE repair_jobs
        SET status='completed',verified_at=%s,finished_at=%s,updated_at=%s,
            lease_expires_at=NULL,last_error=NULL,failure_code=NULL
        WHERE repair_job_id=%s
        """,
        (timestamp, timestamp, timestamp, str(repair_job_id)),
    )
    _event(
        cur,
        repair_job_id=str(repair_job_id),
        status="completed",
        event="verified_and_published",
        source_node_id=job.get("source_node_id"),
        target_node_id=str(target_node_id),
        detail=f"retired_bad_replicas={','.join(retired)}" if retired else None,
        ts=timestamp,
    )
    return {
        "applied": True,
        "status": "completed",
        "file_object_id": file_object_id,
        "target_node_id": str(target_node_id),
        "retired_node_ids": retired,
    }


def cancel_repair_job(
    cur,
    *,
    repair_job_id: str,
    reason: str = "operator_canceled",
    ts: Optional[int] = None,
) -> Dict[str, Any]:
    timestamp = int(now_ts() if ts is None else ts)
    cur.execute("SELECT * FROM repair_jobs WHERE repair_job_id=%s FOR UPDATE", (str(repair_job_id),))
    job = _row_dict(cur.fetchone())
    if not job:
        return {"applied": False, "reason": "not_found"}
    if str(job.get("status")) in TERMINAL_REPAIR_STATUSES:
        return {"applied": False, "reason": "already_terminal", **job}
    _release_job_reservation(cur, job)
    _queue_cleanup(
        cur,
        repair_job_id=str(repair_job_id),
        file_object_id=str(job["file_object_id"]),
        node_id=str(job.get("target_node_id") or ""),
        transfer_id=str(job.get("transfer_id") or ""),
        ts=timestamp,
    )
    cur.execute(
        """
        UPDATE audit_jobs SET status='canceled',last_error=%s,updated_at=%s,completed_at=%s
        WHERE repair_job_id=%s AND status IN ('queued','sent','retry_wait')
        """,
        (str(reason)[:1000], timestamp, timestamp, str(repair_job_id)),
    )
    if job.get("target_node_id"):
        cur.execute(
            """
            UPDATE replica_health h
            SET status='deleted',last_failure_at=%s,last_error=%s,updated_at=%s
            WHERE h.file_object_id=%s AND h.node_id=%s
              AND NOT EXISTS (
                  SELECT 1 FROM replicas r
                  WHERE r.file_object_id=h.file_object_id AND r.node_id=h.node_id
              )
            """,
            (
                timestamp,
                str(reason)[:1000],
                timestamp,
                str(job["file_object_id"]),
                str(job["target_node_id"]),
            ),
        )
    cur.execute(
        """
        UPDATE repair_jobs
        SET status='canceled',last_error=%s,failure_code='operator_canceled',
            next_retry_at=NULL,worker_id=NULL,lease_expires_at=NULL,reserved_bytes=0,
            canceled_at=%s,finished_at=%s,updated_at=%s
        WHERE repair_job_id=%s
        """,
        (str(reason)[:1000], timestamp, timestamp, timestamp, str(repair_job_id)),
    )
    _event(
        cur,
        repair_job_id=str(repair_job_id),
        status="canceled",
        event="operator_canceled",
        source_node_id=job.get("source_node_id"),
        target_node_id=job.get("target_node_id"),
        detail=reason,
        ts=timestamp,
    )
    return {"applied": True, "status": "canceled", "repair_job_id": str(repair_job_id)}


def requeue_repair_job(
    cur,
    *,
    repair_job_id: str,
    reason: str = "operator_requeued",
    reset_attempts: bool = False,
    ts: Optional[int] = None,
) -> Dict[str, Any]:
    timestamp = int(now_ts() if ts is None else ts)
    cur.execute("SELECT * FROM repair_jobs WHERE repair_job_id=%s FOR UPDATE", (str(repair_job_id),))
    job = _row_dict(cur.fetchone())
    if not job:
        return {"applied": False, "reason": "not_found"}
    if str(job.get("status")) not in {"failed", "canceled"}:
        return {"applied": False, "reason": "not_terminal_retryable", **job}
    cur.execute(
        """
        SELECT repair_job_id FROM repair_jobs
        WHERE file_object_id=%s AND repair_job_id<>%s
          AND status IN ('queued','selecting_source','selecting_target','copying','verifying','retry_wait')
        LIMIT 1
        """,
        (str(job["file_object_id"]), str(repair_job_id)),
    )
    if cur.fetchone():
        return {"applied": False, "reason": "another_active_job_exists", **job}
    cur.execute(
        """
        UPDATE repair_jobs
        SET status='queued',attempt_count=CASE WHEN %s THEN 0 ELSE attempt_count END,
            next_retry_at=NULL,last_error=NULL,failure_code=NULL,worker_id=NULL,
            lease_expires_at=NULL,source_node_id=NULL,target_node_id=NULL,transfer_id=NULL,
            reserved_bytes=0,copied_bytes=0,total_chunks=NULL,verified_at=NULL,
            canceled_at=NULL,finished_at=NULL,updated_at=%s
        WHERE repair_job_id=%s
        """,
        (bool(reset_attempts), timestamp, str(repair_job_id)),
    )
    _event(
        cur,
        repair_job_id=str(repair_job_id),
        status="queued",
        event="operator_requeued",
        detail=reason,
        ts=timestamp,
    )
    return {"applied": True, "status": "queued", "repair_job_id": str(repair_job_id)}


def recover_stale_repair_jobs(
    cur,
    *,
    ts: Optional[int] = None,
) -> List[str]:
    timestamp = int(now_ts() if ts is None else ts)
    cur.execute(
        """
        SELECT repair_job_id
        FROM repair_jobs
        WHERE status IN ('selecting_source','selecting_target','copying','verifying')
          AND COALESCE(lease_expires_at,0) <= %s
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED
        """,
        (timestamp,),
    )
    job_ids = [str(_row_dict(row)["repair_job_id"]) for row in cur.fetchall()]
    recovered: List[str] = []
    for job_id in job_ids:
        result = schedule_repair_retry(
            cur,
            repair_job_id=job_id,
            error_code="worker_lease_expired",
            detail="DataServer stopped or did not renew the repair lease",
            ts=timestamp,
        )
        if result.get("applied"):
            recovered.append(job_id)
    return recovered


def fetch_repair_job_statuses(cur, repair_job_ids: Sequence[str]) -> Dict[str, str]:
    ids = [str(value) for value in repair_job_ids if value]
    if not ids:
        return {}
    cur.execute("SELECT repair_job_id,status FROM repair_jobs WHERE repair_job_id=ANY(%s::text[])", (ids,))
    return {str(_row_dict(row)["repair_job_id"]): str(_row_dict(row)["status"]) for row in cur.fetchall()}


def claim_repair_cleanup_tasks(
    cur,
    *,
    limit: int,
    ts: Optional[int] = None,
) -> List[Dict[str, Any]]:
    timestamp = int(now_ts() if ts is None else ts)
    cur.execute(
        """
        SELECT * FROM repair_cleanup_queue
        WHERE status IN ('pending','sent') AND COALESCE(next_retry_at,0) <= %s
        ORDER BY created_at
        LIMIT %s
        FOR UPDATE SKIP LOCKED
        """,
        (timestamp, max(1, int(limit))),
    )
    rows = [_row_dict(row) for row in cur.fetchall()]
    for row in rows:
        cur.execute(
            """
            UPDATE repair_cleanup_queue
            SET status='sent',attempts=attempts+1,next_retry_at=%s,updated_at=%s,last_error=NULL
            WHERE cleanup_id=%s AND status IN ('pending','sent')
            """,
            (timestamp + 300, timestamp, str(row["cleanup_id"])),
        )
    return rows


def mark_repair_cleanup_result(
    cur,
    *,
    node_id: str,
    transfer_id: str,
    success: bool,
    error: Optional[str] = None,
    ts: Optional[int] = None,
) -> None:
    timestamp = int(now_ts() if ts is None else ts)
    if success:
        cur.execute(
            """
            UPDATE repair_cleanup_queue
            SET status='done',next_retry_at=NULL,last_error=NULL,updated_at=%s
            WHERE node_id=%s AND transfer_id=%s AND status<>'done'
            """,
            (timestamp, str(node_id), str(transfer_id)),
        )
    else:
        cur.execute(
            """
            UPDATE repair_cleanup_queue
            SET status='pending',next_retry_at=%s,last_error=%s,updated_at=%s
            WHERE node_id=%s AND transfer_id=%s AND status<>'done'
            """,
            (timestamp + 300, str(error or "cleanup_failed")[:1000], timestamp, str(node_id), str(transfer_id)),
        )


def list_repair_jobs(cur, *, limit: int = 100) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT repair_job_id,file_object_id,source_node_id,target_node_id,reason,status,
               attempt_count,max_attempts,next_retry_at,last_error,failure_code,
               copied_bytes,total_chunks,created_at,started_at,updated_at,finished_at
        FROM repair_jobs
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (max(1, min(1000, int(limit))),),
    )
    return [_row_dict(row) for row in cur.fetchall()]
