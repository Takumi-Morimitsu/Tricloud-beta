# -*- coding: utf-8 -*-
"""Persistent ciphertext-slice audit jobs for Tricloud storage nodes.

The service stores scheduling and result state only.  The DataServer sends the
actual challenge because it already owns the authenticated node ROUTER socket.
No function in this module decrypts file contents.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List, Optional, Sequence

from meta_db_pg import db_conn, now_ts
from replica_health_service import (
    _tracked_object_union,
    mark_replica_failure,
    mark_replica_healthy,
)
from repair_transfer import retry_delay_seconds


AUDIT_TIMEOUT_SEC = float(os.environ.get("AUDIT_TIMEOUT_SEC", "8"))
AUDIT_MAX_ATTEMPTS = int(os.environ.get("AUDIT_MAX_ATTEMPTS", "3"))
AUDIT_MISMATCH_CORRUPT_THRESHOLD = int(os.environ.get("AUDIT_MISMATCH_CORRUPT_THRESHOLD", "2"))
AUDIT_RETRY_DELAYS_SEC = [
    int(value.strip())
    for value in os.environ.get("AUDIT_RETRY_DELAYS_SEC", "30,300,1800").split(",")
    if value.strip()
]

ACTIVE_AUDIT_STATUSES = {"queued", "sent", "retry_wait"}
TERMINAL_AUDIT_STATUSES = {"completed", "failed", "canceled"}


STORAGE_AUDIT_DDL = [
    """
    CREATE TABLE IF NOT EXISTS audit_jobs (
        audit_job_id TEXT PRIMARY KEY,
        file_object_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        chunk_id INTEGER NOT NULL,
        byte_offset INTEGER NOT NULL,
        length INTEGER NOT NULL,
        expected_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        purpose TEXT NOT NULL DEFAULT 'scheduled',
        repair_job_id TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        next_retry_at INTEGER,
        last_error TEXT,
        failure_kind TEXT,
        result_hash TEXT,
        latency_ms INTEGER,
        worker_id TEXT,
        current_event_id TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        sent_at INTEGER,
        completed_at INTEGER
    )
    """,
    "ALTER TABLE audit_jobs ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'scheduled'",
    "ALTER TABLE audit_jobs ADD COLUMN IF NOT EXISTS repair_job_id TEXT",
    "ALTER TABLE audit_jobs ADD COLUMN IF NOT EXISTS failure_kind TEXT",
    "ALTER TABLE audit_jobs ADD COLUMN IF NOT EXISTS result_hash TEXT",
    "ALTER TABLE audit_jobs ADD COLUMN IF NOT EXISTS latency_ms INTEGER",
    "ALTER TABLE audit_jobs ADD COLUMN IF NOT EXISTS worker_id TEXT",
    "ALTER TABLE audit_jobs ADD COLUMN IF NOT EXISTS current_event_id TEXT",
    "ALTER TABLE audit_jobs ADD COLUMN IF NOT EXISTS updated_at INTEGER",
    "CREATE INDEX IF NOT EXISTS idx_audit_jobs_due ON audit_jobs(status,next_retry_at,created_at)",
    "CREATE INDEX IF NOT EXISTS idx_audit_jobs_replica ON audit_jobs(file_object_id,node_id,created_at)",
    "CREATE INDEX IF NOT EXISTS idx_audit_jobs_repair ON audit_jobs(repair_job_id,status)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_jobs_active_replica
    ON audit_jobs(file_object_id,node_id)
    WHERE status IN ('queued','sent','retry_wait')
    """,
    """
    CREATE TABLE IF NOT EXISTS chunk_audit_slices (
        file_object_id TEXT NOT NULL,
        chunk_id INTEGER NOT NULL,
        slice_index INTEGER NOT NULL,
        byte_offset INTEGER NOT NULL,
        length INTEGER NOT NULL,
        hash_hex TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        PRIMARY KEY (file_object_id,chunk_id,slice_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chunk_audit_results (
        event_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        got_hash TEXT,
        latency_ms INTEGER,
        created_at INTEGER NOT NULL
    )
    """,
]


def init_storage_audit_schema(cur=None) -> None:
    if cur is not None:
        _apply_schema(cur)
        return
    with db_conn() as conn:
        with conn.cursor() as local_cur:
            _apply_schema(local_cur)
        conn.commit()


def _apply_schema(cur) -> None:
    for statement in STORAGE_AUDIT_DDL:
        cur.execute(statement)
    # Existing installations may have rows from a partial migration.
    cur.execute("UPDATE audit_jobs SET updated_at=COALESCE(updated_at,created_at) WHERE updated_at IS NULL")


def _row_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    raise TypeError("audit service requires a dict-row cursor")


def schedule_due_audit_jobs(
    cur,
    *,
    due_before: int,
    online_after: int,
    limit: int = 1,
    ts: Optional[int] = None,
) -> List[str]:
    """Create idempotent scheduled jobs for the least recently verified replicas."""
    timestamp = int(now_ts() if ts is None else ts)
    tracked_union = _tracked_object_union(cur)
    cur.execute(
        f"""
        WITH tracked_objects AS (
            SELECT DISTINCT file_object_id FROM ({tracked_union}) tracked
        )
        SELECT r.file_object_id,r.node_id,
               s.chunk_id,s.byte_offset,s.length,s.hash_hex
        FROM tracked_objects t
        JOIN replicas r ON r.file_object_id=t.file_object_id
        JOIN nodes n ON n.node_id=r.node_id
        LEFT JOIN replica_health h
          ON h.file_object_id=r.file_object_id AND h.node_id=r.node_id
        JOIN LATERAL (
            SELECT chunk_id,byte_offset,length,hash_hex
            FROM chunk_audit_slices cas
            WHERE cas.file_object_id=r.file_object_id
            ORDER BY random()
            LIMIT 1
        ) s ON TRUE
        WHERE n.last_seen >= %s
          AND COALESCE(h.status,'pending') NOT IN ('repairing','deleting','deleted')
          AND GREATEST(COALESCE(h.last_verified_at,0),COALESCE(h.last_failure_at,0)) <= %s
          AND NOT EXISTS (
              SELECT 1 FROM audit_jobs aj
              WHERE aj.file_object_id=r.file_object_id
                AND aj.node_id=r.node_id
                AND aj.status IN ('queued','sent','retry_wait')
          )
        ORDER BY GREATEST(COALESCE(h.last_verified_at,0),COALESCE(h.last_failure_at,0)) ASC,random()
        LIMIT %s
        """,
        (int(online_after), int(due_before), max(1, int(limit))),
    )

    created: List[str] = []
    for raw in cur.fetchall():
        row = _row_dict(raw)
        job_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO audit_jobs(
                audit_job_id,file_object_id,node_id,chunk_id,byte_offset,length,
                expected_hash,status,purpose,repair_job_id,attempt_count,next_retry_at,
                last_error,failure_kind,result_hash,latency_ms,worker_id,
                created_at,updated_at,sent_at,completed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,'queued','scheduled',NULL,0,NULL,
                      NULL,NULL,NULL,NULL,NULL,%s,%s,NULL,NULL)
            ON CONFLICT DO NOTHING
            RETURNING audit_job_id
            """,
            (
                job_id,
                str(row["file_object_id"]),
                str(row["node_id"]),
                int(row["chunk_id"]),
                int(row["byte_offset"]),
                int(row["length"]),
                str(row["hash_hex"]),
                timestamp,
                timestamp,
            ),
        )
        inserted = cur.fetchone()
        if inserted:
            created.append(str(_row_dict(inserted)["audit_job_id"]))
    return created


def create_repair_verification_audit(
    cur,
    *,
    repair_job_id: str,
    file_object_id: str,
    node_id: str,
    ts: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Queue one trusted slice challenge for a newly copied replica."""
    timestamp = int(now_ts() if ts is None else ts)
    cur.execute(
        """
        SELECT chunk_id,byte_offset,length,hash_hex
        FROM chunk_audit_slices
        WHERE file_object_id=%s
        ORDER BY chunk_id ASC,slice_index ASC
        LIMIT 1
        """,
        (str(file_object_id),),
    )
    slice_row = cur.fetchone()
    if not slice_row:
        return None
    selected = _row_dict(slice_row)
    audit_job_id = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO audit_jobs(
            audit_job_id,file_object_id,node_id,chunk_id,byte_offset,length,
            expected_hash,status,purpose,repair_job_id,attempt_count,next_retry_at,
            last_error,failure_kind,result_hash,latency_ms,worker_id,
            created_at,updated_at,sent_at,completed_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,'queued','repair_verify',%s,0,NULL,
                  NULL,NULL,NULL,NULL,NULL,%s,%s,NULL,NULL)
        ON CONFLICT DO NOTHING
        RETURNING *
        """,
        (
            audit_job_id,
            str(file_object_id),
            str(node_id),
            int(selected["chunk_id"]),
            int(selected["byte_offset"]),
            int(selected["length"]),
            str(selected["hash_hex"]),
            str(repair_job_id),
            timestamp,
            timestamp,
        ),
    )
    return _row_dict(cur.fetchone()) or None


def claim_due_audit_jobs(
    cur,
    *,
    worker_id: str,
    limit: int,
    include_scheduled: bool = True,
    ts: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Claim queue rows without blocking another audit consumer."""
    timestamp = int(now_ts() if ts is None else ts)
    cur.execute(
        """
        SELECT *
        FROM audit_jobs
        WHERE status IN ('queued','retry_wait')
          AND COALESCE(next_retry_at,0) <= %s
          AND (%s OR purpose='repair_verify')
        ORDER BY CASE WHEN purpose='repair_verify' THEN 0 ELSE 1 END,created_at
        LIMIT %s
        FOR UPDATE SKIP LOCKED
        """,
        (timestamp, bool(include_scheduled), max(1, int(limit))),
    )
    rows = [_row_dict(row) for row in cur.fetchall()]
    claimed: List[Dict[str, Any]] = []
    for row in rows:
        event_id = str(uuid.uuid4())
        cur.execute(
            """
            UPDATE audit_jobs
            SET status='sent',attempt_count=attempt_count+1,next_retry_at=NULL,
                last_error=NULL,failure_kind=NULL,worker_id=%s,current_event_id=%s,
                sent_at=%s,updated_at=%s
            WHERE audit_job_id=%s AND status IN ('queued','retry_wait')
            RETURNING *
            """,
            (str(worker_id), event_id, timestamp, timestamp, str(row["audit_job_id"])),
        )
        updated = _row_dict(cur.fetchone())
        if updated:
            claimed.append(updated)
    return claimed


def complete_audit_job(
    cur,
    *,
    audit_job_id: str,
    event_id: Optional[str] = None,
    outcome: str,
    got_hash: str = "",
    latency_ms: int = 0,
    max_attempts: int = AUDIT_MAX_ATTEMPTS,
    mismatch_corrupt_threshold: int = AUDIT_MISMATCH_CORRUPT_THRESHOLD,
    retry_delays: Sequence[int] = tuple(AUDIT_RETRY_DELAYS_SEC),
    ts: Optional[int] = None,
) -> Dict[str, Any]:
    """Apply one result exactly once and return the resulting classification."""
    timestamp = int(now_ts() if ts is None else ts)
    normalized = str(outcome or "error").lower()
    if normalized not in {"ok", "missing", "hash_mismatch", "timeout", "error"}:
        normalized = "error"

    cur.execute("SELECT * FROM audit_jobs WHERE audit_job_id=%s FOR UPDATE", (str(audit_job_id),))
    job = _row_dict(cur.fetchone())
    if not job:
        return {"applied": False, "reason": "not_found", "audit_job_id": str(audit_job_id)}
    if str(job.get("status")) in TERMINAL_AUDIT_STATUSES:
        return {"applied": False, "reason": "already_terminal", **job}
    if str(job.get("status")) != "sent":
        return {"applied": False, "reason": "not_sent", **job}
    expected_event_id = str(job.get("current_event_id") or job["audit_job_id"])
    received_event_id = str(event_id or audit_job_id)
    if received_event_id != expected_event_id:
        return {"applied": False, "reason": "stale_attempt", **job}

    attempt_count = int(job.get("attempt_count") or 0)
    file_object_id = str(job["file_object_id"])
    node_id = str(job["node_id"])
    purpose = str(job.get("purpose") or "scheduled")
    repair_job_id = job.get("repair_job_id")
    health_status: Optional[str] = None
    repair_needed = False
    terminal = False
    next_retry_at: Optional[int] = None

    if normalized == "ok":
        job_status = "completed"
        terminal = True
        mark_replica_healthy(
            cur,
            file_object_id=file_object_id,
            node_id=node_id,
            verified=True,
            ts=timestamp,
        )
        health_status = "healthy"
    else:
        cur.execute(
            """
            SELECT consecutive_failures
            FROM replica_health
            WHERE file_object_id=%s AND node_id=%s
            """,
            (file_object_id, node_id),
        )
        failure_row = cur.fetchone()
        prior_failures = int((_row_dict(failure_row).get("consecutive_failures") if failure_row else 0) or 0)
        projected_failures = prior_failures + 1

        if normalized == "missing":
            health_status = "missing"
            terminal = True
        elif normalized == "hash_mismatch":
            threshold = max(1, int(mismatch_corrupt_threshold))
            health_status = "corrupt" if projected_failures >= threshold or attempt_count >= max(1, int(max_attempts)) else "suspect"
            terminal = health_status == "corrupt"
        else:
            health_status = "suspect"
            terminal = attempt_count >= max(1, int(max_attempts))

        repair_needed = health_status in {"missing", "corrupt"} and purpose != "repair_verify"
        mark_replica_failure(
            cur,
            file_object_id=file_object_id,
            node_id=node_id,
            status=health_status,
            error=f"audit_{normalized}",
            verified_failure=normalized in {"missing", "hash_mismatch"},
            ts=timestamp,
        )
        if terminal:
            job_status = "failed"
        else:
            job_status = "retry_wait"
            delay = retry_delay_seconds(attempt_count, [int(x) for x in retry_delays])
            next_retry_at = timestamp + delay

    completed_at = timestamp if terminal else None
    cur.execute(
        """
        UPDATE audit_jobs
        SET status=%s,next_retry_at=%s,last_error=%s,failure_kind=%s,
            result_hash=%s,latency_ms=%s,updated_at=%s,completed_at=%s
        WHERE audit_job_id=%s
        """,
        (
            job_status,
            next_retry_at,
            None if normalized == "ok" else f"audit_{normalized}",
            None if normalized == "ok" else normalized,
            str(got_hash or "")[:200],
            max(0, int(latency_ms or 0)),
            timestamp,
            completed_at,
            str(audit_job_id),
        ),
    )
    cur.execute(
        """
        INSERT INTO chunk_audit_results(event_id,status,got_hash,latency_ms,created_at)
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (event_id) DO UPDATE SET
          status=EXCLUDED.status,got_hash=EXCLUDED.got_hash,latency_ms=EXCLUDED.latency_ms
        """,
        (received_event_id, normalized, str(got_hash or "")[:200], max(0, int(latency_ms or 0)), timestamp),
    )
    return {
        "applied": True,
        "audit_job_id": str(audit_job_id),
        "file_object_id": file_object_id,
        "node_id": node_id,
        "purpose": purpose,
        "repair_job_id": None if repair_job_id is None else str(repair_job_id),
        "outcome": normalized,
        "status": job_status,
        "health_status": health_status,
        "repair_needed": repair_needed,
        "terminal": terminal,
        "next_retry_at": next_retry_at,
    }


def recover_stale_audit_jobs(
    cur,
    *,
    timeout_sec: float = AUDIT_TIMEOUT_SEC,
    max_attempts: int = AUDIT_MAX_ATTEMPTS,
    exclude_event_ids: Sequence[str] = (),
    ts: Optional[int] = None,
) -> int:
    """Return sent rows abandoned by a stopped DataServer to a retryable state."""
    timestamp = int(now_ts() if ts is None else ts)
    stale_before = timestamp - max(1, int(timeout_sec))
    excluded = [str(value) for value in exclude_event_ids if value]
    cur.execute(
        """
        SELECT audit_job_id,current_event_id
        FROM audit_jobs
        WHERE status='sent' AND COALESCE(sent_at,created_at) <= %s
          AND NOT (COALESCE(current_event_id,'') = ANY(%s::text[]))
        ORDER BY sent_at,created_at
        FOR UPDATE SKIP LOCKED
        """,
        (stale_before, excluded),
    )
    rows = [_row_dict(row) for row in cur.fetchall()]
    recovered = 0
    for row in rows:
        result = complete_audit_job(
            cur,
            audit_job_id=str(row["audit_job_id"]),
            event_id=str(row.get("current_event_id") or row["audit_job_id"]),
            outcome="timeout",
            latency_ms=max(0, int(timeout_sec * 1000)),
            max_attempts=max(1, int(max_attempts)),
            ts=timestamp,
        )
        if result.get("applied"):
            recovered += 1
    return recovered
