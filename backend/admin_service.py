# -*- coding: utf-8 -*-
"""Database services used by the Tricloud Phase 2 admin API."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from meta_db_pg import db_conn, now_ts
from replica_health_service import detect_under_replicated_objects
from repair_object_lock import healthy_replica_count, lock_repair_object
from replica_repair_service import (
    cancel_repair_job,
    enqueue_repair_job,
    list_repair_jobs,
    requeue_repair_job,
)


NODE_ONLINE_WINDOW_SEC = max(5, int(os.environ.get("NODE_ONLINE_WINDOW_SEC", "20")))
TARGET_REPLICA_COUNT = max(1, int(os.environ.get("TARGET_REPLICA_COUNT", "3")))
REPLICA_REQUIRE_RECENT_AUDIT = os.environ.get("REPLICA_REQUIRE_RECENT_AUDIT", "0") == "1"
MAX_PAGE_SIZE = 200


def clamp_limit(value: int, *, default: int = 50, maximum: int = MAX_PAGE_SIZE) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(int(maximum), number))


def clamp_offset(value: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    raise TypeError("admin service requires a dict-row cursor")


def _dicts(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    return [_dict(row) for row in rows]


def _json_value(value: Optional[Dict[str, Any]]) -> Optional[Jsonb]:
    return None if value is None else Jsonb(value)


def write_admin_audit(
    cur,
    *,
    admin_user_id: str,
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    before: Optional[Dict[str, Any]] = None,
    after: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
    result_status: str = "success",
    error_code: Optional[str] = None,
    ts: Optional[int] = None,
) -> str:
    log_id = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO admin_audit_logs(
            log_id,admin_user_id,action,target_type,target_id,before_json,after_json,
            ip_address,user_agent,request_id,result_status,error_code,created_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            log_id,
            str(admin_user_id),
            str(action)[:200],
            None if target_type is None else str(target_type)[:100],
            None if target_id is None else str(target_id)[:300],
            _json_value(before),
            _json_value(after),
            None if ip_address is None else str(ip_address)[:200],
            None if user_agent is None else str(user_agent)[:500],
            None if request_id is None else str(request_id)[:200],
            str(result_status)[:40],
            None if error_code is None else str(error_code)[:100],
            int(now_ts() if ts is None else ts),
        ),
    )
    return log_id


def record_admin_audit(**kwargs: Any) -> str:
    with db_conn() as conn:
        with conn.cursor() as cur:
            log_id = write_admin_audit(cur, **kwargs)
        conn.commit()
    return log_id


def dashboard_summary() -> Dict[str, Any]:
    now = int(now_ts())
    online_after = now - NODE_ONLINE_WINDOW_SEC
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT COUNT(*) AS count FROM users")
            user_count = int(cur.fetchone()["count"])
            cur.execute("SELECT COUNT(*) AS count FROM subscriptions WHERE status IN ('active','trialing')")
            paid_user_count = int(cur.fetchone()["count"])
            cur.execute(
                """
                SELECT COUNT(*) FILTER (WHERE last_seen >= %s) AS online_count,
                       COUNT(*) AS total_count,
                       COALESCE(SUM(capacity_bytes),0) AS capacity_bytes,
                       COALESCE(SUM(reserved_bytes),0) AS reserved_bytes,
                       COUNT(*) FILTER (WHERE COALESCE(placement_paused,FALSE)) AS placement_paused_count
                FROM nodes
                """,
                (online_after,),
            )
            node_stats = _dict(cur.fetchone())
            cur.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE h.status='healthy') AS healthy,
                       COUNT(*) FILTER (WHERE h.status IN ('missing','corrupt')) AS bad
                FROM replicas r
                LEFT JOIN replica_health h
                  ON h.file_object_id=r.file_object_id AND h.node_id=r.node_id
                """
            )
            replica_stats = _dict(cur.fetchone())
            cur.execute(
                """
                SELECT COUNT(*) FILTER (WHERE status IN ('queued','selecting_source','selecting_target','copying','verifying','retry_wait')) AS active,
                       COUNT(*) FILTER (WHERE status='failed') AS failed
                FROM repair_jobs
                """
            )
            repair_stats = _dict(cur.fetchone())
            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM audit_jobs
                WHERE created_at >= %s
                  AND (status='failed' OR failure_kind IS NOT NULL)
                """,
                (now - 24 * 3600,),
            )
            audit_failure_count = int(cur.fetchone()["count"])
            cur.execute("SELECT COALESCE(SUM(total),0) AS total FROM invoices WHERE status='paid'")
            paid_revenue_yen = int(cur.fetchone()["total"] or 0)
            cur.execute("SELECT COUNT(*) AS count FROM subscriptions WHERE status IN ('past_due','unpaid')")
            payment_failure_count = int(cur.fetchone()["count"])
            cur.execute("SELECT COUNT(*) AS count FROM node_earnings WHERE status IN ('calculated','held')")
            unapproved_earning_count = int(cur.fetchone()["count"])
            cur.execute("SELECT COUNT(*) AS count FROM node_payouts WHERE status='failed'")
            payout_failure_count = int(cur.fetchone()["count"])

            shortages = detect_under_replicated_objects(
                cur,
                target_replicas=TARGET_REPLICA_COUNT,
                require_recent_audit=REPLICA_REQUIRE_RECENT_AUDIT,
                limit=10000,
                ts=now,
            )

    total_replicas = int(replica_stats.get("total") or 0)
    healthy_replicas = int(replica_stats.get("healthy") or 0)
    return {
        "generated_at": now,
        "users": {"registered": user_count, "paid": paid_user_count},
        "nodes": {
            "online": int(node_stats.get("online_count") or 0),
            "total": int(node_stats.get("total_count") or 0),
            "capacity_bytes": int(node_stats.get("capacity_bytes") or 0),
            "reserved_bytes": int(node_stats.get("reserved_bytes") or 0),
            "placement_paused": int(node_stats.get("placement_paused_count") or 0),
        },
        "integrity": {
            "healthy_replica_percent": round(100.0 * healthy_replicas / total_replicas, 2) if total_replicas else 100.0,
            "healthy_replicas": healthy_replicas,
            "bad_replicas": int(replica_stats.get("bad") or 0),
            "under_replicated_objects": len(shortages),
            "audit_failures_24h": audit_failure_count,
            "active_repairs": int(repair_stats.get("active") or 0),
            "failed_repairs": int(repair_stats.get("failed") or 0),
        },
        "billing": {
            "paid_revenue_yen": paid_revenue_yen,
            "payment_failures": payment_failure_count,
        },
        "rewards": {
            "unapproved_earnings": unapproved_earning_count,
            "payout_failures": payout_failure_count,
        },
        "app_versions": [],
        "notes": [
            "Node reliability scoring is intentionally not implemented.",
            "App version distribution requires Phase 3 client telemetry.",
        ],
    }


def under_replicated_objects(*, limit: int = 200) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return detect_under_replicated_objects(
                cur,
                target_replicas=TARGET_REPLICA_COUNT,
                require_recent_audit=REPLICA_REQUIRE_RECENT_AUDIT,
                limit=clamp_limit(limit),
            )


def list_integrity_objects(*, query: str = "", limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    pattern = f"%{str(query or '').strip()}%"
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT o.file_object_id,o.owner_user_id,o.size_bytes,o.chunk_size,o.created_at,
                       COALESCE(i.name,'') AS item_name,
                       COUNT(DISTINCT r.node_id) AS replica_count,
                       COUNT(DISTINCT r.node_id) FILTER (WHERE h.status='healthy') AS healthy_count,
                       COUNT(DISTINCT r.node_id) FILTER (WHERE h.status IN ('missing','corrupt')) AS bad_count,
                       MAX(h.last_verified_at) AS last_verified_at
                FROM objects o
                LEFT JOIN LATERAL (
                    SELECT name FROM items
                    WHERE file_object_id=o.file_object_id
                    ORDER BY created_at DESC LIMIT 1
                ) i ON TRUE
                LEFT JOIN replicas r ON r.file_object_id=o.file_object_id
                LEFT JOIN replica_health h
                  ON h.file_object_id=r.file_object_id AND h.node_id=r.node_id
                WHERE (%s='' OR o.file_object_id ILIKE %s OR o.owner_user_id ILIKE %s OR COALESCE(i.name,'') ILIKE %s)
                GROUP BY o.file_object_id,o.owner_user_id,o.size_bytes,o.chunk_size,o.created_at,i.name
                ORDER BY o.created_at DESC,o.file_object_id
                LIMIT %s OFFSET %s
                """,
                (
                    str(query or "").strip(),
                    pattern,
                    pattern,
                    pattern,
                    clamp_limit(limit),
                    clamp_offset(offset),
                ),
            )
            return _dicts(cur.fetchall())


def integrity_object_detail(file_object_id: str) -> Optional[Dict[str, Any]]:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM objects WHERE file_object_id=%s", (str(file_object_id),))
            obj = _dict(cur.fetchone())
            if not obj:
                return None
            cur.execute(
                """
                SELECT r.node_id,r.created_at AS replica_created_at,
                       COALESCE(h.status,'pending') AS health_status,
                       h.last_verified_at,h.last_success_at,h.last_failure_at,
                       h.consecutive_failures,h.last_error,h.updated_at,
                       n.last_seen,n.capacity_bytes,n.reserved_bytes,n.failure_domain,
                       COALESCE(n.placement_paused,FALSE) AS placement_paused
                FROM replicas r
                LEFT JOIN replica_health h
                  ON h.file_object_id=r.file_object_id AND h.node_id=r.node_id
                LEFT JOIN nodes n ON n.node_id=r.node_id
                WHERE r.file_object_id=%s
                ORDER BY r.node_id
                """,
                (str(file_object_id),),
            )
            replicas = _dicts(cur.fetchall())
            cur.execute(
                """
                SELECT audit_job_id,node_id,chunk_id,byte_offset,length,status,purpose,
                       attempt_count,next_retry_at,last_error,failure_kind,result_hash,
                       latency_ms,created_at,updated_at,sent_at,completed_at
                FROM audit_jobs WHERE file_object_id=%s
                ORDER BY created_at DESC LIMIT 100
                """,
                (str(file_object_id),),
            )
            audits = _dicts(cur.fetchall())
            cur.execute(
                """
                SELECT * FROM repair_jobs WHERE file_object_id=%s
                ORDER BY created_at DESC LIMIT 100
                """,
                (str(file_object_id),),
            )
            repairs = _dicts(cur.fetchall())
            return {"object": obj, "replicas": replicas, "audits": audits, "repairs": repairs}


def _queue_manual_audits(cur, *, file_object_id: Optional[str], node_id: Optional[str], limit: int) -> List[str]:
    clauses: List[str] = []
    params: List[Any] = []
    if file_object_id:
        clauses.append("r.file_object_id=%s")
        params.append(str(file_object_id))
    if node_id:
        clauses.append("r.node_id=%s")
        params.append(str(node_id))
    where = " AND ".join(clauses) if clauses else "TRUE"
    params.append(clamp_limit(limit, maximum=500))
    cur.execute(
        f"""
        SELECT r.file_object_id,r.node_id,s.chunk_id,s.byte_offset,s.length,s.hash_hex
        FROM replicas r
        JOIN LATERAL (
            SELECT chunk_id,byte_offset,length,hash_hex
            FROM chunk_audit_slices s
            WHERE s.file_object_id=r.file_object_id
            ORDER BY chunk_id,slice_index LIMIT 1
        ) s ON TRUE
        WHERE {where}
          AND NOT EXISTS (
              SELECT 1 FROM audit_jobs a
              WHERE a.file_object_id=r.file_object_id AND a.node_id=r.node_id
                AND a.status IN ('queued','sent','retry_wait')
          )
        ORDER BY r.file_object_id,r.node_id
        LIMIT %s
        """,
        tuple(params),
    )
    now = int(now_ts())
    created: List[str] = []
    for row in _dicts(cur.fetchall()):
        audit_job_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO audit_jobs(
                audit_job_id,file_object_id,node_id,chunk_id,byte_offset,length,
                expected_hash,status,purpose,repair_job_id,attempt_count,next_retry_at,
                last_error,failure_kind,result_hash,latency_ms,worker_id,current_event_id,
                created_at,updated_at,sent_at,completed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,'queued','scheduled',NULL,0,NULL,
                      NULL,NULL,NULL,NULL,NULL,NULL,%s,%s,NULL,NULL)
            ON CONFLICT DO NOTHING RETURNING audit_job_id
            """,
            (
                audit_job_id,
                row["file_object_id"],
                row["node_id"],
                row["chunk_id"],
                row["byte_offset"],
                row["length"],
                row["hash_hex"],
                now,
                now,
            ),
        )
        inserted = cur.fetchone()
        if inserted:
            created.append(str(_dict(inserted)["audit_job_id"]))
    return created


def force_audits(
    *,
    admin_user_id: str,
    file_object_id: Optional[str] = None,
    node_id: Optional[str] = None,
    limit: int = 100,
    audit_context: Dict[str, Any],
) -> List[str]:
    if not file_object_id and not node_id:
        raise ValueError("file_object_id or node_id is required")
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if file_object_id:
                cur.execute(
                    "SELECT 1 AS found FROM objects WHERE file_object_id=%s",
                    (str(file_object_id),),
                )
                if not cur.fetchone():
                    raise LookupError("object not found")
            if node_id:
                cur.execute(
                    "SELECT 1 AS found FROM nodes WHERE node_id=%s",
                    (str(node_id),),
                )
                if not cur.fetchone():
                    raise LookupError("node not found")
            created = _queue_manual_audits(
                cur,
                file_object_id=file_object_id,
                node_id=node_id,
                limit=limit,
            )
            write_admin_audit(
                cur,
                admin_user_id=admin_user_id,
                action="audit.force_queue",
                target_type="object" if file_object_id else "node",
                target_id=file_object_id or node_id,
                after={"created_audit_job_ids": created},
                **audit_context,
            )
        conn.commit()
    return created


def create_manual_repair(
    *,
    admin_user_id: str,
    file_object_id: str,
    reason: str,
    audit_context: Dict[str, Any],
) -> Optional[str]:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if not lock_repair_object(cur, file_object_id=str(file_object_id)):
                raise LookupError("object not found")
            healthy_count = healthy_replica_count(cur, file_object_id=str(file_object_id))
            if healthy_count >= TARGET_REPLICA_COUNT:
                raise ValueError("object already has the target number of healthy replicas")
            repair_job_id = enqueue_repair_job(
                cur,
                file_object_id=str(file_object_id),
                reason=str(reason or "operator_requested")[:200],
            )
            write_admin_audit(
                cur,
                admin_user_id=admin_user_id,
                action="repair.create",
                target_type="object",
                target_id=str(file_object_id),
                after={"repair_job_id": repair_job_id, "reason": str(reason or "operator_requested")[:200]},
                **audit_context,
            )
        conn.commit()
    return repair_job_id


def repairs(*, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if status:
                cur.execute(
                    "SELECT * FROM repair_jobs WHERE status=%s ORDER BY created_at DESC LIMIT %s",
                    (str(status), clamp_limit(limit, maximum=1000)),
                )
                return _dicts(cur.fetchall())
            return list_repair_jobs(cur, limit=clamp_limit(limit, maximum=1000))


def repair_events(repair_job_id: str) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM repair_job_events WHERE repair_job_id=%s ORDER BY created_at,id",
                (str(repair_job_id),),
            )
            return _dicts(cur.fetchall())


def cancel_repair(
    *, admin_user_id: str, repair_job_id: str, reason: str, audit_context: Dict[str, Any]
) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM repair_jobs WHERE repair_job_id=%s", (str(repair_job_id),))
            before = _dict(cur.fetchone())
            result = cancel_repair_job(cur, repair_job_id=str(repair_job_id), reason=str(reason)[:1000])
            write_admin_audit(
                cur,
                admin_user_id=admin_user_id,
                action="repair.cancel",
                target_type="repair_job",
                target_id=str(repair_job_id),
                before=before or None,
                after=result,
                **audit_context,
            )
        conn.commit()
    return result


def retry_repair(
    *,
    admin_user_id: str,
    repair_job_id: str,
    reason: str,
    reset_attempts: bool,
    audit_context: Dict[str, Any],
) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM repair_jobs WHERE repair_job_id=%s", (str(repair_job_id),))
            before = _dict(cur.fetchone())
            result = requeue_repair_job(
                cur,
                repair_job_id=str(repair_job_id),
                reason=str(reason)[:1000],
                reset_attempts=bool(reset_attempts),
            )
            write_admin_audit(
                cur,
                admin_user_id=admin_user_id,
                action="repair.retry",
                target_type="repair_job",
                target_id=str(repair_job_id),
                before=before or None,
                after=result,
                **audit_context,
            )
        conn.commit()
    return result


def list_nodes(*, query: str = "", limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    pattern = f"%{str(query or '').strip()}%"
    now = int(now_ts())
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT n.node_id,n.last_seen,n.capacity_bytes,n.reserved_bytes,n.failure_domain,
                       COALESCE(n.placement_paused,FALSE) AS placement_paused,
                       np.node_name,np.owner_user_id,np.payout_enabled,
                       COALESCE(np.payouts_paused,FALSE) AS payouts_paused,
                       u.email AS owner_email,u.country_code,
                       (SELECT COUNT(*) FROM replicas r WHERE r.node_id=n.node_id) AS replica_count,
                       (SELECT COUNT(*) FROM replica_health h WHERE h.node_id=n.node_id AND h.status='healthy') AS healthy_replica_count,
                       (SELECT COUNT(*) FROM node_heartbeat_hourly hh WHERE hh.node_id=n.node_id AND hh.hour_start >= %s) AS online_hours_30d,
                       (SELECT ROUND(100.0 * AVG(CASE WHEN a.status='completed' AND a.failure_kind IS NULL THEN 1 ELSE 0 END),2)
                          FROM audit_jobs a WHERE a.node_id=n.node_id AND a.created_at >= %s
                            AND a.status IN ('completed','failed')) AS audit_success_percent,
                       (SELECT ROUND(100.0 * AVG(CASE WHEN m.success THEN 1 ELSE 0 END),2)
                          FROM node_transfer_metrics m WHERE m.node_id=n.node_id AND m.created_at >= %s) AS transfer_success_percent,
                       (SELECT errors.error_text
                          FROM (
                              SELECT h.last_error AS error_text,COALESCE(h.last_failure_at,0) AS error_at
                              FROM replica_health h
                              WHERE h.node_id=n.node_id AND h.last_error IS NOT NULL
                              UNION ALL
                              SELECT m.error_code AS error_text,m.created_at AS error_at
                              FROM node_transfer_metrics m
                              WHERE m.node_id=n.node_id AND NOT m.success AND m.error_code IS NOT NULL
                          ) errors
                          ORDER BY errors.error_at DESC LIMIT 1) AS recent_error
                FROM nodes n
                LEFT JOIN node_profiles np ON np.node_id=n.node_id
                LEFT JOIN users u ON u.user_id=np.owner_user_id
                WHERE (%s='' OR n.node_id ILIKE %s OR COALESCE(np.node_name,'') ILIKE %s OR COALESCE(u.email,'') ILIKE %s)
                ORDER BY n.last_seen DESC,n.node_id
                LIMIT %s OFFSET %s
                """,
                (
                    now - 30 * 24 * 3600,
                    now - 30 * 24 * 3600,
                    now - 30 * 24 * 3600,
                    str(query or "").strip(),
                    pattern,
                    pattern,
                    pattern,
                    clamp_limit(limit),
                    clamp_offset(offset),
                ),
            )
            rows = _dicts(cur.fetchall())
            for row in rows:
                row["online"] = int(row.get("last_seen") or 0) >= now - NODE_ONLINE_WINDOW_SEC
                row["uptime_30d_percent"] = round(min(100.0, 100.0 * int(row.get("online_hours_30d") or 0) / (30 * 24)), 2)
            return rows


def node_detail(node_id: str) -> Optional[Dict[str, Any]]:
    rows = list_nodes(query=str(node_id), limit=200)
    node = next((row for row in rows if str(row.get("node_id")) == str(node_id)), None)
    if not node:
        return None
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT r.file_object_id,r.created_at,COALESCE(h.status,'pending') AS health_status,
                       h.last_verified_at,h.last_error,o.size_bytes
                FROM replicas r
                LEFT JOIN replica_health h
                  ON h.file_object_id=r.file_object_id AND h.node_id=r.node_id
                LEFT JOIN objects o ON o.file_object_id=r.file_object_id
                WHERE r.node_id=%s ORDER BY r.created_at DESC LIMIT 500
                """,
                (str(node_id),),
            )
            node["replicas"] = _dicts(cur.fetchall())
    return node


def update_node_controls(
    *,
    admin_user_id: str,
    node_id: str,
    placement_paused: bool,
    payouts_paused: bool,
    reason: str,
    audit_context: Dict[str, Any],
) -> Dict[str, Any]:
    timestamp = int(now_ts())
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM nodes WHERE node_id=%s FOR UPDATE", (str(node_id),))
            node = _dict(cur.fetchone())
            if not node:
                raise LookupError("node not found")
            cur.execute("SELECT * FROM admin_node_controls WHERE node_id=%s", (str(node_id),))
            before = _dict(cur.fetchone())
            cur.execute(
                """
                INSERT INTO admin_node_controls(
                    node_id,placement_paused,payouts_paused,reason,updated_by,updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (node_id) DO UPDATE SET
                    placement_paused=EXCLUDED.placement_paused,
                    payouts_paused=EXCLUDED.payouts_paused,
                    reason=EXCLUDED.reason,updated_by=EXCLUDED.updated_by,updated_at=EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    str(node_id),
                    bool(placement_paused),
                    bool(payouts_paused),
                    str(reason or "")[:1000] or None,
                    str(admin_user_id),
                    timestamp,
                ),
            )
            after = _dict(cur.fetchone())
            cur.execute(
                "UPDATE nodes SET placement_paused=%s WHERE node_id=%s",
                (bool(placement_paused), str(node_id)),
            )
            cur.execute(
                "UPDATE node_profiles SET payouts_paused=%s,updated_at=%s WHERE node_id=%s",
                (bool(payouts_paused), timestamp, str(node_id)),
            )
            write_admin_audit(
                cur,
                admin_user_id=admin_user_id,
                action="node.controls.update",
                target_type="node",
                target_id=str(node_id),
                before=before or None,
                after=after,
                **audit_context,
            )
        conn.commit()
    return after


def list_users(*, query: str = "", limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    pattern = f"%{str(query or '').strip()}%"
    now = int(now_ts())
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT u.user_id,u.email,u.last_name,u.first_name,u.country_code,u.created_at,
                       s.plan_id,s.status AS subscription_status,s.current_period_end,
                       COALESCE(c.suspended,FALSE) AS suspended,
                       COALESCE(c.abuse_flag,FALSE) AS abuse_flag,
                       COALESCE(c.sharing_disabled,FALSE) AS sharing_disabled,
                       COALESCE(c.downloads_disabled,FALSE) AS downloads_disabled,
                       c.reason AS control_reason,c.updated_at AS controls_updated_at,
                       (SELECT COALESCE(SUM(o.size_bytes),0) FROM objects o WHERE o.owner_user_id=u.user_id) AS storage_bytes,
                       (SELECT COALESCE(SUM(t.bytes),0) FROM transfer_events t WHERE t.user_id=u.user_id AND t.ts >= %s) AS transfer_bytes_30d
                FROM users u
                LEFT JOIN subscriptions s ON s.user_id=u.user_id
                LEFT JOIN admin_user_controls c ON c.user_id=u.user_id
                WHERE (%s='' OR u.user_id ILIKE %s OR u.email ILIKE %s OR COALESCE(u.last_name,'') ILIKE %s OR COALESCE(u.first_name,'') ILIKE %s)
                ORDER BY u.created_at DESC,u.user_id
                LIMIT %s OFFSET %s
                """,
                (
                    now - 30 * 24 * 3600,
                    str(query or "").strip(),
                    pattern,
                    pattern,
                    pattern,
                    pattern,
                    clamp_limit(limit),
                    clamp_offset(offset),
                ),
            )
            return _dicts(cur.fetchall())


def user_detail(user_id: str) -> Optional[Dict[str, Any]]:
    rows = list_users(query=str(user_id), limit=200)
    user = next((row for row in rows if str(row.get("user_id")) == str(user_id)), None)
    if not user:
        return None
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT role,created_at FROM user_roles WHERE user_id=%s ORDER BY role", (str(user_id),))
            user["roles"] = _dicts(cur.fetchall())
            cur.execute(
                "SELECT * FROM admin_audit_logs WHERE target_type='user' AND target_id=%s ORDER BY created_at DESC LIMIT 100",
                (str(user_id),),
            )
            user["admin_history"] = _dicts(cur.fetchall())
    return user


def update_user_controls(
    *,
    admin_user_id: str,
    user_id: str,
    suspended: bool,
    abuse_flag: bool,
    sharing_disabled: bool,
    downloads_disabled: bool,
    reason: str,
    audit_context: Dict[str, Any],
) -> Dict[str, Any]:
    if str(user_id) == str(admin_user_id) and bool(suspended):
        raise ValueError("the active admin cannot suspend their own account")
    timestamp = int(now_ts())
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT user_id FROM users WHERE user_id=%s FOR UPDATE", (str(user_id),))
            if not cur.fetchone():
                raise LookupError("user not found")
            cur.execute("SELECT * FROM admin_user_controls WHERE user_id=%s", (str(user_id),))
            before = _dict(cur.fetchone())
            cur.execute(
                """
                INSERT INTO admin_user_controls(
                    user_id,suspended,abuse_flag,sharing_disabled,downloads_disabled,
                    reason,updated_by,updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (user_id) DO UPDATE SET
                    suspended=EXCLUDED.suspended,abuse_flag=EXCLUDED.abuse_flag,
                    sharing_disabled=EXCLUDED.sharing_disabled,
                    downloads_disabled=EXCLUDED.downloads_disabled,
                    reason=EXCLUDED.reason,updated_by=EXCLUDED.updated_by,updated_at=EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    str(user_id),
                    bool(suspended),
                    bool(abuse_flag),
                    bool(sharing_disabled),
                    bool(downloads_disabled),
                    str(reason or "")[:1000] or None,
                    str(admin_user_id),
                    timestamp,
                ),
            )
            after = _dict(cur.fetchone())
            if bool(suspended):
                cur.execute(
                    "UPDATE admin_sessions SET revoked_at=%s WHERE admin_user_id=%s AND revoked_at IS NULL",
                    (timestamp, str(user_id)),
                )
            write_admin_audit(
                cur,
                admin_user_id=admin_user_id,
                action="user.controls.update",
                target_type="user",
                target_id=str(user_id),
                before=before or None,
                after=after,
                **audit_context,
            )
        conn.commit()
    return after


def billing_overview(*, limit: int = 100) -> Dict[str, Any]:
    page_size = clamp_limit(limit)
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT s.*,u.email,sc.stripe_customer_id
                FROM subscriptions s
                LEFT JOIN users u ON u.user_id=s.user_id
                LEFT JOIN stripe_customers sc ON sc.user_id=s.user_id
                ORDER BY COALESCE(s.updated_at,s.created_at,0) DESC LIMIT %s
                """,
                (page_size,),
            )
            subscriptions = _dicts(cur.fetchall())
            cur.execute("SELECT * FROM invoices ORDER BY created_at DESC LIMIT %s", (page_size,))
            invoices = _dicts(cur.fetchall())
            cur.execute("SELECT * FROM stripe_webhook_events ORDER BY processed_at DESC LIMIT %s", (page_size,))
            webhook_events = _dicts(cur.fetchall())
            cur.execute("SELECT * FROM stripe_plan_prices ORDER BY plan_id")
            plan_prices = _dicts(cur.fetchall())
            cur.execute("SELECT * FROM admin_billing_retry_requests ORDER BY created_at DESC LIMIT %s", (page_size,))
            retry_requests = _dicts(cur.fetchall())
    return {
        "subscriptions": subscriptions,
        "invoices": invoices,
        "webhook_events": webhook_events,
        "plan_prices": plan_prices,
        "retry_requests": retry_requests,
    }


def request_billing_retry(
    *,
    admin_user_id: str,
    event_id: str,
    reason: str,
    audit_context: Dict[str, Any],
) -> Dict[str, Any]:
    timestamp = int(now_ts())
    retry_id = str(uuid.uuid4())
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM stripe_webhook_events WHERE event_id=%s", (str(event_id),))
            event = _dict(cur.fetchone())
            if not event:
                raise LookupError("webhook event not found")
            cur.execute(
                """
                INSERT INTO admin_billing_retry_requests(
                    request_id,event_id,status,requested_by,reason,created_at,updated_at
                ) VALUES (%s,%s,'requested',%s,%s,%s,%s)
                ON CONFLICT DO NOTHING RETURNING *
                """,
                (retry_id, str(event_id), str(admin_user_id), str(reason or "")[:1000] or None, timestamp, timestamp),
            )
            result = _dict(cur.fetchone())
            write_admin_audit(
                cur,
                admin_user_id=admin_user_id,
                action="billing.webhook_retry.request",
                target_type="stripe_webhook_event",
                target_id=str(event_id),
                before=event,
                after=result or {"created": False, "reason": "already_requested"},
                **audit_context,
            )
        conn.commit()
    return result or {"created": False, "reason": "already_requested"}


def rewards_overview(*, limit: int = 100) -> Dict[str, Any]:
    page_size = clamp_limit(limit)
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT e.*,
                       (SELECT ROUND(100.0 * AVG(
                            CASE WHEN a.status='completed' AND a.failure_kind IS NULL THEN 1 ELSE 0 END
                        ),2)
                        FROM audit_jobs a
                        WHERE a.node_id=e.node_id
                          AND a.created_at >= e.period_start
                          AND a.created_at < e.period_end
                          AND a.status IN ('completed','failed')) AS audit_success_percent,
                       (SELECT COUNT(DISTINCT h.hour_start)
                        FROM node_heartbeat_hourly h
                        WHERE h.node_id=e.node_id
                          AND h.hour_start >= e.period_start
                          AND h.hour_start < e.period_end) AS online_hours
                FROM node_earnings e
                ORDER BY e.created_at DESC LIMIT %s
                """,
                (page_size,),
            )
            earnings = _dicts(cur.fetchall())
            for earning in earnings:
                period_seconds = max(
                    3600,
                    int(earning.get("period_end") or 0) - int(earning.get("period_start") or 0),
                )
                expected_hours = max(1, (period_seconds + 3599) // 3600)
                earning["uptime_percent"] = round(
                    min(100.0, 100.0 * int(earning.get("online_hours") or 0) / expected_hours),
                    2,
                )
            cur.execute("SELECT * FROM node_payouts ORDER BY created_at DESC LIMIT %s", (page_size,))
            payouts = _dicts(cur.fetchall())
    return {"earnings": earnings, "payouts": payouts}


def update_earning_status(
    *,
    admin_user_id: str,
    earning_id: str,
    status: str,
    note: str,
    audit_context: Dict[str, Any],
) -> Dict[str, Any]:
    target_status = str(status).strip().lower()
    if target_status not in {"approved", "held", "void"}:
        raise ValueError("status must be approved, held, or void")
    timestamp = int(now_ts())
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM node_earnings WHERE earning_id=%s FOR UPDATE", (str(earning_id),))
            before = _dict(cur.fetchone())
            if not before:
                raise LookupError("earning not found")
            if str(before.get("status")) == "paid":
                raise ValueError("paid earnings cannot be changed")
            cur.execute(
                """
                UPDATE node_earnings SET status=%s,note=%s,updated_at=%s
                WHERE earning_id=%s RETURNING *
                """,
                (target_status, str(note or "")[:2000] or None, timestamp, str(earning_id)),
            )
            after = _dict(cur.fetchone())
            write_admin_audit(
                cur,
                admin_user_id=admin_user_id,
                action=f"reward.{target_status}",
                target_type="node_earning",
                target_id=str(earning_id),
                before=before,
                after=after,
                **audit_context,
            )
        conn.commit()
    return after


def list_releases() -> List[Dict[str, Any]]:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM admin_release_registry ORDER BY updated_at DESC,version DESC")
            return _dicts(cur.fetchall())


def upsert_release(
    *,
    admin_user_id: str,
    version: str,
    channel: str,
    status: str,
    minimum_supported: bool,
    force_update: bool,
    rollout_percent: int,
    release_notes: str,
    audit_context: Dict[str, Any],
) -> Dict[str, Any]:
    normalized_version = str(version or "").strip()
    if not normalized_version or len(normalized_version) > 100:
        raise ValueError("valid version is required")
    normalized_status = str(status or "draft").strip().lower()
    if normalized_status not in {"draft", "active", "paused", "retired"}:
        raise ValueError("invalid release status")
    rollout = max(0, min(100, int(rollout_percent)))
    timestamp = int(now_ts())
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM admin_release_registry WHERE version=%s", (normalized_version,))
            before = _dict(cur.fetchone())
            cur.execute(
                """
                INSERT INTO admin_release_registry(
                    version,channel,status,minimum_supported,force_update,rollout_percent,
                    release_notes,created_by,created_at,updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (version) DO UPDATE SET
                    channel=EXCLUDED.channel,status=EXCLUDED.status,
                    minimum_supported=EXCLUDED.minimum_supported,force_update=EXCLUDED.force_update,
                    rollout_percent=EXCLUDED.rollout_percent,release_notes=EXCLUDED.release_notes,
                    updated_at=EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    normalized_version,
                    str(channel or "stable")[:50],
                    normalized_status,
                    bool(minimum_supported),
                    bool(force_update),
                    rollout,
                    str(release_notes or "")[:10000] or None,
                    str(admin_user_id),
                    timestamp,
                    timestamp,
                ),
            )
            after = _dict(cur.fetchone())
            write_admin_audit(
                cur,
                admin_user_id=admin_user_id,
                action="release.upsert",
                target_type="release",
                target_id=normalized_version,
                before=before or None,
                after=after,
                **audit_context,
            )
        conn.commit()
    return after


def list_admin_audit_logs(
    *, query: str = "", limit: int = 100, offset: int = 0
) -> List[Dict[str, Any]]:
    pattern = f"%{str(query or '').strip()}%"
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT * FROM admin_audit_logs
                WHERE (%s='' OR action ILIKE %s OR COALESCE(target_id,'') ILIKE %s OR admin_user_id ILIKE %s)
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (
                    str(query or "").strip(),
                    pattern,
                    pattern,
                    pattern,
                    clamp_limit(limit, maximum=1000),
                    clamp_offset(offset),
                ),
            )
            rows = _dicts(cur.fetchall())
            for row in rows:
                for key in ("before_json", "after_json"):
                    value = row.get(key)
                    if isinstance(value, str):
                        try:
                            row[key] = json.loads(value)
                        except Exception:
                            pass
            return rows
