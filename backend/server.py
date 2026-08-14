# -*- coding: utf-8 -*-
"""
サーバー_patched_pg_fixed.py

目的:
- 添付の サーバー_patched.py の「壊れた構文/インデント」を踏まえ、DataServer 部分を
  PostgreSQL(psycopg3) 前提で再構成した修復版。
- 共有リンクDLの日次枠対応（owner課金主体）
- 実測ベース上限（送れた分だけ egress 計上、上限到達で中断）
- sqlite3 の ? プレースホルダを psycopg3 の %s に統一

注意:
- download_tokens に charge_user_id / is_shared がある v2 スキーマ（データベース_dailycap_v2 相当）を前提
- 使用量計算_postgres.py / meta_db_pg.py と組み合わせる想定
"""
from __future__ import annotations

import hmac
import json
import os
import time
import uuid
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import zmq
from psycopg.rows import dict_row

from meta_db_pg import db_conn, init_schema, ensure_default_plan, now_ts
from usage_metering import (
    ensure_object_lifetime_started,
    record_transfer_event,
    check_cap_allow_send,
)
from node_heartbeat_stats_patch import (
    init_node_heartbeat_stats_schema,
    record_node_heartbeat_sample,
)
from object_gc import (
    fetch_pending_object_gc_tasks,
    gc_unreferenced_objects,
    init_object_gc_schema,
    mark_object_gc_task_done_by_reply,
    mark_object_gc_task_failed,
    mark_object_gc_task_sent,
)
from download_failover import DownloadFailoverState
from repair_transfer import RepairTransferState
from replica_health_service import (
    fetch_download_candidates,
    init_storage_maintenance_schema,
    mark_replica_failure,
    mark_replica_healthy,
    mark_replicas_healthy,
    record_node_transfer_metric,
)
from repair_object_lock import lock_repair_object
from storage_audit_service import (
    AUDIT_MAX_ATTEMPTS,
    AUDIT_TIMEOUT_SEC,
    claim_due_audit_jobs,
    complete_audit_job,
    create_repair_verification_audit,
    init_storage_audit_schema,
    recover_stale_audit_jobs,
    schedule_due_audit_jobs,
)
from replica_repair_service import (
    REPAIR_LEASE_SEC,
    REPAIR_SOURCE_AUDIT_VALID_SEC,
    claim_due_repair_jobs,
    claim_repair_cleanup_tasks,
    complete_repair_job,
    enqueue_repair_job,
    fetch_repair_job_statuses,
    init_replica_repair_schema,
    mark_repair_cleanup_result,
    mark_repair_copying,
    mark_repair_target_started,
    mark_repair_verifying,
    note_source_failure,
    queue_repair_cleanup,
    recover_stale_repair_jobs,
    renew_repair_lease,
    schedule_repair_retry,
    select_and_reserve_target,
    select_source_candidates,
    update_repair_progress,
)
from auth_util import jwt_decode, JWT_SECRET  # type: ignore

ROOT_ID = "root"
SESSION_TTL_SEC = 3600
CHUNK_SIZE_DEFAULT = 256 * 1024
CHUNK_METER_FLUSH_BYTES = 64 * 1024 * 1024
REQUIRE_UPLOAD_AUTH = True
OBJECT_GC_POLL_SEC = float(os.environ.get("OBJECT_GC_POLL_SEC", "5"))
OBJECT_GC_QUEUE_BATCH = int(os.environ.get("OBJECT_GC_QUEUE_BATCH", "50"))
OBJECT_GC_RETRY_AFTER_SEC = int(os.environ.get("OBJECT_GC_RETRY_AFTER_SEC", "300"))
OBJECT_GC_MAX_ATTEMPTS = int(os.environ.get("OBJECT_GC_MAX_ATTEMPTS", "5"))
REQUIRE_NODE_API_KEY = os.environ.get("REQUIRE_NODE_API_KEY", "1").lower() not in {"0", "false", "no", "off"}

# ZeroMQ node connection settings. All values are milliseconds unless noted.
# Environment variables allow production tuning without changing the source.
NODE_ZMQ_HEARTBEAT_IVL_MS = int(os.environ.get("NODE_ZMQ_HEARTBEAT_IVL_MS", "3000"))
NODE_ZMQ_HEARTBEAT_TIMEOUT_MS = int(os.environ.get("NODE_ZMQ_HEARTBEAT_TIMEOUT_MS", "10000"))
NODE_ZMQ_HEARTBEAT_TTL_MS = int(os.environ.get("NODE_ZMQ_HEARTBEAT_TTL_MS", "15000"))
NODE_ZMQ_HANDSHAKE_IVL_MS = int(os.environ.get("NODE_ZMQ_HANDSHAKE_IVL_MS", "5000"))
NODE_ONLINE_WINDOW_SEC = int(os.environ.get("NODE_ONLINE_WINDOW_SEC", "20"))
NODE_HEARTBEAT_LOG_INTERVAL_SEC = int(os.environ.get("NODE_HEARTBEAT_LOG_INTERVAL_SEC", "60"))

# Replica download failover.  The per-node timeout stays below the UI bridge's
# default 15 second receive timeout; a retry status is also sent between attempts.
DOWNLOAD_NODE_TIMEOUT_SEC = float(os.environ.get("DOWNLOAD_NODE_TIMEOUT_SEC", "4"))
DOWNLOAD_MAX_REPLICA_ATTEMPTS = int(os.environ.get("DOWNLOAD_MAX_REPLICA_ATTEMPTS", "3"))

# Periodic ciphertext audits.  The implementation is complete but remains
# opt-in so a schema migration never starts production I/O by itself.
STORAGE_AUDIT_ENABLED = os.environ.get("STORAGE_AUDIT_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
REPLICA_REPAIR_QUEUE_ENABLED = os.environ.get("REPLICA_REPAIR_QUEUE_ENABLED", "0").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUDIT_SCHEDULE_INTERVAL_SEC = float(os.environ.get("AUDIT_SCHEDULE_INTERVAL_SEC", "5"))
AUDIT_TARGET_AGE_SEC = int(os.environ.get("AUDIT_TARGET_AGE_SEC", str(72 * 3600)))
AUDIT_MAX_INFLIGHT = int(os.environ.get("AUDIT_MAX_INFLIGHT", "16"))
AUDIT_SCHEDULE_BATCH = int(os.environ.get("AUDIT_SCHEDULE_BATCH", "4"))

# Encrypted DataServer-relayed repairs.  Queue creation and execution have
# separate feature flags to support observation -> approval -> automation.
REPLICA_REPAIR_EXECUTION_ENABLED = os.environ.get("REPLICA_REPAIR_EXECUTION_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
REPAIR_POLL_INTERVAL_SEC = float(os.environ.get("REPAIR_POLL_INTERVAL_SEC", "1"))
REPAIR_MAX_INFLIGHT = int(os.environ.get("REPAIR_MAX_INFLIGHT", "2"))
REPAIR_MAX_SOURCE_ATTEMPTS = int(os.environ.get("REPAIR_MAX_SOURCE_ATTEMPTS", "3"))
REPAIR_STEP_TIMEOUT_SEC = float(os.environ.get("REPAIR_STEP_TIMEOUT_SEC", "15"))
REPAIR_PROGRESS_FLUSH_BYTES = int(os.environ.get("REPAIR_PROGRESS_FLUSH_BYTES", str(64 * 1024 * 1024)))
REPAIR_CLEANUP_BATCH = int(os.environ.get("REPAIR_CLEANUP_BATCH", "20"))


def jdump(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def jload(b: bytes) -> Any:
    return json.loads(b.decode("utf-8"))


def s(x: Any) -> str:
    if isinstance(x, bytes):
        return x.decode("utf-8", "replace")
    return str(x)


def sha256_hex(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _log_event(level: str, event: str, **fields: Any) -> None:
    """Write one immediately flushed, structured line to journald/stdout.

    Secrets such as node_api_key must never be included in ``fields``.
    """
    timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    suffix = ""
    if fields:
        suffix = " " + json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str)
    print(f"[tricloud-dataserver {timestamp}] {level.upper()} {event}{suffix}", flush=True)


@dataclass
class UploadSessionCache:
    owner_user_id: str
    node_ids: List[str]
    file_name: str
    file_size: int
    chunk_size: int
    file_object_id: str
    shared_mode: str = "normal"          # normal / share_upload / share_replace
    shared_parent_id: Optional[str] = None
    shared_replace_item_id: Optional[str] = None


@dataclass
class TransferCtx:
    transfer_id: str
    client_id: bytes
    file_object_id: str
    total_chunks: int
    charge_user_id: str
    is_shared: bool
    failover: DownloadFailoverState
    bytes_since_flush: int = 0
    aborted_by_cap: bool = False
    client_ready_sent: bool = False




@dataclass
class AuditPending:
    event_id: str
    audit_job_id: str
    node_id: str
    file_object_id: str
    chunk_id: int
    offset: int
    length: int
    expected_hash: str
    sent_ts: float
    purpose: str = "scheduled"
    repair_job_id: Optional[str] = None
class DataServer:
    def __init__(self, client_endpoint: str = "tcp://*:8888", node_endpoint: str = "tcp://*:9999"):
        self.client_endpoint = client_endpoint
        self.node_endpoint = node_endpoint

        self.ctx = zmq.Context.instance()
        self.client_sock = self.ctx.socket(zmq.ROUTER)
        self.client_sock.setsockopt(zmq.LINGER, 0)
        self.client_sock.bind(client_endpoint)

        self.node_sock = self.ctx.socket(zmq.ROUTER)
        self.node_sock.setsockopt(zmq.LINGER, 0)

        # A reconnecting Windows node uses the same routing identity (node_id).
        # HANDOVER makes the newest connection replace a stale connection that
        # still owns the same identity.
        self.node_sock.setsockopt(zmq.ROUTER_HANDOVER, 1)

        # ZMTP-level heartbeat closes dead TCP connections promptly. This works
        # in addition to Tricloud's application-level heartbeat stored in DB.
        self.node_sock.setsockopt(zmq.HANDSHAKE_IVL, NODE_ZMQ_HANDSHAKE_IVL_MS)
        self.node_sock.setsockopt(zmq.HEARTBEAT_IVL, NODE_ZMQ_HEARTBEAT_IVL_MS)
        self.node_sock.setsockopt(zmq.HEARTBEAT_TIMEOUT, NODE_ZMQ_HEARTBEAT_TIMEOUT_MS)
        self.node_sock.setsockopt(zmq.HEARTBEAT_TTL, NODE_ZMQ_HEARTBEAT_TTL_MS)
        self.node_sock.bind(node_endpoint)

        self.poller = zmq.Poller()
        self.poller.register(self.client_sock, zmq.POLLIN)
        self.poller.register(self.node_sock, zmq.POLLIN)

        # 簡易キャッシュ（プロトタイプ）
        self.uploads: Dict[str, UploadSessionCache] = {}
        self.transfers: Dict[str, TransferCtx] = {}
        # node attempt id -> stable client transfer id.  A fresh id is used for
        # every replica attempt so delayed frames from an old node are ignored.
        self.node_transfer_index: Dict[str, str] = {}

        # In-memory heartbeat state is used only for concise diagnostics.
        # PostgreSQL remains the source of truth for online/offline status.
        self._heartbeat_last_accepted: Dict[str, int] = {}
        self._heartbeat_last_logged: Dict[str, int] = {}
        self._heartbeat_reject_last_logged: Dict[str, int] = {}

        _log_event(
            "INFO",
            "ZeroMQ node router configured",
            node_endpoint=node_endpoint,
            router_handover=True,
            heartbeat_ivl_ms=NODE_ZMQ_HEARTBEAT_IVL_MS,
            heartbeat_timeout_ms=NODE_ZMQ_HEARTBEAT_TIMEOUT_MS,
            heartbeat_ttl_ms=NODE_ZMQ_HEARTBEAT_TTL_MS,
            handshake_ivl_ms=NODE_ZMQ_HANDSHAKE_IVL_MS,
        )

        init_schema()
        ensure_default_plan()
        self._init_node_heartbeat_stats_schema()
        init_storage_maintenance_schema()
        init_storage_audit_schema()
        init_replica_repair_schema()

        # --- audit (challenge-response) ---
        self.audit_pending: Dict[str, AuditPending] = {}
        self.audit_worker_id = f"dataserver-audit:{uuid.uuid4().hex}"
        self._next_audit_ts: float = time.time() + 1.0

        # --- encrypted replica repair ---
        self.repairs: Dict[str, RepairTransferState] = {}
        self.repair_source_index: Dict[str, str] = {}
        self.repair_target_index: Dict[str, str] = {}
        self.repair_worker_id = f"dataserver-repair:{uuid.uuid4().hex}"
        self._next_repair_poll_ts: float = time.time() + 1.0
        self._next_repair_cleanup_ts: float = time.time() + 1.0

        # --- object GC queue ---
        init_object_gc_schema()
        self._next_object_gc_ts: float = time.time() + 2.0

    # ---------------- low-level send helpers ----------------
    def _send_client_json(self, client_id: bytes, obj: Dict[str, Any]) -> None:
        self.client_sock.send_multipart([client_id, b"json", jdump(obj)])

    def _send_node_json(self, node_id: str, obj: Dict[str, Any]) -> None:
        self.node_sock.send_multipart([node_id.encode("utf-8"), b"json", jdump(obj)])

    def _send_node_data(self, node_id: str, parts_wo_identity: List[bytes]) -> None:
        self.node_sock.send_multipart([node_id.encode("utf-8")] + parts_wo_identity)

    # ---------------- auth / nodes ----------------
    def _user_from_access_token(self, token: str) -> Optional[str]:
        if not token:
            return None
        try:
            td = jwt_decode(token, JWT_SECRET)
            return td.sub
        except Exception:
            return None

    def _get_user_country_code(self, user_id: str) -> Optional[str]:
        with db_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT country_code FROM users WHERE user_id=%s", (user_id,))
                row = cur.fetchone()
                if not row:
                    return None
                country_code = row.get("country_code")
                return str(country_code) if country_code else None

    def _online_nodes(self, country_code: Optional[str] = None) -> List[Tuple[str, int, int]]:
        with db_conn() as conn:
            with conn.cursor() as cur:
                if country_code:
                    cur.execute(
                        """
                        SELECT n.node_id, n.capacity_bytes, n.reserved_bytes
                        FROM nodes n
                        JOIN node_profiles np ON np.node_id = n.node_id
                        JOIN users u ON u.user_id = np.owner_user_id
                        WHERE n.last_seen >= %s
                          AND COALESCE(n.placement_paused,FALSE)=FALSE
                          AND u.country_code=%s
                        """,
                        (now_ts() - NODE_ONLINE_WINDOW_SEC, country_code),
                    )
                else:
                    cur.execute(
                        """SELECT node_id, capacity_bytes, reserved_bytes
                           FROM nodes
                           WHERE last_seen >= %s
                             AND COALESCE(placement_paused,FALSE)=FALSE""",
                        (now_ts() - NODE_ONLINE_WINDOW_SEC,)
                    )
                rows = cur.fetchall()
                return [(s(r[0]), int(r[1]), int(r[2])) for r in rows]

    def _select_3_nodes(self, file_size: int, country_code: Optional[str] = None) -> Optional[List[str]]:
        """3レプリカ用のノード選定。
        方針（ユーザー要件）:
          - まずアップロード側ユーザーと同じ country_code のノードに限定する
          - そのうえで提供リソース量（capacity_bytes）が大きい順を優先
          - ただし実際に書けるかは free = capacity - reserved で判定
        """
        nodes = self._online_nodes(country_code=country_code)
        # sort by capacity desc, then free desc
        cands = [(nid, cap, cap - resv) for (nid, cap, resv) in nodes]
        cands.sort(key=lambda x: (x[1], x[2]), reverse=True)
        ok = [nid for nid, cap, free in cands if free >= file_size]
        return ok[:3] if len(ok) >= 3 else None

    def _reserve(self, node_ids: List[str], size_bytes: int) -> None:
        with db_conn() as conn:
            with conn.cursor() as cur:
                for nid in node_ids:
                    cur.execute("UPDATE nodes SET reserved_bytes = reserved_bytes + %s WHERE node_id=%s",
                                (int(size_bytes), nid))
            conn.commit()

    def _release(self, node_ids: List[str], size_bytes: int) -> None:
        with db_conn() as conn:
            with conn.cursor() as cur:
                for nid in node_ids:
                    cur.execute(
                        "UPDATE nodes SET reserved_bytes = GREATEST(0, reserved_bytes - %s) WHERE node_id=%s",
                        (int(size_bytes), nid)
                    )
            conn.commit()

    def _mark_committed_replicas_healthy(self, file_object_id: str, node_ids: List[str]) -> None:
        """Record a successful all-node commit check/finalize observation."""
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    if not lock_repair_object(cur, file_object_id=str(file_object_id)):
                        return
                    mark_replicas_healthy(
                        cur,
                        file_object_id=str(file_object_id),
                        node_ids=[str(node_id) for node_id in node_ids],
                        verified=True,
                    )
                conn.commit()
        except Exception as exc:
            # The user upload is already durable at this point.  Health
            # bookkeeping failure must be visible but must not undo it.
            _log_event(
                "ERROR",
                "committed replica health update failed",
                file_object_id=file_object_id,
                node_ids=list(node_ids),
                error=f"{type(exc).__name__}: {exc}",
            )

    def _init_node_heartbeat_stats_schema(self) -> None:
        """ノード heartbeat の月次表示用集計テーブルを作成する。"""
        with db_conn() as conn:
            with conn.cursor() as cur:
                init_node_heartbeat_stats_schema(cur)
            conn.commit()

    def _init_audit_schema(self) -> None:
        """Compatibility entry point retained for older startup wrappers."""
        init_storage_audit_schema()

    def _send_audit_job(self, job: Dict[str, Any]) -> None:
        audit_job_id = str(job["audit_job_id"])
        event_id = str(job.get("current_event_id") or audit_job_id)
        pending = AuditPending(
            event_id=event_id,
            audit_job_id=audit_job_id,
            node_id=str(job["node_id"]),
            file_object_id=str(job["file_object_id"]),
            chunk_id=int(job["chunk_id"]),
            offset=int(job["byte_offset"]),
            length=int(job["length"]),
            expected_hash=str(job["expected_hash"]),
            sent_ts=time.time(),
            purpose=str(job.get("purpose") or "scheduled"),
            repair_job_id=None if not job.get("repair_job_id") else str(job["repair_job_id"]),
        )
        self.audit_pending[event_id] = pending
        try:
            self._send_node_json(
                pending.node_id,
                {
                    "op": "audit_slice",
                    "event_id": event_id,
                    "file_object_id": pending.file_object_id,
                    "chunk_id": pending.chunk_id,
                    "offset": pending.offset,
                    "length": pending.length,
                },
            )
        except Exception as exc:
            self._apply_audit_result(
                event_id,
                "error",
                "",
                0,
                detail=f"audit_send_failed:{type(exc).__name__}",
            )

    def _schedule_one_audit(self) -> None:
        """Persist, claim, and send a bounded batch of due audit challenges."""
        if not STORAGE_AUDIT_ENABLED and not REPLICA_REPAIR_EXECUTION_ENABLED:
            return
        now_wall = time.time()
        if now_wall < self._next_audit_ts:
            return
        self._next_audit_ts = now_wall + max(0.5, AUDIT_SCHEDULE_INTERVAL_SEC)
        available = max(0, max(1, AUDIT_MAX_INFLIGHT) - len(self.audit_pending))
        if available <= 0:
            return

        jobs: List[Dict[str, Any]] = []
        try:
            with db_conn() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    recover_stale_audit_jobs(
                        cur,
                        exclude_event_ids=list(self.audit_pending),
                    )
                    if STORAGE_AUDIT_ENABLED:
                        schedule_due_audit_jobs(
                            cur,
                            due_before=int(now_ts()) - max(1, AUDIT_TARGET_AGE_SEC),
                            online_after=int(now_ts()) - NODE_ONLINE_WINDOW_SEC,
                            limit=min(available, max(1, AUDIT_SCHEDULE_BATCH)),
                        )
                    jobs = claim_due_audit_jobs(
                        cur,
                        worker_id=self.audit_worker_id,
                        limit=min(available, max(1, AUDIT_SCHEDULE_BATCH)),
                        include_scheduled=STORAGE_AUDIT_ENABLED,
                    )
                conn.commit()
        except Exception as exc:
            _log_event("ERROR", "audit scheduling failed", error=f"{type(exc).__name__}: {exc}")
            return

        for job in jobs:
            self._send_audit_job(job)

    def _apply_audit_result(
        self,
        event_id: str,
        status: str,
        got_hash: str,
        latency_ms: int,
        *,
        detail: Optional[str] = None,
    ) -> None:
        """Apply one response idempotently and connect failures to repair."""
        pending = self.audit_pending.pop(str(event_id), None)
        repair_action: Optional[str] = None
        repair_job_id: Optional[str] = pending.repair_job_id if pending else None
        result: Dict[str, Any] = {}
        try:
            with db_conn() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    result = complete_audit_job(
                        cur,
                        audit_job_id=str(pending.audit_job_id if pending else event_id),
                        event_id=str(event_id),
                        outcome=str(status),
                        got_hash=str(got_hash or ""),
                        latency_ms=max(0, int(latency_ms or 0)),
                    )
                    if result.get("repair_needed") and REPLICA_REPAIR_QUEUE_ENABLED:
                        enqueue_repair_job(
                            cur,
                            file_object_id=str(result["file_object_id"]),
                            reason=f"audit_{result.get('outcome') or 'failed'}",
                        )

                    if result.get("purpose") == "repair_verify" and result.get("repair_job_id"):
                        repair_job_id = str(result["repair_job_id"])
                        if result.get("outcome") == "ok" and result.get("status") == "completed":
                            completed = complete_repair_job(
                                cur,
                                repair_job_id=repair_job_id,
                                target_node_id=str(result["node_id"]),
                            )
                            if completed.get("status") == "canceled" and completed.get("reason") in {
                                "target_already_satisfied",
                                "no_safe_retirement_candidate",
                            }:
                                repair_action = "superseded"
                            elif completed.get("applied") or completed.get("reason") == "already_completed":
                                repair_action = "completed"
                            else:
                                schedule_repair_retry(
                                    cur,
                                    repair_job_id=repair_job_id,
                                    error_code="verification_publish_failed",
                                    detail=str(completed.get("reason") or detail or "unknown"),
                                )
                                repair_action = "retry"
                        elif result.get("terminal"):
                            schedule_repair_retry(
                                cur,
                                repair_job_id=repair_job_id,
                                error_code=f"verification_{result.get('outcome') or 'failed'}",
                                detail=detail,
                            )
                            repair_action = "retry"
                conn.commit()
        except Exception as exc:
            _log_event(
                "ERROR",
                "audit result persistence failed",
                audit_job_id=str(pending.audit_job_id if pending else ""),
                event_id=str(event_id),
                error=f"{type(exc).__name__}: {exc}",
            )
            return

        if repair_job_id and repair_action in {"completed", "retry", "superseded"}:
            ctx = self.repairs.get(repair_job_id)
            if ctx and repair_action == "completed":
                if ctx.current_source_node_id:
                    self._record_repair_metric(
                        ctx,
                        node_id=ctx.current_source_node_id,
                        operation="repair_source",
                        success=True,
                    )
                self._record_repair_metric(
                    ctx,
                    node_id=ctx.target_node_id,
                    operation="repair_target",
                    success=True,
                )
            if ctx and repair_action == "retry":
                self._abort_repair_transport(ctx)
            if ctx and repair_action == "superseded":
                self._abort_repair_transport(ctx)
            if ctx:
                self._discard_repair_context(ctx)
        _log_event(
            "INFO" if result.get("outcome") == "ok" else "WARN",
            "storage audit completed",
            audit_job_id=result.get("audit_job_id"),
            event_id=str(event_id),
            file_object_id=result.get("file_object_id"),
            node_id=result.get("node_id"),
            outcome=result.get("outcome"),
            status=result.get("status"),
            purpose=result.get("purpose"),
        )

    def _node_api_key_valid(self, node_id: str, node_api_key: str) -> bool:
        """node_profiles に保存された API キーと一致する heartbeat だけを受け付ける。"""
        if not REQUIRE_NODE_API_KEY:
            return True
        if not node_id or not node_api_key:
            return False

        with db_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT node_api_key
                    FROM node_profiles
                    WHERE node_id=%s
                    LIMIT 1
                    """,
                    (node_id,),
                )
                row = cur.fetchone()

        expected = str((row or {}).get("node_api_key") or "")
        return bool(expected) and hmac.compare_digest(expected, str(node_api_key))

    def _handle_node_heartbeat(self, node_id_b: bytes, payload: Dict[str, Any]) -> None:
        node_id = s(node_id_b)
        node_api_key = str(payload.get("node_api_key", "") or "")
        ts = int(now_ts())

        if not self._node_api_key_valid(node_id, node_api_key):
            # Avoid flooding logs every three seconds while still making an
            # expired/replaced API key visible during diagnosis.
            last_reject_log = self._heartbeat_reject_last_logged.get(node_id, 0)
            if ts - last_reject_log >= NODE_HEARTBEAT_LOG_INTERVAL_SEC:
                _log_event(
                    "WARN",
                    "heartbeat rejected",
                    node_id=node_id,
                    reason="invalid_or_missing_node_api_key",
                )
                self._heartbeat_reject_last_logged[node_id] = ts
            return

        capacity = int(payload.get("capacity_bytes", 0))
        meta_json = json.dumps(payload.get("meta", payload.get("meta_json", {})), ensure_ascii=False)
        failure_domain = str(payload.get("failure_domain", "") or "").strip() or None
        previous_accepted = self._heartbeat_last_accepted.get(node_id)

        try:
            with db_conn() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        """
                        INSERT INTO nodes(node_id,last_seen,capacity_bytes,reserved_bytes,meta_json,failure_domain)
                        VALUES (%s,%s,%s,COALESCE((SELECT reserved_bytes FROM nodes WHERE node_id=%s),0),%s,%s)
                        ON CONFLICT (node_id) DO UPDATE SET
                          last_seen=EXCLUDED.last_seen,
                          capacity_bytes=EXCLUDED.capacity_bytes,
                          meta_json=EXCLUDED.meta_json,
                          failure_domain=COALESCE(EXCLUDED.failure_domain,nodes.failure_domain)
                        RETURNING reserved_bytes, capacity_bytes
                        """,
                        (node_id, ts, capacity, node_id, meta_json, failure_domain),
                    )
                    row = cur.fetchone() or {}
                    record_node_heartbeat_sample(
                        cur,
                        node_id=node_id,
                        reserved_bytes=int(row.get("reserved_bytes") or 0),
                        capacity_bytes=int(row.get("capacity_bytes") or capacity),
                        ts=ts,
                    )
                conn.commit()
        except Exception as exc:
            _log_event(
                "ERROR",
                "heartbeat database update failed",
                node_id=node_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return

        self._heartbeat_last_accepted[node_id] = ts
        self._heartbeat_reject_last_logged.pop(node_id, None)

        heartbeat_gap_sec = None if previous_accepted is None else max(0, ts - previous_accepted)
        recovered = heartbeat_gap_sec is not None and heartbeat_gap_sec > NODE_ONLINE_WINDOW_SEC
        last_log = self._heartbeat_last_logged.get(node_id, 0)
        should_log = (
            previous_accepted is None
            or recovered
            or ts - last_log >= NODE_HEARTBEAT_LOG_INTERVAL_SEC
        )
        if should_log:
            _log_event(
                "INFO",
                "heartbeat accepted and stored",
                node_id=node_id,
                capacity_bytes=capacity,
                last_seen=ts,
                heartbeat_gap_sec=heartbeat_gap_sec,
                state="recovered" if recovered else ("first_seen" if previous_accepted is None else "online"),
            )
            self._heartbeat_last_logged[node_id] = ts

        # Application-level ACK proves that DataServer received the heartbeat
        # and committed last_seen to PostgreSQL. Older node.py versions safely
        # ignore this unknown operation.
        try:
            self._send_node_json(node_id, {
                "op": "heartbeat_ack",
                "server_ts": ts,
                "last_seen": ts,
            })
        except zmq.ZMQError as exc:
            _log_event(
                "WARN",
                "heartbeat ACK send failed",
                node_id=node_id,
                error=f"{type(exc).__name__}: {exc}",
            )

    # ---------------- upload init (normal / shared upload / shared replace) ----------------
    def _resolve_shared_owner_and_mode(self, *, mode: str, parent_id: Optional[str], target_item_id: Optional[str]) -> Tuple[str, str, Optional[str], Optional[str]]:
        """
        Returns (owner_user_id, shared_mode, shared_parent_id, shared_replace_item_id)
        shared_mode: normal / share_upload / share_replace
        """
        if mode not in ("upload", "replace"):
            raise ValueError("invalid mode")

        with db_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                if mode == "upload":
                    if not parent_id:
                        raise ValueError("parent_id required")
                    cur.execute("SELECT owner_user_id FROM items WHERE item_id=%s", (str(parent_id),))
                    r = cur.fetchone()
                    if not r:
                        raise ValueError("share upload parent not found")
                    return str(r["owner_user_id"]), "share_upload", str(parent_id), None
                else:
                    if not target_item_id:
                        raise ValueError("target_item_id required")
                    cur.execute("SELECT owner_user_id FROM items WHERE item_id=%s", (str(target_item_id),))
                    r = cur.fetchone()
                    if not r:
                        raise ValueError("share replace target not found")
                    return str(r["owner_user_id"]), "share_replace", None, str(target_item_id)

    def _plan_multipart(self, total_size: int, country_code: Optional[str] = None) -> Optional[List[Tuple[int, List[str]]]]:
        """multipart用に (part_size, replica_node_ids) の列を作る。
        3レプリカを維持しつつ、提供容量が小さいノードでも総量が足りればアップロード可能にする。
        さらに、アップロード側ユーザーと同じ country_code のノードだけを候補にする。
        Greedy:
          - capacityが大きい順に並べたノード集合から、
            その時点で free が大きい上位3ノードを選び、
            part_size = min(remaining, min(free_of_3)) を割り当てる。
        """
        nodes = self._online_nodes(country_code=country_code)
        # candidates sorted by capacity desc
        cands = [(nid, cap, cap - resv) for (nid, cap, resv) in nodes]
        cands = [c for c in cands if c[2] > 0]
        cands.sort(key=lambda x: x[1], reverse=True)

        remaining = total_size
        plan: List[Tuple[int, List[str]]] = []

        # We will simulate reservations locally to plan parts.
        free_map = {nid: free for (nid, cap, free) in cands}
        while remaining > 0:
            # pick 3 nodes with highest free, but only among top-by-capacity ordering
            # (capacity order used for tie/priority; free order for actual fit)
            sorted_by_free = sorted(cands, key=lambda x: (free_map.get(x[0], 0), x[1]), reverse=True)
            top3 = [x[0] for x in sorted_by_free if free_map.get(x[0], 0) > 0][:3]
            if len(top3) < 3:
                return None
            part_cap = min(free_map[top3[0]], free_map[top3[1]], free_map[top3[2]])
            if part_cap <= 0:
                return None
            part_size = int(min(remaining, part_cap))
            plan.append((part_size, top3))
            for nid in top3:
                free_map[nid] -= part_size
            remaining -= part_size
        return plan

    def _client_init_upload(self, client_id: bytes, msg: Dict[str, Any]) -> None:
        access_token = str(msg.get("access_token", ""))
        user_id = str(msg.get("user_id", ""))  # 開発用途 fallback
        if access_token:
            uid = self._user_from_access_token(access_token)
            if not uid:
                self._send_client_json(client_id, {"status": "error", "message": "invalid access_token"})
                return
            user_id = uid
        elif REQUIRE_UPLOAD_AUTH:
            self._send_client_json(client_id, {"status": "error", "message": "access_token required"})
            return

        file_name = str(msg.get("file_name", ""))
        file_size = int(msg.get("file_size", 0))
        chunk_size = int(msg.get("chunk_size", CHUNK_SIZE_DEFAULT))
        if not user_id or not file_name or file_size <= 0 or chunk_size <= 0:
            self._send_client_json(client_id, {"status": "error", "message": "init_upload args invalid"})
            return

        uploader_user_id = user_id
        uploader_country_code = self._get_user_country_code(uploader_user_id)
        if not uploader_country_code:
            self._send_client_json(client_id, {"status": "error", "message": "uploader country_code not found"})
            return

        shared_mode = "normal"
        shared_parent_id = None
        shared_replace_item_id = None
        owner_user_id = user_id

        share_token = str(msg.get("share_token", "") or "")
        if share_token:
            mode = str(msg.get("mode", "upload"))
            try:
                owner_user_id, shared_mode, shared_parent_id, shared_replace_item_id = self._resolve_shared_owner_and_mode(
                    mode=mode,
                    parent_id=msg.get("parent_id"),
                    target_item_id=msg.get("target_item_id"),
                )
            except Exception as e:
                self._send_client_json(client_id, {"status": "error", "message": f"share upload resolve failed: {e}"})
                return

        # まず単一オブジェクト（従来方式）で収まるか試す
        node_ids = self._select_3_nodes(file_size, uploader_country_code)
        if node_ids:
            session_id = str(uuid.uuid4())
            file_object_id = str(uuid.uuid4())
            created = now_ts()
            expires_session = created + SESSION_TTL_SEC

            self._reserve(node_ids, file_size)

            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO objects(file_object_id,owner_user_id,size_bytes,chunk_size,created_at) VALUES (%s,%s,%s,%s,%s)",
                        (file_object_id, owner_user_id, file_size, chunk_size, created)
                    )
                    for nid in node_ids:
                        cur.execute(
                            "INSERT INTO replicas(file_object_id,node_id,created_at) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                            (file_object_id, nid, created)
                        )
                    cur.execute(
                        """
                        INSERT INTO upload_sessions(
                            session_id,file_object_id,user_id,file_name,file_size,chunk_size,node_ids,status,expires_at,created_at,
                            shared_mode,shared_parent_id,shared_replace_item_id
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            session_id, file_object_id, owner_user_id, file_name, file_size, chunk_size, ",".join(node_ids),
                            "UPLOADING", expires_session, created,
                            shared_mode, shared_parent_id, shared_replace_item_id,
                        )
                    )
                conn.commit()

            self.uploads[session_id] = UploadSessionCache(
                owner_user_id=owner_user_id,
                node_ids=node_ids,
                file_name=file_name,
                file_size=file_size,
                chunk_size=chunk_size,
                file_object_id=file_object_id,
                shared_mode=shared_mode,
                shared_parent_id=shared_parent_id,
                shared_replace_item_id=shared_replace_item_id,
            )

            for nid in node_ids:
                self._send_node_json(nid, {
                    "op": "init_session",
                    "session_id": session_id,
                    "file_object_id": file_object_id,
                    "file_name": file_name,
                    "file_size": file_size,
                    "chunk_size": chunk_size,
                    "expires_at": expires_session,
                })

            self._send_client_json(client_id, {
                "status": "ready",
                "session_id": session_id,
                "file_object_id": file_object_id,
                "chunk_size": chunk_size,
                "replicas": node_ids,
                "expires_at": expires_session,
                "mode": shared_mode,
            })
            return

        # 収まらない場合は multipart へ
        plan = self._plan_multipart(file_size, uploader_country_code)
        if not plan:
            self._send_client_json(client_id, {"status": "error", "message": f"同じ国({uploader_country_code})の中で、3レプリカ前提の分割配置ができるノードが足りません"})
            return

        upload_id = str(uuid.uuid4())
        created = now_ts()
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO multipart_uploads(upload_id,owner_user_id,file_name,total_size,chunk_size,status,created_at,finalized_item_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,NULL)",
                    (upload_id, owner_user_id, file_name, file_size, chunk_size, "UPLOADING", created)
                )
            conn.commit()

        parts_out: List[Dict[str, Any]] = []
        offset = 0
        for idx, (part_size, replica_nodes) in enumerate(plan):
            session_id = str(uuid.uuid4())
            file_object_id = str(uuid.uuid4())
            expires_session = created + SESSION_TTL_SEC
            part_name = f"{file_name}.part{idx:04d}"

            self._reserve(replica_nodes, part_size)

            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO objects(file_object_id,owner_user_id,size_bytes,chunk_size,created_at) VALUES (%s,%s,%s,%s,%s)",
                        (file_object_id, owner_user_id, int(part_size), chunk_size, created)
                    )
                    for nid in replica_nodes:
                        cur.execute(
                            "INSERT INTO replicas(file_object_id,node_id,created_at) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                            (file_object_id, nid, created)
                        )
                    cur.execute(
                        """
                        INSERT INTO upload_sessions(
                            session_id,file_object_id,user_id,file_name,file_size,chunk_size,node_ids,status,expires_at,created_at,
                            shared_mode,shared_parent_id,shared_replace_item_id
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            session_id, file_object_id, owner_user_id, part_name, int(part_size), chunk_size, ",".join(replica_nodes),
                            "UPLOADING", expires_session, created,
                            shared_mode, shared_parent_id, shared_replace_item_id,
                        )
                    )
                    cur.execute(
                        "INSERT INTO multipart_parts(upload_id,part_index,session_id,file_object_id,part_offset,part_size,node_ids,status) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (upload_id, idx, session_id, file_object_id, int(offset), int(part_size), ",".join(replica_nodes), "UPLOADING")
                    )
                conn.commit()

            self.uploads[session_id] = UploadSessionCache(
                owner_user_id=owner_user_id,
                node_ids=replica_nodes,
                file_name=part_name,
                file_size=int(part_size),
                chunk_size=chunk_size,
                file_object_id=file_object_id,
                shared_mode=shared_mode,
                shared_parent_id=shared_parent_id,
                shared_replace_item_id=shared_replace_item_id,
            )

            for nid in replica_nodes:
                self._send_node_json(nid, {
                    "op": "init_session",
                    "session_id": session_id,
                    "file_object_id": file_object_id,
                    "file_name": part_name,
                    "file_size": int(part_size),
                    "chunk_size": chunk_size,
                    "expires_at": expires_session,
                })

            parts_out.append({
                "part_index": idx,
                "offset": int(offset),
                "part_size": int(part_size),
                "session_id": session_id,
                "file_object_id": file_object_id,
                "replicas": replica_nodes,
            })
            offset += int(part_size)

        self._send_client_json(client_id, {
            "status": "ready_multipart",
            "upload_id": upload_id,
            "file_name": file_name,
            "file_size": file_size,
            "chunk_size": chunk_size,
            "parts": parts_out,
            "mode": shared_mode,
        })

    def _client_chunk(self, client_id: bytes, frames: List[bytes]) -> None:
        # frames after removing identity => [b"data", session_id, chunk_id, hash, blob]
        if len(frames) != 5:
            self._send_client_json(client_id, {"status": "error", "message": "data frame invalid"})
            return
        _, session_id_b, _, _, _ = frames
        session_id = s(session_id_b)
        up = self.uploads.get(session_id)
        if not up:
            self._send_client_json(client_id, {"status": "error", "message": "unknown session"})
            return

        # --- audit slices (ciphertext spot-check tags) ---
        try:
            file_object_id = up.file_object_id
            chunk_id = int(s(frames[2]))
            blob = frames[4]
            configured_slice_len = max(1, int(os.environ.get("AUDIT_SLICE_LEN", "1024")))
            SLICE_N = int(os.environ.get("AUDIT_SLICES_PER_CHUNK", "3"))
            slice_len = min(configured_slice_len, len(blob))
            if slice_len > 0:
                with db_conn() as conn:
                    with conn.cursor() as cur:
                        for i in range(SLICE_N):
                            off = 0 if len(blob) == slice_len else (int.from_bytes(os.urandom(4), "big") % (len(blob) - slice_len + 1))
                            seg = blob[off:off+slice_len]
                            h = sha256_hex(seg)
                            cur.execute(
                                """
                                INSERT INTO chunk_audit_slices(file_object_id,chunk_id,slice_index,byte_offset,length,hash_hex,created_at)
                                VALUES (%s,%s,%s,%s,%s,%s,%s)
                                ON CONFLICT (file_object_id,chunk_id,slice_index) DO NOTHING
                                """,
                                (file_object_id, chunk_id, i, int(off), int(slice_len), h, now_ts())
                            )
                    conn.commit()
        except Exception as exc:
            _log_event(
                "ERROR",
                "audit slice persistence failed",
                session_id=session_id,
                file_object_id=up.file_object_id,
                error=f"{type(exc).__name__}: {exc}",
            )

        # 3ノードへ転送
        for nid in up.node_ids:
            self._send_node_data(nid, frames)

        # プロトタイプ: いずれか1ノードのACKを待つ。
        # 待機中に届いた heartbeat / stream / GC reply は必ず再ディスパッチする。
        deadline = time.time() + 2.0
        while time.time() < deadline:
            socks = dict(self.poller.poll(timeout=50))
            if self.node_sock not in socks:
                continue

            nframes = self.node_sock.recv_multipart()
            if len(nframes) >= 3 and nframes[1] == b"json":
                try:
                    payload = jload(nframes[2])
                except Exception:
                    self._dispatch_node_frames(nframes)
                    continue
                if (
                    payload.get("op") == "chunk_ack"
                    and payload.get("session_id") == session_id
                    and payload.get("status") == "ack"
                ):
                    self._send_client_json(client_id, {"status": "ack"})
                    return

            self._dispatch_node_frames(nframes)

        self._send_client_json(client_id, {"status": "error", "message": "chunk ack timeout"})

    def _client_commit(self, client_id: bytes, session_id: str) -> None:
        with db_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT session_id,file_object_id,user_id,file_name,file_size,chunk_size,node_ids,status,
                           shared_mode,shared_parent_id,shared_replace_item_id
                    FROM upload_sessions WHERE session_id=%s
                    """,
                    (session_id,)
                )
                row = cur.fetchone()
        if not row:
            self._send_client_json(client_id, {"status": "error", "message": "session not found"})
            return

        file_object_id = str(row["file_object_id"])
        owner_user_id = str(row["user_id"])
        file_name = str(row["file_name"])
        file_size = int(row["file_size"])
        chunk_size = int(row["chunk_size"])
        node_ids = [x for x in str(row["node_ids"]).split(",") if x]
        shared_mode = str(row["shared_mode"] or "normal")
        shared_parent_id = row["shared_parent_id"]
        shared_replace_item_id = row["shared_replace_item_id"]

        # Zero-byte objects have no data frame from which to derive a slice.
        # A trusted metadata tag lets the same audit/repair pipeline verify
        # that the node finalized the expected empty object.
        if file_size == 0:
            try:
                metadata_hash = sha256_hex(f"meta:0:{chunk_size}:0".encode("utf-8"))
                with db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO chunk_audit_slices(
                                file_object_id,chunk_id,slice_index,byte_offset,length,hash_hex,created_at
                            ) VALUES (%s,-1,0,0,0,%s,%s)
                            ON CONFLICT (file_object_id,chunk_id,slice_index) DO NOTHING
                            """,
                            (file_object_id, metadata_hash, int(now_ts())),
                        )
                    conn.commit()
            except Exception as exc:
                _log_event(
                    "ERROR",
                    "empty-object audit tag persistence failed",
                    file_object_id=file_object_id,
                    error=f"{type(exc).__name__}: {exc}",
                )

        # multipartパートかどうか判定（multipart_parts に session_id があればパート）
        mp_info = None
        with db_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT upload_id, part_index, part_offset, part_size FROM multipart_parts WHERE session_id=%s",
                    (session_id,)
                )
                mp_info = cur.fetchone()

        # commit_check
        for nid in node_ids:
            self._send_node_json(nid, {
                "op": "commit_check",
                "session_id": session_id,
                "file_size": file_size,
                "chunk_size": chunk_size,
            })

        want = set(node_ids)
        replies: Dict[str, Dict[str, Any]] = {}
        deadline = time.time() + 3.0
        while time.time() < deadline and len(replies) < len(want):
            socks = dict(self.poller.poll(timeout=50))
            if self.node_sock not in socks:
                continue
            nframes = self.node_sock.recv_multipart()
            handled_as_expected_reply = False
            if len(nframes) >= 3 and nframes[1] == b"json":
                try:
                    nid = s(nframes[0])
                    payload = jload(nframes[2])
                except Exception:
                    payload = {}
                    nid = ""
                if (
                    nid in want
                    and payload.get("op") == "commit_check_reply"
                    and payload.get("session_id") == session_id
                ):
                    replies[nid] = payload
                    handled_as_expected_reply = True
            if not handled_as_expected_reply:
                self._dispatch_node_frames(nframes)

        if len(replies) != len(want):
            self._send_client_json(client_id, {"status": "error", "message": "commit_check応答不足"})
            return

        union_missing: Set[int] = set()
        for r in replies.values():
            union_missing |= set(int(x) for x in (r.get("missing") or []))

        if union_missing:
            self._send_client_json(client_id, {"status": "incomplete", "session_id": session_id, "resend_list": sorted(union_missing)})
            return

        # finalize
        for nid in node_ids:
            self._send_node_json(nid, {"op": "commit_finalize", "session_id": session_id})

        fin: Dict[str, Dict[str, Any]] = {}
        deadline = time.time() + 3.0
        while time.time() < deadline and len(fin) < len(want):
            socks = dict(self.poller.poll(timeout=50))
            if self.node_sock not in socks:
                continue
            nframes = self.node_sock.recv_multipart()
            handled_as_expected_reply = False
            if len(nframes) >= 3 and nframes[1] == b"json":
                try:
                    nid = s(nframes[0])
                    payload = jload(nframes[2])
                except Exception:
                    payload = {}
                    nid = ""
                if (
                    nid in want
                    and payload.get("op") == "commit_finalize_reply"
                    and payload.get("session_id") == session_id
                ):
                    fin[nid] = payload
                    handled_as_expected_reply = True
            if not handled_as_expected_reply:
                self._dispatch_node_frames(nframes)

        if len(fin) != len(want) or any(p.get("status") != "ok" for p in fin.values()):
            self._release(node_ids, file_size)
            for nid in node_ids:
                self._send_node_json(nid, {"op": "delete_object", "file_object_id": file_object_id})
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE upload_sessions SET status=%s WHERE session_id=%s", ("ABORTED", session_id))
                conn.commit()
            self._send_client_json(client_id, {"status": "error", "message": "commit_finalize失敗"})
            return

        self._mark_committed_replicas_healthy(file_object_id, node_ids)
        now = now_ts()

        # multipartパートの場合は、ここでは論理itemを作らない（最後に commit_multipart で統合）
        if mp_info:
            upload_id = str(mp_info["upload_id"])
            part_index = int(mp_info["part_index"])
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE upload_sessions SET status=%s WHERE session_id=%s", ("COMMITTED", session_id))
                    cur.execute("UPDATE multipart_parts SET status=%s WHERE upload_id=%s AND part_index=%s",
                                ("COMMITTED", upload_id, part_index))
                conn.commit()

            # usage metering (billing): commit確定時のみ ingress課金 + 寿命開始
            ensure_object_lifetime_started(file_object_id, owner_user_id, file_size, start_ts=now)
            record_transfer_event(owner_user_id, "ingress", file_size, ts=now, file_object_id=file_object_id, is_shared=False)

            self._send_client_json(client_id, {
                "status": "part_uploaded",
                "upload_id": upload_id,
                "part_index": part_index,
                "session_id": session_id,
                "file_object_id": file_object_id,
                "replicas": node_ids,
            })
            return
        item_id = str(uuid.uuid4())
        resp_status = "uploaded"

        with db_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("UPDATE upload_sessions SET status=%s WHERE session_id=%s", ("COMMITTED", session_id))

                if shared_mode == "share_upload":
                    parent_id = str(shared_parent_id or ROOT_ID)
                    cur.execute(
                        """
                        INSERT INTO items(item_id,type,parent_id,name,size_bytes,file_object_id,created_at,updated_at,trashed_at,trash_batch_id,owner_user_id)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL,NULL,%s)
                        """,
                        (item_id, "file", parent_id, file_name, file_size, file_object_id, now, now, owner_user_id)
                    )

                elif shared_mode == "share_replace":
                    if not shared_replace_item_id:
                        conn.rollback()
                        self._send_client_json(client_id, {"status": "error", "message": "replace target missing"})
                        return
                    cur.execute("SELECT file_object_id FROM items WHERE item_id=%s", (str(shared_replace_item_id),))
                    old = cur.fetchone()
                    old_oid = str(old["file_object_id"]) if old and old["file_object_id"] else None
                    cur.execute(
                        "UPDATE items SET file_object_id=%s, name=%s, size_bytes=%s, updated_at=%s WHERE item_id=%s",
                        (file_object_id, file_name, file_size, now, str(shared_replace_item_id))
                    )
                    item_id = str(shared_replace_item_id)
                    resp_status = "replaced"

                    # The shared GC path locks and re-checks the old object,
                    # then queues deletion for its actual replica nodes.
                    if old_oid:
                        gc_unreferenced_objects(cur, [old_oid], reason="share_replace")
                else:
                    cur.execute(
                        """
                        INSERT INTO items(item_id,type,parent_id,name,size_bytes,file_object_id,created_at,updated_at,trashed_at,trash_batch_id,owner_user_id)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL,NULL,%s)
                        """,
                        (item_id, "file", ROOT_ID, file_name, file_size, file_object_id, now, now, owner_user_id)
                    )

            conn.commit()

        # ---- usage metering (billing): commit確定時のみ ingress課金 ----
        ensure_object_lifetime_started(file_object_id, owner_user_id, file_size, start_ts=now)
        record_transfer_event(owner_user_id, "ingress", file_size, ts=now, file_object_id=file_object_id, is_shared=(shared_mode != "normal"))

        self._send_client_json(client_id, {
            "status": resp_status,
            "session_id": session_id,
            "file_object_id": file_object_id,
            "item_id": item_id,
            "replicas": node_ids,
        })

    # ---------------- download (shared cap / actual metering) ----------------

    def _client_commit_multipart(self, client_id: bytes, msg: Dict[str, Any]) -> None:
        """multipartアップロードを論理ファイルとして確定する。
        - すべてのパートが COMMITTED であることを確認
        - items に論理ファイル（file_object_id=NULL）を作成
        - item_parts に part_index順で file_object_id を登録
        - user_storage_allocations に論理サイズを加算（借用量の記録）
        """
        upload_id = str(msg.get("upload_id", "") or "")
        if not upload_id:
            self._send_client_json(client_id, {"status": "error", "message": "upload_id required"})
            return

        with db_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT owner_user_id,file_name,total_size,chunk_size,status FROM multipart_uploads WHERE upload_id=%s",
                            (upload_id,))
                up = cur.fetchone()
                if not up:
                    self._send_client_json(client_id, {"status": "error", "message": "multipart upload not found"})
                    return
                if str(up["status"]) == "FINALIZED":
                    self._send_client_json(client_id, {"status": "already_finalized", "upload_id": upload_id, "item_id": up.get("finalized_item_id")})
                    return

                cur.execute("SELECT part_index,file_object_id,part_offset,part_size,status FROM multipart_parts WHERE upload_id=%s ORDER BY part_index ASC",
                            (upload_id,))
                parts = cur.fetchall()
                if not parts:
                    self._send_client_json(client_id, {"status": "error", "message": "no parts"})
                    return
                if any(str(p["status"]) != "COMMITTED" for p in parts):
                    self._send_client_json(client_id, {"status": "error", "message": "not all parts committed"})
                    return

                now = now_ts()
                item_id = str(uuid.uuid4())

                cur.execute(
                    """
                    INSERT INTO items(item_id,type,parent_id,name,size_bytes,file_object_id,created_at,updated_at,trashed_at,trash_batch_id,owner_user_id)
                    VALUES (%s,%s,%s,%s,%s,NULL,%s,%s,NULL,NULL,%s)
                    """,
                    (item_id, "file", ROOT_ID, str(up["file_name"]), int(up["total_size"]), now, now, str(up["owner_user_id"]))
                )

                for p in parts:
                    cur.execute(
                        "INSERT INTO item_parts(item_id,part_index,file_object_id,part_offset,part_size) VALUES (%s,%s,%s,%s,%s)",
                        (item_id, int(p["part_index"]), str(p["file_object_id"]), int(p["part_offset"]), int(p["part_size"]))
                    )

                cur.execute("UPDATE multipart_uploads SET status=%s, finalized_item_id=%s WHERE upload_id=%s",
                            ("FINALIZED", item_id, upload_id))

                # 借用量の記録（論理サイズ）
                cur.execute("SELECT allocated_bytes FROM user_storage_allocations WHERE user_id=%s", (str(up["owner_user_id"]),))
                r = cur.fetchone()
                if r:
                    cur.execute("UPDATE user_storage_allocations SET allocated_bytes=allocated_bytes+%s, updated_at=%s WHERE user_id=%s",
                                (int(up["total_size"]), now, str(up["owner_user_id"])))
                else:
                    cur.execute("INSERT INTO user_storage_allocations(user_id,allocated_bytes,updated_at) VALUES (%s,%s,%s)",
                                (str(up["owner_user_id"]), int(up["total_size"]), now))

            conn.commit()

        self._send_client_json(client_id, {"status": "uploaded", "upload_id": upload_id, "item_id": item_id})
    def _download_candidate_node_ids(self, file_object_id: str) -> List[str]:
        """Return ordered, usable replicas without using a reliability score."""
        with db_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                rows = fetch_download_candidates(
                    cur,
                    file_object_id=str(file_object_id),
                    online_after=int(now_ts()) - NODE_ONLINE_WINDOW_SEC,
                    limit=max(1, DOWNLOAD_MAX_REPLICA_ATTEMPTS),
                )
        return [str(row["node_id"]) for row in rows]

    def _record_download_attempt(
        self,
        ctx: TransferCtx,
        *,
        success: bool,
        error_code: Optional[str] = None,
        replica_status: str = "suspect",
        verified_failure: bool = False,
    ) -> None:
        """Persist one node attempt without allowing metrics failure to stop a download."""
        state = ctx.failover
        node_id = str(state.current_node_id or "")
        if not node_id:
            return
        latency_ms = max(0, int((time.monotonic() - state.attempt_started_ts) * 1000.0))
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    if not lock_repair_object(cur, file_object_id=str(ctx.file_object_id)):
                        return
                    if success:
                        mark_replica_healthy(
                            cur,
                            file_object_id=ctx.file_object_id,
                            node_id=node_id,
                            verified=True,
                        )
                    else:
                        mark_replica_failure(
                            cur,
                            file_object_id=ctx.file_object_id,
                            node_id=node_id,
                            status=replica_status,
                            error=str(error_code or "download_attempt_failed"),
                            verified_failure=verified_failure,
                        )
                    record_node_transfer_metric(
                        cur,
                        node_id=node_id,
                        file_object_id=ctx.file_object_id,
                        transfer_id=ctx.transfer_id,
                        operation="download",
                        success=bool(success),
                        bytes_count=state.attempt_bytes,
                        latency_ms=latency_ms,
                        error_code=None if success else str(error_code or "download_attempt_failed"),
                    )
                conn.commit()
        except Exception as exc:
            _log_event(
                "ERROR",
                "download attempt health update failed",
                transfer_id=ctx.transfer_id,
                file_object_id=ctx.file_object_id,
                node_id=node_id,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _remove_active_node_attempt(self, ctx: TransferCtx) -> None:
        node_transfer_id = ctx.failover.current_node_transfer_id
        if node_transfer_id:
            self.node_transfer_index.pop(str(node_transfer_id), None)

    def _finish_download_transfer(self, ctx: TransferCtx) -> None:
        self._remove_active_node_attempt(ctx)
        self.transfers.pop(ctx.transfer_id, None)

    def _start_next_download_attempt(
        self,
        ctx: TransferCtx,
        *,
        previous_error: Optional[str] = None,
        previous_status: str = "suspect",
        verified_failure: bool = False,
    ) -> bool:
        """Fail the active attempt if needed and start the next replica."""
        if previous_error and ctx.failover.current_node_id:
            self._record_download_attempt(
                ctx,
                success=False,
                error_code=previous_error,
                replica_status=previous_status,
                verified_failure=verified_failure,
            )
            self._remove_active_node_attempt(ctx)

        while True:
            next_attempt = ctx.failover.begin_next_attempt()
            if next_attempt is None:
                self._flush_transfer_meter(ctx)
                self._send_client_json(
                    ctx.client_id,
                    {
                        "status": "error",
                        "message": "all replicas failed",
                        "error_code": "all_replicas_failed",
                        "transfer_id": ctx.transfer_id,
                        "attempted_nodes": list(ctx.failover.attempted_node_ids),
                        "missing": ctx.failover.global_missing(),
                    },
                )
                _log_event(
                    "ERROR",
                    "download failed on all replicas",
                    transfer_id=ctx.transfer_id,
                    file_object_id=ctx.file_object_id,
                    attempted_nodes=list(ctx.failover.attempted_node_ids),
                    received_chunk_count=len(ctx.failover.got),
                    total_chunks=ctx.total_chunks,
                )
                self._finish_download_transfer(ctx)
                return False

            node_id, node_transfer_id = next_attempt
            self.node_transfer_index[node_transfer_id] = ctx.transfer_id
            try:
                self._send_node_json(
                    node_id,
                    {
                        "op": "stream_object_begin",
                        "transfer_id": node_transfer_id,
                        "file_object_id": ctx.file_object_id,
                    },
                )
            except Exception as exc:
                send_error = f"node_send_failed:{type(exc).__name__}"
                self._record_download_attempt(
                    ctx,
                    success=False,
                    error_code=send_error,
                    replica_status="suspect",
                    verified_failure=False,
                )
                self._remove_active_node_attempt(ctx)
                continue

            if ctx.client_ready_sent and ctx.failover.candidate_index > 0:
                self._send_client_json(
                    ctx.client_id,
                    {
                        "status": "retrying",
                        "transfer_id": ctx.transfer_id,
                        "attempt": len(ctx.failover.attempted_node_ids),
                        "max_attempts": ctx.failover.max_attempts,
                    },
                )
            _log_event(
                "INFO",
                "download replica attempt started",
                transfer_id=ctx.transfer_id,
                file_object_id=ctx.file_object_id,
                node_id=node_id,
                attempt=len(ctx.failover.attempted_node_ids),
                max_attempts=ctx.failover.max_attempts,
            )
            return True

    def _process_download_timeouts(self) -> None:
        """Switch replicas when the active node stops producing control or data frames."""
        for ctx in list(self.transfers.values()):
            if ctx.aborted_by_cap or not ctx.failover.current_node_id:
                continue
            if not ctx.failover.timed_out(DOWNLOAD_NODE_TIMEOUT_SEC):
                continue
            self._start_next_download_attempt(
                ctx,
                previous_error="node_response_timeout",
                previous_status="suspect",
                verified_failure=False,
            )

    def _client_download_begin(self, client_id: bytes, msg: Dict[str, Any]) -> None:
        token = str(msg.get("token", ""))
        now = now_ts()
        with db_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                # v2スキーマ想定（charge_user_id, is_shared）
                cur.execute(
                    """
                    SELECT file_object_id, owner_user_id, charge_user_id, is_shared, expires_at
                    FROM download_tokens WHERE token=%s
                    """,
                    (token,)
                )
                t = cur.fetchone()
                if not t or int(t["expires_at"]) < now:
                    self._send_client_json(client_id, {"status": "error", "message": "token invalid"})
                    return

                file_object_id = str(t["file_object_id"])
                owner_user_id = str(t["owner_user_id"])
                charge_user_id = str(t["charge_user_id"] or owner_user_id)
                is_shared = bool(t["is_shared"])

                cur.execute("SELECT size_bytes, chunk_size FROM objects WHERE file_object_id=%s", (file_object_id,))
                o = cur.fetchone()
                if not o:
                    self._send_client_json(client_id, {"status": "error", "message": "object not found"})
                    return
                size_bytes = int(o["size_bytes"])
                chunk_size = int(o["chunk_size"])
                total_chunks = (size_bytes + chunk_size - 1) // chunk_size

        candidates = self._download_candidate_node_ids(file_object_id)
        if not candidates:
            self._send_client_json(
                client_id,
                {"status": "error", "message": "no usable replicas", "error_code": "no_usable_replicas"},
            )
            return

        transfer_id = uuid.uuid4().hex
        failover = DownloadFailoverState(
            transfer_id=transfer_id,
            candidate_node_ids=candidates,
            total_chunks=total_chunks,
            max_attempts=min(len(candidates), max(1, DOWNLOAD_MAX_REPLICA_ATTEMPTS)),
        )
        transfer_ctx = TransferCtx(
            transfer_id=transfer_id,
            client_id=client_id,
            file_object_id=file_object_id,
            total_chunks=total_chunks,
            charge_user_id=charge_user_id,
            is_shared=is_shared,
            failover=failover,
        )
        self.transfers[transfer_id] = transfer_ctx

        if not self._start_next_download_attempt(transfer_ctx):
            return
        self._send_client_json(client_id, {"status": "ready", "transfer_id": transfer_id, "total_chunks": total_chunks})
        transfer_ctx.client_ready_sent = True

    def _client_download_resend(self, client_id: bytes, msg: Dict[str, Any]) -> None:
        transfer_id = str(msg.get("transfer_id", ""))
        missing = [int(x) for x in (msg.get("missing") or [])]
        ctx = self.transfers.get(transfer_id)
        if not ctx or ctx.client_id != client_id:
            self._send_client_json(client_id, {"status": "error", "message": "unknown transfer"})
            return
        if not missing:
            return
        node_id = ctx.failover.current_node_id
        node_transfer_id = ctx.failover.current_node_transfer_id
        if not node_id or not node_transfer_id:
            self._send_client_json(client_id, {"status": "error", "message": "no active replica", "transfer_id": transfer_id})
            return
        safe_missing = [cid for cid in missing if 0 <= cid < ctx.total_chunks]
        if not safe_missing:
            return
        ctx.failover.touch()
        self._send_node_json(node_id, {
            "op": "stream_object_resend",
            "transfer_id": node_transfer_id,
            "file_object_id": ctx.file_object_id,
            "missing": safe_missing
        })

    def _flush_transfer_meter(self, ctx: TransferCtx) -> None:
        if ctx.bytes_since_flush <= 0:
            return
        record_transfer_event(
            ctx.charge_user_id, "egress", ctx.bytes_since_flush,
            ts=now_ts(), file_object_id=ctx.file_object_id, is_shared=ctx.is_shared
        )
        ctx.bytes_since_flush = 0

    def _handle_node_stream(self, frames: List[bytes]) -> None:
        # [node_id, b"stream", transfer_id, chunk_id, hash, data]
        if len(frames) != 6:
            return
        if s(frames[2]) in getattr(self, "repair_source_index", {}):
            self._handle_repair_source_stream(frames)
            return
        node_id_b, _, tid_b, cid_b, hash_b, data = frames
        node_id = s(node_id_b)
        node_transfer_id = s(tid_b)
        stable_transfer_id = self.node_transfer_index.get(node_transfer_id)
        ctx = self.transfers.get(stable_transfer_id or "")
        if not ctx or not ctx.failover.accepts_frame(node_id=node_id, node_transfer_id=node_transfer_id):
            # A stale attempt may finish after failover.  Its unique node-side
            # id prevents those frames from reaching the client.
            return

        if sha256_hex(data) != s(hash_b):
            ctx.failover.touch()
            ctx.failover.attempt_bytes += len(data)
            self._start_next_download_attempt(
                ctx,
                previous_error="ciphertext_hash_mismatch",
                previous_status="corrupt",
                verified_failure=True,
            )
            return

        try:
            chunk_id = int(s(cid_b))
        except Exception:
            self._start_next_download_attempt(
                ctx,
                previous_error="invalid_chunk_id",
                previous_status="suspect",
                verified_failure=False,
            )
            return

        observation = ctx.failover.observe_chunk(chunk_id, len(data))
        if observation == "invalid":
            self._start_next_download_attempt(
                ctx,
                previous_error="chunk_id_out_of_range",
                previous_status="suspect",
                verified_failure=False,
            )
            return
        if observation == "duplicate":
            # It still proves that the current replica has this chunk, but it
            # must not be delivered or metered twice.
            return

        # 実測ベース上限：送る直前に「このチャンク分」だけ許可されるか判定
        can_send, remaining, limit_b = check_cap_allow_send(
            ctx.charge_user_id,
            is_shared=ctx.is_shared,
            bytes_to_send=len(data),
        )
        if not can_send:
            self._flush_transfer_meter(ctx)
            ctx.aborted_by_cap = True
            self._send_client_json(ctx.client_id, {
                "status": "cap_reached",
                "transfer_id": ctx.transfer_id,
                "remaining_bytes": int(remaining),
                "daily_limit_bytes": int(limit_b) if limit_b is not None else None,
                "is_shared": ctx.is_shared,
            })
            self._finish_download_transfer(ctx)
            return

        # The client always sees the stable id, never a node attempt id.
        self.client_sock.send_multipart([
            ctx.client_id,
            b"stream",
            ctx.transfer_id.encode("utf-8"),
            cid_b,
            hash_b,
            data,
        ])

        ctx.bytes_since_flush += len(data)

        if ctx.bytes_since_flush >= CHUNK_METER_FLUSH_BYTES:
            self._flush_transfer_meter(ctx)

    # ---------------- encrypted replica repair ----------------
    def _discard_repair_context(self, ctx: RepairTransferState) -> None:
        if ctx.source_transfer_id:
            self.repair_source_index.pop(str(ctx.source_transfer_id), None)
        if ctx.target_transfer_id:
            self.repair_target_index.pop(str(ctx.target_transfer_id), None)
        self.repairs.pop(ctx.repair_job_id, None)

    def _abort_repair_transport(self, ctx: RepairTransferState) -> None:
        """Best-effort immediate cleanup; the persistent cleanup queue is authoritative."""
        try:
            self._send_node_json(
                ctx.target_node_id,
                {
                    "op": "repair_abort",
                    "repair_job_id": ctx.repair_job_id,
                    "file_object_id": ctx.file_object_id,
                    "transfer_id": ctx.target_transfer_id,
                },
            )
        except Exception:
            pass
        if ctx.current_source_node_id and ctx.source_transfer_id:
            try:
                self._send_node_json(
                    ctx.current_source_node_id,
                    {"op": "stream_object_cancel", "transfer_id": ctx.source_transfer_id},
                )
            except Exception:
                pass

    def _record_repair_metric(
        self,
        ctx: RepairTransferState,
        *,
        node_id: str,
        operation: str,
        success: bool,
        error_code: Optional[str] = None,
        bytes_count: Optional[int] = None,
    ) -> None:
        try:
            latency_ms = max(0, int((time.monotonic() - ctx.started_monotonic) * 1000.0))
            with db_conn() as conn:
                with conn.cursor() as cur:
                    record_node_transfer_metric(
                        cur,
                        node_id=str(node_id),
                        file_object_id=ctx.file_object_id,
                        transfer_id=ctx.repair_job_id,
                        operation=str(operation),
                        success=bool(success),
                        bytes_count=ctx.copied_bytes if bytes_count is None else max(0, int(bytes_count)),
                        latency_ms=latency_ms,
                        error_code=None if success else str(error_code or "repair_failed"),
                    )
                conn.commit()
        except Exception as exc:
            _log_event(
                "ERROR",
                "repair metric persistence failed",
                repair_job_id=ctx.repair_job_id,
                node_id=node_id,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _start_repair_target(self, ctx: RepairTransferState) -> bool:
        self.repair_target_index[ctx.target_transfer_id] = ctx.repair_job_id
        try:
            with db_conn() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    started = mark_repair_target_started(
                        cur,
                        repair_job_id=ctx.repair_job_id,
                        target_node_id=ctx.target_node_id,
                        transfer_id=ctx.target_transfer_id,
                    )
                conn.commit()
            if not started:
                self._discard_repair_context(ctx)
                return False
            self._send_node_json(
                ctx.target_node_id,
                {
                    "op": "store_object_begin",
                    "repair_job_id": ctx.repair_job_id,
                    "transfer_id": ctx.target_transfer_id,
                    "file_object_id": ctx.file_object_id,
                    "file_size": ctx.file_size,
                    "chunk_size": ctx.chunk_size,
                },
            )
            ctx.phase = "awaiting_target_ready"
            ctx.touch()
            return True
        except Exception as exc:
            self._fail_repair_job(ctx, "target_begin_failed", detail=f"{type(exc).__name__}: {exc}")
            return False

    def _begin_next_repair_source(self, ctx: RepairTransferState) -> bool:
        if ctx.source_transfer_id:
            self.repair_source_index.pop(str(ctx.source_transfer_id), None)
        attempt = ctx.begin_next_source()
        if attempt is None:
            self._fail_repair_job(ctx, "no_healthy_source_available")
            return False
        source_node_id, source_transfer_id = attempt
        self.repair_source_index[source_transfer_id] = ctx.repair_job_id
        try:
            with db_conn() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    started = mark_repair_copying(
                        cur,
                        repair_job_id=ctx.repair_job_id,
                        source_node_id=source_node_id,
                        target_node_id=ctx.target_node_id,
                        transfer_id=ctx.target_transfer_id,
                        total_chunks=ctx.total_chunks,
                    )
                conn.commit()
            if not started:
                self._abort_repair_transport(ctx)
                self._discard_repair_context(ctx)
                return False
            self._send_node_json(
                source_node_id,
                {
                    "op": "stream_object_begin",
                    "transfer_id": source_transfer_id,
                    "file_object_id": ctx.file_object_id,
                },
            )
            return True
        except Exception as exc:
            self._restart_repair_from_next_source(
                ctx,
                "source_begin_failed",
                replica_status="suspect",
                verified_failure=False,
                detail=f"{type(exc).__name__}: {exc}",
            )
            return False

    def _restart_repair_from_next_source(
        self,
        ctx: RepairTransferState,
        error_code: str,
        *,
        replica_status: str,
        verified_failure: bool,
        detail: Optional[str] = None,
    ) -> None:
        source_node_id = str(ctx.current_source_node_id or "")
        old_target_transfer_id = str(ctx.target_transfer_id)
        if source_node_id:
            try:
                with db_conn() as conn:
                    with conn.cursor(row_factory=dict_row) as cur:
                        note_source_failure(
                            cur,
                            repair_job_id=ctx.repair_job_id,
                            file_object_id=ctx.file_object_id,
                            node_id=source_node_id,
                            error_code=error_code,
                            replica_status=replica_status,
                            verified_failure=verified_failure,
                        )
                        queue_repair_cleanup(
                            cur,
                            repair_job_id=ctx.repair_job_id,
                            file_object_id=ctx.file_object_id,
                            node_id=ctx.target_node_id,
                            transfer_id=old_target_transfer_id,
                        )
                    conn.commit()
            except Exception as exc:
                _log_event(
                    "ERROR",
                    "repair source failure persistence failed",
                    repair_job_id=ctx.repair_job_id,
                    node_id=source_node_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            self._record_repair_metric(
                ctx,
                node_id=source_node_id,
                operation="repair_source",
                success=False,
                error_code=error_code,
            )

        if ctx.source_transfer_id:
            self.repair_source_index.pop(str(ctx.source_transfer_id), None)
        self.repair_target_index.pop(old_target_transfer_id, None)
        self._abort_repair_transport(ctx)

        if (
            len(ctx.attempted_source_node_ids) >= ctx.max_source_attempts
            or ctx.source_index >= len(ctx.source_node_ids)
        ):
            self._fail_repair_job(ctx, error_code, detail=detail)
            return

        ctx.reset_target_transfer()
        self._start_repair_target(ctx)

    def _fail_repair_job(self, ctx: RepairTransferState, error_code: str, *, detail: Optional[str] = None) -> None:
        try:
            with db_conn() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    schedule_repair_retry(
                        cur,
                        repair_job_id=ctx.repair_job_id,
                        error_code=str(error_code),
                        detail=detail,
                    )
                conn.commit()
        except Exception as exc:
            _log_event(
                "ERROR",
                "repair retry persistence failed",
                repair_job_id=ctx.repair_job_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        self._record_repair_metric(
            ctx,
            node_id=ctx.target_node_id,
            operation="repair_target",
            success=False,
            error_code=error_code,
        )
        self._abort_repair_transport(ctx)
        self._discard_repair_context(ctx)
        _log_event(
            "WARN",
            "repair attempt ended",
            repair_job_id=ctx.repair_job_id,
            file_object_id=ctx.file_object_id,
            error_code=error_code,
        )

    def _process_repair_jobs(self) -> None:
        if not REPLICA_REPAIR_EXECUTION_ENABLED:
            return
        now_wall = time.time()
        if now_wall < self._next_repair_poll_ts:
            return
        self._next_repair_poll_ts = now_wall + max(0.25, REPAIR_POLL_INTERVAL_SEC)

        ended_contexts: List[RepairTransferState] = []
        start_specs: List[Tuple[Dict[str, Any], List[str], str]] = []
        try:
            with db_conn() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    if self.repairs:
                        statuses = fetch_repair_job_statuses(cur, list(self.repairs))
                        for job_id, ctx in list(self.repairs.items()):
                            status = statuses.get(job_id)
                            if status not in {"selecting_source", "selecting_target", "copying", "verifying"}:
                                ended_contexts.append(ctx)
                            else:
                                renew_repair_lease(
                                    cur,
                                    repair_job_id=job_id,
                                    worker_id=self.repair_worker_id,
                                    lease_sec=max(REPAIR_LEASE_SEC, int(AUDIT_TIMEOUT_SEC) + 30),
                                )

                    # Run after renewing this process's leases.  This also
                    # recovers jobs whose previous DataServer stopped only a
                    # few seconds before the current process started.
                    recovered = recover_stale_repair_jobs(cur)
                    if recovered:
                        _log_event("WARN", "stale repairs recovered", repair_job_ids=recovered)

                    available = max(0, max(1, REPAIR_MAX_INFLIGHT) - len(self.repairs))
                    claimed = claim_due_repair_jobs(
                        cur,
                        worker_id=self.repair_worker_id,
                        limit=max(1, available) if available else 1,
                    ) if available else []
                    for job in claimed:
                        job_id = str(job["repair_job_id"])
                        file_object_id = str(job["file_object_id"])
                        cur.execute(
                            "SELECT size_bytes,chunk_size FROM objects WHERE file_object_id=%s",
                            (file_object_id,),
                        )
                        object_row = cur.fetchone()
                        if not object_row:
                            schedule_repair_retry(
                                cur,
                                repair_job_id=job_id,
                                error_code="object_metadata_missing",
                            )
                            continue
                        size_bytes = int(object_row["size_bytes"])
                        sources = select_source_candidates(
                            cur,
                            file_object_id=file_object_id,
                            online_after=int(now_ts()) - NODE_ONLINE_WINDOW_SEC,
                            audit_after=int(now_ts()) - max(1, REPAIR_SOURCE_AUDIT_VALID_SEC),
                            limit=max(1, REPAIR_MAX_SOURCE_ATTEMPTS),
                        )
                        if not sources:
                            schedule_repair_retry(
                                cur,
                                repair_job_id=job_id,
                                error_code="no_verified_online_source",
                            )
                            continue
                        target = select_and_reserve_target(
                            cur,
                            repair_job_id=job_id,
                            file_object_id=file_object_id,
                            file_size=size_bytes,
                            source_node_ids=sources,
                            online_after=int(now_ts()) - NODE_ONLINE_WINDOW_SEC,
                        )
                        if not target:
                            schedule_repair_retry(
                                cur,
                                repair_job_id=job_id,
                                error_code="no_capacity_or_failure_domain_target",
                            )
                            continue
                        spec = dict(job)
                        spec["size_bytes"] = size_bytes
                        spec["chunk_size"] = int(object_row["chunk_size"])
                        start_specs.append((spec, sources, target))
                conn.commit()
        except Exception as exc:
            _log_event("ERROR", "repair polling failed", error=f"{type(exc).__name__}: {exc}")
            return

        for ctx in ended_contexts:
            self._abort_repair_transport(ctx)
            self._discard_repair_context(ctx)
        for job, sources, target in start_specs:
            ctx = RepairTransferState(
                repair_job_id=str(job["repair_job_id"]),
                file_object_id=str(job["file_object_id"]),
                file_size=int(job["size_bytes"]),
                chunk_size=int(job["chunk_size"]),
                target_node_id=str(target),
                source_node_ids=sources,
                max_source_attempts=max(1, REPAIR_MAX_SOURCE_ATTEMPTS),
            )
            self.repairs[ctx.repair_job_id] = ctx
            self._start_repair_target(ctx)
            _log_event(
                "INFO",
                "repair transfer started",
                repair_job_id=ctx.repair_job_id,
                file_object_id=ctx.file_object_id,
                target_node_id=ctx.target_node_id,
                source_candidates=ctx.source_node_ids,
            )

    def _process_repair_timeouts(self) -> None:
        for ctx in list(self.repairs.values()):
            if ctx.phase == "verifying" or not ctx.timed_out(REPAIR_STEP_TIMEOUT_SEC):
                continue
            if ctx.phase in {"awaiting_source_ready", "copying", "awaiting_source_end"}:
                self._restart_repair_from_next_source(
                    ctx,
                    "repair_source_timeout",
                    replica_status="suspect",
                    verified_failure=False,
                )
            else:
                self._fail_repair_job(ctx, "repair_target_timeout")

    def _process_repair_cleanup_queue(self) -> None:
        if not REPLICA_REPAIR_EXECUTION_ENABLED or time.time() < self._next_repair_cleanup_ts:
            return
        self._next_repair_cleanup_ts = time.time() + 5.0
        tasks: List[Dict[str, Any]] = []
        try:
            with db_conn() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    tasks = claim_repair_cleanup_tasks(cur, limit=max(1, REPAIR_CLEANUP_BATCH))
                conn.commit()
        except Exception as exc:
            _log_event("ERROR", "repair cleanup polling failed", error=f"{type(exc).__name__}: {exc}")
            return
        for task in tasks:
            try:
                self._send_node_json(
                    str(task["node_id"]),
                    {
                        "op": "repair_abort",
                        "repair_job_id": str(task["repair_job_id"]),
                        "file_object_id": str(task["file_object_id"]),
                        "transfer_id": str(task["transfer_id"]),
                    },
                )
            except Exception as exc:
                try:
                    with db_conn() as conn:
                        with conn.cursor() as cur:
                            mark_repair_cleanup_result(
                                cur,
                                node_id=str(task["node_id"]),
                                transfer_id=str(task["transfer_id"]),
                                success=False,
                                error=f"{type(exc).__name__}: {exc}",
                            )
                        conn.commit()
                except Exception:
                    pass

    def _handle_repair_source_stream(self, frames: List[bytes]) -> bool:
        if len(frames) != 6:
            return False
        node_id_b, _, transfer_id_b, chunk_id_b, hash_b, data = frames
        transfer_id = s(transfer_id_b)
        job_id = self.repair_source_index.get(transfer_id)
        ctx = self.repairs.get(job_id or "")
        node_id = s(node_id_b)
        if not ctx or not ctx.accepts_source(node_id=node_id, transfer_id=transfer_id):
            return False
        if sha256_hex(data) != s(hash_b):
            self._restart_repair_from_next_source(
                ctx,
                "repair_ciphertext_hash_mismatch",
                replica_status="corrupt",
                verified_failure=True,
            )
            return True
        try:
            chunk_id = int(s(chunk_id_b))
        except Exception:
            self._restart_repair_from_next_source(
                ctx,
                "repair_invalid_chunk_id",
                replica_status="suspect",
                verified_failure=False,
            )
            return True
        observation = ctx.observe_source_chunk(chunk_id, len(data))
        if observation == "invalid":
            self._restart_repair_from_next_source(
                ctx,
                "repair_chunk_out_of_range",
                replica_status="suspect",
                verified_failure=False,
            )
            return True
        if observation == "duplicate":
            return True
        try:
            self._send_node_data(
                ctx.target_node_id,
                [b"store", ctx.target_transfer_id.encode("utf-8"), chunk_id_b, hash_b, data],
            )
        except Exception as exc:
            self._fail_repair_job(
                ctx,
                "repair_target_chunk_send_failed",
                detail=f"{type(exc).__name__}: {exc}",
            )
            return True
        if ctx.copied_bytes - ctx.persisted_copied_bytes >= max(1, REPAIR_PROGRESS_FLUSH_BYTES):
            try:
                with db_conn() as conn:
                    with conn.cursor() as cur:
                        update_repair_progress(
                            cur,
                            repair_job_id=ctx.repair_job_id,
                            copied_bytes=ctx.copied_bytes,
                        )
                    conn.commit()
                ctx.persisted_copied_bytes = ctx.copied_bytes
            except Exception as exc:
                _log_event(
                    "ERROR",
                    "repair progress persistence failed",
                    repair_job_id=ctx.repair_job_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
        return True

    def _handle_repair_node_json(self, node_id_b: bytes, payload: Dict[str, Any]) -> bool:
        op = str(payload.get("op") or "")
        node_id = s(node_id_b)

        if op == "repair_abort_reply":
            transfer_id = str(payload.get("transfer_id") or "")
            if transfer_id:
                try:
                    with db_conn() as conn:
                        with conn.cursor() as cur:
                            mark_repair_cleanup_result(
                                cur,
                                node_id=node_id,
                                transfer_id=transfer_id,
                                success=str(payload.get("status") or "error") == "ok",
                                error=str(payload.get("message") or "repair_abort_failed"),
                            )
                        conn.commit()
                except Exception as exc:
                    _log_event(
                        "ERROR",
                        "repair cleanup result persistence failed",
                        node_id=node_id,
                        transfer_id=transfer_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            return True

        if op in {"store_object_ready", "store_ack", "store_object_end_reply"}:
            transfer_id = str(payload.get("transfer_id") or "")
            job_id = self.repair_target_index.get(transfer_id)
            ctx = self.repairs.get(job_id or "")
            if not ctx or not ctx.accepts_target(node_id=node_id, transfer_id=transfer_id):
                return bool(job_id)

            ctx.touch()
            if op == "store_object_ready":
                if str(payload.get("status") or "error") != "ready":
                    self._fail_repair_job(ctx, "repair_target_rejected_store")
                    return True
                node_total = int(payload.get("total_chunks", ctx.total_chunks) or 0)
                if node_total != ctx.total_chunks:
                    self._fail_repair_job(ctx, "repair_target_metadata_mismatch")
                    return True
                self._begin_next_repair_source(ctx)
                return True

            if op == "store_ack":
                if str(payload.get("status") or "error") != "ack":
                    self._fail_repair_job(
                        ctx,
                        "repair_target_chunk_rejected",
                        detail=str(payload.get("message") or "target rejected ciphertext chunk"),
                    )
                    return True
                try:
                    ctx.observe_target_ack(int(payload.get("chunk_id")))
                except Exception:
                    pass
                return True

            if str(payload.get("status") or "error") != "ok":
                self._fail_repair_job(
                    ctx,
                    "repair_target_finalize_failed",
                    detail=str(payload.get("message") or "target returned error"),
                )
                return True

            audit_job: Optional[Dict[str, Any]] = None
            try:
                with db_conn() as conn:
                    with conn.cursor(row_factory=dict_row) as cur:
                        changed = mark_repair_verifying(
                            cur,
                            repair_job_id=ctx.repair_job_id,
                            copied_bytes=ctx.copied_bytes,
                            lease_sec=max(REPAIR_LEASE_SEC, int(AUDIT_TIMEOUT_SEC) + 30),
                        )
                        if changed:
                            audit_job = create_repair_verification_audit(
                                cur,
                                repair_job_id=ctx.repair_job_id,
                                file_object_id=ctx.file_object_id,
                                node_id=ctx.target_node_id,
                            )
                        if not changed or audit_job is None:
                            schedule_repair_retry(
                                cur,
                                repair_job_id=ctx.repair_job_id,
                                error_code="verification_slice_unavailable" if changed else "repair_state_changed",
                            )
                    conn.commit()
            except Exception as exc:
                self._fail_repair_job(
                    ctx,
                    "repair_verification_setup_failed",
                    detail=f"{type(exc).__name__}: {exc}",
                )
                return True

            if audit_job is None:
                self._abort_repair_transport(ctx)
                self._discard_repair_context(ctx)
                return True
            ctx.phase = "verifying"
            ctx.touch()
            _log_event(
                "INFO",
                "repair copy stored; verification queued",
                repair_job_id=ctx.repair_job_id,
                audit_job_id=str(audit_job["audit_job_id"]),
                target_node_id=ctx.target_node_id,
            )
            return True

        if op in {"stream_object_ready", "stream_object_end"}:
            transfer_id = str(payload.get("transfer_id") or "")
            job_id = self.repair_source_index.get(transfer_id)
            ctx = self.repairs.get(job_id or "")
            if not ctx or not ctx.accepts_source(node_id=node_id, transfer_id=transfer_id):
                return bool(job_id)
            ctx.touch()
            if op == "stream_object_ready":
                if str(payload.get("status") or "error") != "ready":
                    self._restart_repair_from_next_source(
                        ctx,
                        "repair_source_object_missing",
                        replica_status="missing",
                        verified_failure=True,
                    )
                    return True
                node_total = int(payload.get("total_chunks", ctx.total_chunks) or 0)
                if node_total != ctx.total_chunks:
                    self._restart_repair_from_next_source(
                        ctx,
                        "repair_source_metadata_mismatch",
                        replica_status="corrupt",
                        verified_failure=True,
                    )
                    return True
                ctx.phase = "copying"
                return True

            if str(payload.get("status") or "error") != "ok":
                self._restart_repair_from_next_source(
                    ctx,
                    "repair_source_stream_failed",
                    replica_status="suspect",
                    verified_failure=False,
                )
                return True
            if not ctx.source_stream_complete():
                self._restart_repair_from_next_source(
                    ctx,
                    "repair_source_incomplete",
                    replica_status="missing",
                    verified_failure=True,
                )
                return True
            ctx.phase = "awaiting_target_commit"
            try:
                self._send_node_json(
                    ctx.target_node_id,
                    {
                        "op": "store_object_end",
                        "repair_job_id": ctx.repair_job_id,
                        "transfer_id": ctx.target_transfer_id,
                        "file_object_id": ctx.file_object_id,
                    },
                )
            except Exception as exc:
                self._fail_repair_job(
                    ctx,
                    "repair_target_finalize_send_failed",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            return True

        return False

    def _process_object_gc_queue(self) -> None:
        """API/ジョブ側が積んだ object_gc_queue を読み、ノードへ delete_object を送る。"""
        nowt = time.time()
        if nowt < self._next_object_gc_ts:
            return
        self._next_object_gc_ts = nowt + max(1.0, OBJECT_GC_POLL_SEC)

        with db_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                tasks = fetch_pending_object_gc_tasks(
                    cur,
                    limit=OBJECT_GC_QUEUE_BATCH,
                    retry_after_sec=OBJECT_GC_RETRY_AFTER_SEC,
                    max_attempts=OBJECT_GC_MAX_ATTEMPTS,
                )
                for task in tasks:
                    gc_id = str(task["gc_id"])
                    node_id = str(task["node_id"])
                    file_object_id = str(task["file_object_id"])
                    try:
                        self._send_node_json(node_id, {"op": "delete_object", "file_object_id": file_object_id})
                        mark_object_gc_task_sent(cur, gc_id)
                    except Exception as exc:
                        mark_object_gc_task_failed(cur, gc_id, str(exc))
            conn.commit()

    def _handle_object_gc_delete_reply(self, node_id_b: bytes, payload: Dict[str, Any]) -> None:
        node_id = s(node_id_b)
        file_object_id = str(payload.get("file_object_id", "") or "")
        if not file_object_id:
            return
        status = str(payload.get("status", "") or "")
        with db_conn() as conn:
            with conn.cursor() as cur:
                if status == "ok":
                    mark_object_gc_task_done_by_reply(cur, node_id=node_id, file_object_id=file_object_id)
                else:
                    cur.execute(
                        """
                        UPDATE object_gc_queue
                        SET status='pending', updated_at=%s, last_error=%s
                        WHERE node_id=%s AND file_object_id=%s AND status <> 'done'
                        """,
                        (int(now_ts()), status[:1000], node_id, file_object_id),
                    )
            conn.commit()

    def _dispatch_node_frames(self, nframes: List[bytes]) -> None:
        """Dispatch a node message received outside or inside an ACK wait loop.

        This prevents application heartbeats and download stream frames from
        being consumed and discarded while upload ACK/commit replies are being
        awaited.
        """
        if len(nframes) < 3:
            _log_event("WARN", "short node message ignored", frame_count=len(nframes))
            return

        kind = nframes[1]
        if kind == b"json":
            try:
                payload = jload(nframes[2])
            except Exception as exc:
                _log_event(
                    "WARN",
                    "invalid node JSON ignored",
                    node_id=s(nframes[0]),
                    error=f"{type(exc).__name__}: {exc}",
                )
                return
            try:
                self._handle_node_json(nframes[0], payload)
            except Exception as exc:
                _log_event(
                    "ERROR",
                    "node JSON handler failed",
                    node_id=s(nframes[0]),
                    op=str(payload.get("op") or ""),
                    error=f"{type(exc).__name__}: {exc}",
                )
            return

        if kind == b"stream":
            try:
                self._handle_node_stream(nframes)
            except Exception as exc:
                _log_event(
                    "ERROR",
                    "node stream handler failed",
                    node_id=s(nframes[0]),
                    error=f"{type(exc).__name__}: {exc}",
                )
            return

        _log_event(
            "WARN",
            "unknown node frame kind ignored",
            node_id=s(nframes[0]),
            kind=s(kind),
            frame_count=len(nframes),
        )

    def _handle_node_json(self, node_id_b: bytes, payload: Dict[str, Any]) -> None:
        op = payload.get("op")
        if op == "heartbeat":
            self._handle_node_heartbeat(node_id_b, payload)
            return

        if op == "delete_object_reply":
            self._handle_object_gc_delete_reply(node_id_b, payload)
            return

        if self._handle_repair_node_json(node_id_b, payload):
            return

        if op in {"audit_result", "audit_reply"}:
            event_id = str(payload.get("event_id", ""))
            got_hash = str(payload.get("hash", payload.get("hash_hex", "")) or "")
            status_in = str(payload.get("status", "error"))
            ap = self.audit_pending.get(event_id)
            if not ap:
                return
            if ap.node_id != s(node_id_b):
                _log_event(
                    "WARN",
                    "audit response node mismatch ignored",
                    audit_job_id=event_id,
                    expected_node_id=ap.node_id,
                    actual_node_id=s(node_id_b),
                )
                return
            latency_ms = int((time.time() - ap.sent_ts) * 1000.0)
            if status_in == "ok" and got_hash and got_hash == ap.expected_hash:
                self._apply_audit_result(event_id, "ok", got_hash, latency_ms)
            elif status_in == "missing":
                self._apply_audit_result(event_id, "missing", got_hash, latency_ms)
            elif status_in == "ok" and got_hash:
                self._apply_audit_result(event_id, "hash_mismatch", got_hash, latency_ms)
            else:
                self._apply_audit_result(
                    event_id,
                    "error",
                    got_hash,
                    latency_ms,
                    detail=str(payload.get("message") or "node audit error"),
                )
            return

        if op == "stream_object_ready":
            node_transfer_id = str(payload.get("transfer_id", "") or "")
            stable_transfer_id = self.node_transfer_index.get(node_transfer_id)
            ctx = self.transfers.get(stable_transfer_id or "")
            node_id = s(node_id_b)
            if not ctx or not ctx.failover.accepts_frame(
                node_id=node_id,
                node_transfer_id=node_transfer_id,
            ):
                return

            ctx.failover.touch()
            if str(payload.get("status", "error")) != "ready":
                self._start_next_download_attempt(
                    ctx,
                    previous_error="replica_object_missing",
                    previous_status="missing",
                    verified_failure=True,
                )
                return

            node_total_chunks = int(payload.get("total_chunks", ctx.total_chunks) or 0)
            if node_total_chunks != ctx.total_chunks:
                self._start_next_download_attempt(
                    ctx,
                    previous_error="replica_metadata_mismatch",
                    previous_status="corrupt",
                    verified_failure=True,
                )
            return

        if op == "stream_object_end":
            node_transfer_id = str(payload.get("transfer_id", "") or "")
            stable_transfer_id = self.node_transfer_index.get(node_transfer_id)
            ctx = self.transfers.get(stable_transfer_id or "")
            node_id = s(node_id_b)
            if not ctx or not ctx.failover.accepts_frame(
                node_id=node_id,
                node_transfer_id=node_transfer_id,
            ):
                return

            ctx.failover.touch()
            if str(payload.get("status", "error")) != "ok":
                self._start_next_download_attempt(
                    ctx,
                    previous_error="replica_stream_error",
                    previous_status="suspect",
                    verified_failure=False,
                )
                return

            if ctx.aborted_by_cap:
                self._finish_download_transfer(ctx)
                return

            global_missing = ctx.failover.global_missing()
            current_missing = ctx.failover.current_attempt_missing()
            if current_missing:
                if global_missing:
                    self._start_next_download_attempt(
                        ctx,
                        previous_error="replica_incomplete_stream",
                        previous_status="missing",
                        verified_failure=True,
                    )
                    return

                # The client obtained a complete object by combining validated
                # chunks from more than one replica.  The current replica is
                # still marked missing because it did not prove a full copy.
                self._record_download_attempt(
                    ctx,
                    success=False,
                    error_code="replica_incomplete_stream",
                    replica_status="missing",
                    verified_failure=True,
                )
                self._flush_transfer_meter(ctx)
                self._send_client_json(
                    ctx.client_id,
                    {"status": "done", "transfer_id": ctx.transfer_id, "failover_count": max(0, len(ctx.failover.attempted_node_ids) - 1)},
                )
                self._finish_download_transfer(ctx)
                return

            self._record_download_attempt(ctx, success=True)
            self._flush_transfer_meter(ctx)
            self._send_client_json(
                ctx.client_id,
                {"status": "done", "transfer_id": ctx.transfer_id, "failover_count": max(0, len(ctx.failover.attempted_node_ids) - 1)},
            )
            self._finish_download_transfer(ctx)
            return

    # ---------------- main loop ----------------
    def serve_forever(self) -> None:
        _log_event(
            "INFO",
            "DataServer started",
            client_endpoint=self.client_endpoint,
            node_endpoint=self.node_endpoint,
            pyzmq_version=getattr(zmq, "__version__", "unknown"),
            libzmq_version=zmq.zmq_version(),
            storage_audit_enabled=STORAGE_AUDIT_ENABLED,
            replica_repair_queue_enabled=REPLICA_REPAIR_QUEUE_ENABLED,
            replica_repair_execution_enabled=REPLICA_REPAIR_EXECUTION_ENABLED,
        )
        while True:
            # Bounded maintenance work; no HTTP request waits for these jobs.
            try:
                self._schedule_one_audit()
                nowt = time.time()
                expired = [
                    eid
                    for eid, ap in list(self.audit_pending.items())
                    if (nowt - ap.sent_ts) > max(0.5, AUDIT_TIMEOUT_SEC)
                ]
                for eid in expired:
                    self._apply_audit_result(eid, "timeout", "", int(AUDIT_TIMEOUT_SEC * 1000))
                self._process_download_timeouts()
                self._process_repair_jobs()
                self._process_repair_timeouts()
                self._process_repair_cleanup_queue()
                self._process_object_gc_queue()
            except Exception as exc:
                _log_event("ERROR", "maintenance loop iteration failed", error=f"{type(exc).__name__}: {exc}")

            socks = dict(self.poller.poll(timeout=100))

            if self.client_sock in socks:
                frames = self.client_sock.recv_multipart()
                if len(frames) < 3:
                    continue
                client_id = frames[0]
                kind = frames[1]

                # JSON control (simple): [client_id, "json", payload]
                if kind == b"json" and len(frames) == 3:
                    msg = jload(frames[2])
                    op = msg.get("op")
                    if op == "init_upload":
                        self._client_init_upload(client_id, msg)
                    elif op == "commit_multipart":
                        self._client_commit_multipart(client_id, msg)
                    elif op == "download_begin":
                        self._client_download_begin(client_id, msg)
                    elif op == "download_resend":
                        self._client_download_resend(client_id, msg)
                    else:
                        self._send_client_json(client_id, {"status": "error", "message": f"unknown op: {op}"})

                # JSON control (commit): [client_id, "json", session_id, payload]
                if kind == b"json" and len(frames) == 4:
                    session_id = s(frames[2])
                    ctrl = jload(frames[3])
                    if ctrl.get("op") == "commit":
                        self._client_commit(client_id, session_id)
                    else:
                        self._send_client_json(client_id, {"status": "error", "message": "unknown op"})

                # upload chunk: [client_id, "data", session_id, chunk_id, hash, data]
                if kind == b"data":
                    self._client_chunk(client_id, frames[1:])

            if self.node_sock in socks:
                # A client handler may have consumed the ready node message in
                # its ACK wait loop. NOBLOCK avoids waiting on a stale poll flag.
                try:
                    nframes = self.node_sock.recv_multipart(flags=zmq.NOBLOCK)
                except zmq.Again:
                    pass
                else:
                    self._dispatch_node_frames(nframes)

def main() -> None:
    client_ep = os.environ.get("CLIENT_ENDPOINT", "tcp://*:8888")
    node_ep = os.environ.get("NODE_ENDPOINT", "tcp://*:9999")
    try:
        DataServer(client_ep, node_ep).serve_forever()
    except KeyboardInterrupt:
        # systemd uses KillSignal=SIGINT for this service. Treat it as a
        # normal, intentional shutdown instead of emitting a traceback.
        _log_event("INFO", "DataServer shutdown requested by SIGINT")


if __name__ == "__main__":
    main()
