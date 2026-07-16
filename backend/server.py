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
from dataclasses import dataclass, field
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
    init_object_gc_schema,
    mark_object_gc_task_done_by_reply,
    mark_object_gc_task_failed,
    mark_object_gc_task_sent,
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
    node_id: str
    file_object_id: str
    total_chunks: int
    charge_user_id: str
    is_shared: bool
    bytes_since_flush: int = 0
    got: Set[int] = field(default_factory=set)
    aborted_by_cap: bool = False




@dataclass
class AuditPending:
    event_id: str
    node_id: str
    file_object_id: str
    chunk_id: int
    offset: int
    length: int
    expected_hash: str
    sent_ts: float
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

        # --- audit (challenge-response) ---
        self.audit_pending: Dict[str, AuditPending] = {}
        self._next_audit_ts: float = time.time() + 5.0
        self._init_audit_schema()

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
                        WHERE n.last_seen >= %s AND u.country_code=%s
                        """,
                        (now_ts() - NODE_ONLINE_WINDOW_SEC, country_code),
                    )
                else:
                    cur.execute(
                        "SELECT node_id, capacity_bytes, reserved_bytes FROM nodes WHERE last_seen >= %s",
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

    def _init_node_heartbeat_stats_schema(self) -> None:
        """ノード heartbeat の月次表示用集計テーブルを作成する。"""
        with db_conn() as conn:
            with conn.cursor() as cur:
                init_node_heartbeat_stats_schema(cur)
            conn.commit()

    def _init_audit_schema(self) -> None:
        """テスト版向けの監査補助テーブルを作成する。

        現在の node.py は audit challenge には未対応なので、スケジューリング側は no-op にしている。
        ただしアップロード時の chunk_audit_slices 保存は行われるため、必要テーブルだけ作成する。
        """
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chunk_audit_slices (
                        file_object_id TEXT NOT NULL,
                        chunk_id INTEGER NOT NULL,
                        slice_index INTEGER NOT NULL,
                        byte_offset INTEGER NOT NULL,
                        length INTEGER NOT NULL,
                        hash_hex TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        PRIMARY KEY (file_object_id, chunk_id, slice_index)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chunk_audit_results (
                        event_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        got_hash TEXT,
                        latency_ms INTEGER,
                        created_at INTEGER NOT NULL
                    )
                    """
                )
            conn.commit()

    def _schedule_one_audit(self) -> None:
        """node.py 側が audit challenge 未対応のため、テスト版では無効化する。"""
        return

    def _apply_audit_result(self, event_id: str, status: str, got_hash: str, latency_ms: int) -> None:
        """audit challenge を後日有効化した場合の結果保存口。現状は安全な no-op。"""
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO chunk_audit_results(event_id,status,got_hash,latency_ms,created_at)
                        VALUES (%s,%s,%s,%s,%s)
                        ON CONFLICT (event_id) DO UPDATE SET
                          status=EXCLUDED.status,
                          got_hash=EXCLUDED.got_hash,
                          latency_ms=EXCLUDED.latency_ms
                        """,
                        (str(event_id), str(status), str(got_hash or ""), int(latency_ms or 0), int(now_ts())),
                    )
                conn.commit()
        except Exception:
            pass

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
        previous_accepted = self._heartbeat_last_accepted.get(node_id)

        try:
            with db_conn() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        """
                        INSERT INTO nodes(node_id,last_seen,capacity_bytes,reserved_bytes,meta_json)
                        VALUES (%s,%s,%s,COALESCE((SELECT reserved_bytes FROM nodes WHERE node_id=%s),0),%s)
                        ON CONFLICT (node_id) DO UPDATE SET
                          last_seen=EXCLUDED.last_seen,
                          capacity_bytes=EXCLUDED.capacity_bytes,
                          meta_json=EXCLUDED.meta_json
                        RETURNING reserved_bytes, capacity_bytes
                        """,
                        (node_id, ts, capacity, node_id, meta_json),
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
            SLICE_LEN = int(os.environ.get("AUDIT_SLICE_LEN", "1024"))
            SLICE_N = int(os.environ.get("AUDIT_SLICES_PER_CHUNK", "3"))
            if len(blob) >= SLICE_LEN:
                with db_conn() as conn:
                    with conn.cursor() as cur:
                        for i in range(SLICE_N):
                            off = 0 if len(blob) == SLICE_LEN else (int.from_bytes(os.urandom(4), "big") % (len(blob) - SLICE_LEN + 1))
                            seg = blob[off:off+SLICE_LEN]
                            h = sha256_hex(seg)
                            cur.execute(
                                """
                                INSERT INTO chunk_audit_slices(file_object_id,chunk_id,slice_index,byte_offset,length,hash_hex,created_at)
                                VALUES (%s,%s,%s,%s,%s,%s,%s)
                                ON CONFLICT (file_object_id,chunk_id,slice_index) DO NOTHING
                                """,
                                (file_object_id, chunk_id, i, int(off), int(SLICE_LEN), h, now_ts())
                            )
                    conn.commit()
        except Exception:
            pass

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

                    # 旧オブジェクトの寿命終了 + replica削除（簡易）
                    if old_oid:
                        cur.execute("UPDATE object_lifetimes SET end_ts=%s WHERE file_object_id=%s AND end_ts IS NULL", (now, old_oid))
                        cur.execute("DELETE FROM replicas WHERE file_object_id=%s", (old_oid,))
                        cur.execute("DELETE FROM objects WHERE file_object_id=%s", (old_oid,))
                        # ノード側実体削除は非同期ジョブ推奨だが、ここでは同期発行だけ
                        for nid in node_ids:
                            self._send_node_json(nid, {"op": "delete_object", "file_object_id": old_oid})
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

                cur.execute("SELECT node_id FROM replicas WHERE file_object_id=%s ORDER BY created_at ASC", (file_object_id,))
                # row_factory=dict_row のカーソルでは、取得行は辞書形式になる。
                # r[0] では KeyError: 0 になるため、列名 node_id で取得する。
                reps = []
                for r in cur.fetchall():
                    if isinstance(r, dict):
                        reps.append(s(r["node_id"]))
                    else:
                        reps.append(s(r[0]))
                if not reps:
                    self._send_client_json(client_id, {"status": "error", "message": "no replicas"})
                    return
                node_id = reps[0]

        transfer_id = uuid.uuid4().hex
        self.transfers[transfer_id] = TransferCtx(
            transfer_id=transfer_id,
            client_id=client_id,
            node_id=node_id,
            file_object_id=file_object_id,
            total_chunks=total_chunks,
            charge_user_id=charge_user_id,
            is_shared=is_shared,
        )

        self._send_node_json(node_id, {
            "op": "stream_object_begin",
            "transfer_id": transfer_id,
            "file_object_id": file_object_id
        })
        self._send_client_json(client_id, {"status": "ready", "transfer_id": transfer_id, "total_chunks": total_chunks})

    def _client_download_resend(self, client_id: bytes, msg: Dict[str, Any]) -> None:
        transfer_id = str(msg.get("transfer_id", ""))
        missing = [int(x) for x in (msg.get("missing") or [])]
        ctx = self.transfers.get(transfer_id)
        if not ctx or ctx.client_id != client_id:
            self._send_client_json(client_id, {"status": "error", "message": "unknown transfer"})
            return
        if not missing:
            return
        self._send_node_json(ctx.node_id, {
            "op": "stream_object_resend",
            "transfer_id": transfer_id,
            "file_object_id": ctx.file_object_id,
            "missing": missing
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
        _, _, tid_b, cid_b, hash_b, data = frames
        transfer_id = s(tid_b)
        ctx = self.transfers.get(transfer_id)
        if not ctx:
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
                "transfer_id": transfer_id,
                "remaining_bytes": int(remaining),
                "daily_limit_bytes": int(limit_b) if limit_b is not None else None,
                "is_shared": ctx.is_shared,
            })
            self.transfers.pop(transfer_id, None)
            return

        # chunk中継
        self.client_sock.send_multipart([ctx.client_id, b"stream", tid_b, cid_b, hash_b, data])

        ctx.bytes_since_flush += len(data)
        try:
            ctx.got.add(int(s(cid_b)))
        except Exception:
            pass

        if ctx.bytes_since_flush >= CHUNK_METER_FLUSH_BYTES:
            self._flush_transfer_meter(ctx)

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
            self._handle_node_json(nframes[0], payload)
            return

        if kind == b"stream":
            self._handle_node_stream(nframes)
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

        if op == "audit_reply":
            event_id = str(payload.get("event_id", ""))
            got_hash = str(payload.get("hash_hex", ""))
            status_in = str(payload.get("status", "error"))
            ap = self.audit_pending.get(event_id)
            if not ap:
                return
            latency_ms = int((time.time() - ap.sent_ts) * 1000.0)
            if status_in == "ok" and got_hash and got_hash == ap.expected_hash:
                self._apply_audit_result(event_id, "ok", got_hash, latency_ms)
            elif status_in == "missing":
                self._apply_audit_result(event_id, "missing", got_hash, latency_ms)
            else:
                self._apply_audit_result(event_id, "fail", got_hash, latency_ms)
            return

        if op == "stream_object_end":
            transfer_id = str(payload.get("transfer_id", ""))
            ctx = self.transfers.get(transfer_id)
            if not ctx:
                return

            self._flush_transfer_meter(ctx)

            if ctx.aborted_by_cap:
                self.transfers.pop(transfer_id, None)
                return

            missing = [i for i in range(ctx.total_chunks) if i not in ctx.got]
            if missing:
                self._send_client_json(ctx.client_id, {"status": "incomplete", "transfer_id": transfer_id, "missing": missing})
            else:
                self._send_client_json(ctx.client_id, {"status": "done", "transfer_id": transfer_id})
            self.transfers.pop(transfer_id, None)
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
        )
        while True:
            # periodic node audits (challenge-response)
            try:
                self._schedule_one_audit()
                # timeout cleanup
                ttl = float(os.environ.get("AUDIT_TIMEOUT_SEC", "8"))
                nowt = time.time()
                expired = [eid for eid, ap in list(self.audit_pending.items()) if (nowt - ap.sent_ts) > ttl]
                for eid in expired:
                    self._apply_audit_result(eid, "timeout", "", int(ttl*1000))
                self._process_object_gc_queue()
            except Exception:
                pass

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