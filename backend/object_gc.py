# -*- coding: utf-8 -*-
"""
object_gc.py

完全削除後のオブジェクトGC補助。

方針:
- items / item_parts / item_versions / item_version_parts から参照されなくなった
  file_object_id だけをGC対象にする。
- FINALIZED 済みの multipart_parts は履歴扱いとして参照元に含めず、
  未確定 multipart だけ保護する。
- DB上のメタデータを先に安全に整理し、ノード上の実チャンク削除は
  object_gc_queue に積んで DataServer が非同期に delete_object を送る。
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from psycopg.rows import dict_row

from meta_db_pg import db_conn, now_ts
from repair_object_lock import lock_repair_object


OBJECT_GC_QUEUE_DDL = [
    """
    CREATE TABLE IF NOT EXISTS object_gc_queue (
        gc_id TEXT PRIMARY KEY,
        file_object_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        reason TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER,
        last_error TEXT,
        UNIQUE(file_object_id, node_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_object_gc_queue_status ON object_gc_queue(status, updated_at, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_object_gc_queue_object ON object_gc_queue(file_object_id)",
]


def init_object_gc_schema(cur=None) -> None:
    """object_gc_queue を作成する。cur を渡せば既存トランザクション内で実行する。"""
    if cur is not None:
        for stmt in OBJECT_GC_QUEUE_DDL:
            cur.execute(stmt)
        return

    with db_conn() as conn:
        with conn.cursor() as local_cur:
            for stmt in OBJECT_GC_QUEUE_DDL:
                local_cur.execute(stmt)
        conn.commit()


def _dedupe(values: Iterable[Optional[str]]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for value in values:
        if not value:
            continue
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def collect_file_object_ids_for_items(cur, item_ids: Sequence[str]) -> List[str]:
    """削除予定 item 群が現在・版履歴で参照している file_object_id を集める。"""
    ids = [str(x) for x in item_ids if x]
    if not ids:
        return []

    result: List[str] = []

    cur.execute(
        "SELECT file_object_id FROM items WHERE item_id = ANY(%s) AND file_object_id IS NOT NULL",
        (ids,),
    )
    result.extend(str(r["file_object_id"]) for r in cur.fetchall())

    cur.execute(
        "SELECT file_object_id FROM item_parts WHERE item_id = ANY(%s)",
        (ids,),
    )
    result.extend(str(r["file_object_id"]) for r in cur.fetchall())

    cur.execute(
        "SELECT file_object_id FROM item_versions WHERE item_id = ANY(%s) AND file_object_id IS NOT NULL",
        (ids,),
    )
    result.extend(str(r["file_object_id"]) for r in cur.fetchall())

    cur.execute(
        """
        SELECT p.file_object_id
        FROM item_version_parts p
        JOIN item_versions v ON v.version_id = p.version_id
        WHERE v.item_id = ANY(%s)
        """,
        (ids,),
    )
    result.extend(str(r["file_object_id"]) for r in cur.fetchall())

    return _dedupe(result)


def find_unreferenced_file_object_ids(cur, candidate_file_object_ids: Sequence[str]) -> List[str]:
    """候補のうち、現在どこからも参照されていない file_object_id を返す。"""
    candidates = _dedupe(candidate_file_object_ids)
    if not candidates:
        return []

    cur.execute(
        """
        WITH candidates AS (
            SELECT unnest(%s::text[]) AS file_object_id
        )
        SELECT c.file_object_id
        FROM candidates c
        WHERE NOT EXISTS (
            SELECT 1 FROM items i WHERE i.file_object_id = c.file_object_id
        )
        AND NOT EXISTS (
            SELECT 1 FROM item_parts p WHERE p.file_object_id = c.file_object_id
        )
        AND NOT EXISTS (
            SELECT 1 FROM item_versions v WHERE v.file_object_id = c.file_object_id
        )
        AND NOT EXISTS (
            SELECT 1 FROM item_version_parts vp WHERE vp.file_object_id = c.file_object_id
        )
        AND NOT EXISTS (
            SELECT 1
            FROM multipart_parts mp
            JOIN multipart_uploads mu ON mu.upload_id = mp.upload_id
            WHERE mp.file_object_id = c.file_object_id
              AND COALESCE(mu.status, '') <> 'FINALIZED'
        )
        ORDER BY c.file_object_id
        """,
        (candidates,),
    )
    return [str(r["file_object_id"]) for r in cur.fetchall()]


def _object_meta(cur, file_object_id: str) -> Dict[str, Any]:
    """objects が消えている場合も object_lifetimes から最低限の情報を拾う。"""
    cur.execute(
        """
        SELECT o.file_object_id,
               o.owner_user_id,
               o.size_bytes,
               o.created_at,
               ol.start_ts AS lifetime_start_ts,
               ol.size_bytes AS lifetime_size_bytes,
               ol.owner_user_id AS lifetime_owner_user_id
        FROM objects o
        LEFT JOIN object_lifetimes ol ON ol.file_object_id = o.file_object_id
        WHERE o.file_object_id=%s
        ORDER BY ol.start_ts ASC NULLS LAST
        LIMIT 1
        """,
        (file_object_id,),
    )
    row = cur.fetchone()
    if row:
        d = dict(row)
        return {
            "file_object_id": file_object_id,
            "owner_user_id": d.get("owner_user_id") or d.get("lifetime_owner_user_id"),
            "size_bytes": int(d.get("size_bytes") or d.get("lifetime_size_bytes") or 0),
            "start_ts": int(d.get("lifetime_start_ts") or d.get("created_at") or now_ts()),
        }

    cur.execute(
        """
        SELECT owner_user_id, size_bytes, start_ts
        FROM object_lifetimes
        WHERE file_object_id=%s
        ORDER BY start_ts ASC
        LIMIT 1
        """,
        (file_object_id,),
    )
    row = cur.fetchone()
    if row:
        d = dict(row)
        return {
            "file_object_id": file_object_id,
            "owner_user_id": d.get("owner_user_id"),
            "size_bytes": int(d.get("size_bytes") or 0),
            "start_ts": int(d.get("start_ts") or now_ts()),
        }

    return {"file_object_id": file_object_id, "owner_user_id": None, "size_bytes": 0, "start_ts": int(now_ts())}


def _replica_node_ids(cur, file_object_id: str) -> List[str]:
    cur.execute(
        "SELECT node_id FROM replicas WHERE file_object_id=%s ORDER BY node_id",
        (file_object_id,),
    )
    return [str(r["node_id"]) for r in cur.fetchall()]


def _enqueue_node_delete(cur, *, file_object_id: str, node_id: str, reason: str, ts: int) -> bool:
    gc_id = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO object_gc_queue(gc_id,file_object_id,node_id,status,reason,attempts,created_at,updated_at,last_error)
        VALUES (%s,%s,%s,'pending',%s,0,%s,NULL,NULL)
        ON CONFLICT (file_object_id, node_id) DO NOTHING
        RETURNING gc_id
        """,
        (gc_id, file_object_id, node_id, reason, ts),
    )
    return cur.fetchone() is not None


def gc_unreferenced_objects(
    cur,
    candidate_file_object_ids: Sequence[str],
    *,
    reason: str = "purge",
) -> Dict[str, Any]:
    """参照ゼロになった objects/replicas/file_wrapped_keys を整理し、ノード削除をqueueする。"""
    init_object_gc_schema(cur)
    candidates = _dedupe(candidate_file_object_ids)
    if not candidates:
        return {
            "candidate_count": 0,
            "gc_object_count": 0,
            "queued_delete_count": 0,
            "released_replica_count": 0,
            "released_bytes": 0,
            "gc_file_object_ids": [],
        }

    gc_ids = find_unreferenced_file_object_ids(cur, candidates)
    ts = int(now_ts())
    queued = 0
    released_replica_count = 0
    released_bytes = 0

    processed_gc_ids: List[str] = []
    for oid in gc_ids:
        # Repair publication and object deletion must serialize on the same
        # object row. Re-check references after the optimistic candidate scan.
        if not lock_repair_object(cur, file_object_id=oid):
            continue
        if oid not in find_unreferenced_file_object_ids(cur, [oid]):
            continue
        processed_gc_ids.append(oid)
        meta = _object_meta(cur, oid)
        size_bytes = int(meta.get("size_bytes") or 0)
        start_ts = int(meta.get("start_ts") or ts)
        node_ids = _replica_node_ids(cur, oid)

        for node_id in node_ids:
            if _enqueue_node_delete(cur, file_object_id=oid, node_id=node_id, reason=reason, ts=ts):
                queued += 1

            # replica_lifetimes が存在する環境では、replicas削除後も報酬計算に履歴を残す。
            cur.execute(
                """
                INSERT INTO replica_lifetimes(file_object_id,node_id,size_bytes,start_ts,end_ts)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (file_object_id, node_id, start_ts) DO UPDATE SET
                  end_ts = COALESCE(replica_lifetimes.end_ts, EXCLUDED.end_ts)
                """,
                (oid, node_id, size_bytes, start_ts, ts),
            )

        cur.execute("UPDATE object_lifetimes SET end_ts=%s WHERE file_object_id=%s AND end_ts IS NULL", (ts, oid))
        cur.execute("UPDATE replica_lifetimes SET end_ts=%s WHERE file_object_id=%s AND end_ts IS NULL", (ts, oid))
        cur.execute("DELETE FROM download_tokens WHERE file_object_id=%s", (oid,))
        cur.execute("DELETE FROM file_wrapped_keys WHERE file_object_id=%s", (oid,))
        cur.execute("DELETE FROM replicas WHERE file_object_id=%s RETURNING node_id", (oid,))
        deleted_replicas = [str(r["node_id"]) for r in cur.fetchall()]
        for node_id in deleted_replicas:
            if size_bytes > 0:
                cur.execute(
                    "UPDATE nodes SET reserved_bytes = GREATEST(0, reserved_bytes - %s) WHERE node_id=%s",
                    (size_bytes, node_id),
                )
                released_bytes += size_bytes
            released_replica_count += 1
        cur.execute("DELETE FROM objects WHERE file_object_id=%s", (oid,))

    return {
        "candidate_count": len(candidates),
        "gc_object_count": len(processed_gc_ids),
        "queued_delete_count": queued,
        "released_replica_count": released_replica_count,
        "released_bytes": released_bytes,
        "gc_file_object_ids": processed_gc_ids,
    }


def fetch_pending_object_gc_tasks(cur, *, limit: int = 50, retry_after_sec: int = 300, max_attempts: int = 5) -> List[Dict[str, Any]]:
    init_object_gc_schema(cur)
    ts = int(now_ts())
    retry_before = ts - int(retry_after_sec)
    cur.execute(
        """
        SELECT gc_id,file_object_id,node_id,status,attempts,created_at,updated_at
        FROM object_gc_queue
        WHERE status='pending'
           OR (status='sent' AND COALESCE(updated_at, created_at) <= %s AND attempts < %s)
        ORDER BY created_at ASC
        LIMIT %s
        """,
        (retry_before, int(max_attempts), int(limit)),
    )
    return [dict(r) for r in cur.fetchall()]


def mark_object_gc_task_sent(cur, gc_id: str) -> None:
    cur.execute(
        """
        UPDATE object_gc_queue
        SET status='sent', attempts=attempts+1, updated_at=%s, last_error=NULL
        WHERE gc_id=%s
        """,
        (int(now_ts()), str(gc_id)),
    )


def mark_object_gc_task_failed(cur, gc_id: str, error: str) -> None:
    cur.execute(
        """
        UPDATE object_gc_queue
        SET status='pending', updated_at=%s, last_error=%s
        WHERE gc_id=%s
        """,
        (int(now_ts()), str(error)[:1000], str(gc_id)),
    )


def mark_object_gc_task_done_by_reply(cur, *, node_id: str, file_object_id: str) -> None:
    cur.execute(
        """
        UPDATE object_gc_queue
        SET status='done', updated_at=%s, last_error=NULL
        WHERE node_id=%s AND file_object_id=%s AND status <> 'done'
        """,
        (int(now_ts()), str(node_id), str(file_object_id)),
    )
