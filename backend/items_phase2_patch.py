# -*- coding: utf-8 -*-
"""
items_phase2_patch.py

フェーズ2で必要な追加:
- ごみ箱一覧 / 復元 / 完全削除
- 版履歴（通常ファイル / multipart 論理ファイル両対応）
- デスクトップ同期クライアント用のツリー一覧 / ダウンロードマニフェスト / オブジェクトDL token
- 同期クライアント設定 / heartbeat

既存の items テーブルや item_parts テーブルを壊さずに薄く拡張する。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from psycopg.rows import dict_row

from meta_db_pg import db_conn, now_ts
from auth_util import JWT_SECRET, jwt_decode, JWTError
from object_gc import collect_file_object_ids_for_items, gc_unreferenced_objects

ROOT_ID = "root"
router = APIRouter(tags=["phase2-items"])


DDL = [
    """
    CREATE TABLE IF NOT EXISTS item_versions (
        version_id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL,
        version_no INTEGER NOT NULL,
        file_object_id TEXT,
        name TEXT NOT NULL,
        size_bytes BIGINT NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        created_by_user_id TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'replace',
        restore_from_version_id TEXT,
        UNIQUE(item_id, version_no)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_item_versions_item_created ON item_versions(item_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS item_version_parts (
        version_id TEXT NOT NULL,
        part_index INTEGER NOT NULL,
        file_object_id TEXT NOT NULL,
        part_offset BIGINT NOT NULL,
        part_size BIGINT NOT NULL,
        PRIMARY KEY(version_id, part_index)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_item_version_parts_ver ON item_version_parts(version_id, part_index)",
    """
    CREATE TABLE IF NOT EXISTS sync_profiles (
        user_id TEXT PRIMARY KEY,
        local_root_display TEXT,
        sync_mode TEXT NOT NULL DEFAULT 'mirror',
        polling_interval_sec INTEGER NOT NULL DEFAULT 5,
        ignore_hidden BOOLEAN NOT NULL DEFAULT TRUE,
        created_at INTEGER NOT NULL,
        updated_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_clients (
        client_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        local_root_display TEXT,
        status TEXT NOT NULL DEFAULT 'idle',
        sync_mode TEXT NOT NULL DEFAULT 'mirror',
        pending_changes INTEGER NOT NULL DEFAULT 0,
        app_version TEXT,
        last_seen INTEGER NOT NULL,
        last_sync_at INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sync_clients_user_seen ON sync_clients(user_id, last_seen DESC)",
]


def init_phase2_items_schema() -> None:
    """Phase2 item / trash / version / sync 用の追加テーブルを作成する。

    メインアプリ側が FastAPI lifespan を使うため、router の startup event には依存しない。
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            for stmt in DDL:
                cur.execute(stmt)
        conn.commit()


# ---------- auth helpers ----------
def bearer_token(authorization: Optional[str] = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return authorization.split(" ", 1)[1].strip()


def current_user_id(token: str = Depends(bearer_token)) -> str:
    try:
        td = jwt_decode(token, JWT_SECRET)
    except JWTError as exc:
        raise HTTPException(status_code=401, detail=str(exc) or "Invalid or expired token")
    return td.sub


# ---------- request / response ----------
class FolderCreateIn(BaseModel):
    name: str
    parent_id: Optional[str] = None


class ItemPatchIn(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[str] = None


class ListOut(BaseModel):
    items: List[Dict[str, Any]]
    parent: Optional[Dict[str, Any]] = None
    breadcrumbs: List[Dict[str, Any]] = []


class SearchOut(BaseModel):
    items: List[Dict[str, Any]]
    q: str
    total: int


class VersionsOut(BaseModel):
    item: Dict[str, Any]
    versions: List[Dict[str, Any]]


class SyncProfileIn(BaseModel):
    local_root_display: str = Field(default="~/Phase1 Drive")
    sync_mode: str = Field(default="mirror")
    polling_interval_sec: int = Field(default=5, ge=2, le=600)
    ignore_hidden: bool = True


class SyncHeartbeatIn(BaseModel):
    client_id: str
    local_root_display: str
    status: str = Field(default="idle")
    sync_mode: str = Field(default="mirror")
    pending_changes: int = Field(default=0, ge=0)
    app_version: Optional[str] = None
    last_sync_at: Optional[int] = None


# ---------- generic helpers ----------
def _normalize_parent_id(parent_id: Optional[str]) -> str:
    return str(parent_id or ROOT_ID)


def _assert_folder_owner(cur, uid: str, parent_id: str) -> None:
    cur.execute(
        """
        SELECT item_id, owner_user_id, type, trashed_at
        FROM items
        WHERE item_id=%s
        """,
        (parent_id,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="folder not found")
    if str(row["owner_user_id"]) != uid:
        raise HTTPException(status_code=403, detail="forbidden")
    if str(row["type"]) != "folder":
        raise HTTPException(status_code=400, detail="parent must be folder")
    if row["trashed_at"] is not None:
        raise HTTPException(status_code=400, detail="parent is trashed")


def _fetch_item(cur, item_id: str, uid: str, *, allow_trashed: bool = True) -> Dict[str, Any]:
    where_trashed = "" if allow_trashed else "AND trashed_at IS NULL"
    cur.execute(
        f"""
        SELECT i.item_id,i.type,i.parent_id,i.name,i.size_bytes,i.file_object_id,
               i.created_at,i.updated_at,i.trashed_at,i.trash_batch_id,i.owner_user_id,
               COALESCE(v.version_count, 0) AS version_count
        FROM items i
        LEFT JOIN (
            SELECT item_id, COUNT(*) AS version_count
            FROM item_versions
            GROUP BY item_id
        ) v ON v.item_id = i.item_id
        WHERE i.item_id=%s AND i.owner_user_id=%s {where_trashed}
        """,
        (item_id, uid),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="item not found")
    return dict(row)


def _build_breadcrumbs(cur, uid: str, parent_id: str) -> List[Dict[str, Any]]:
    if parent_id == ROOT_ID:
        return []
    crumbs: List[Dict[str, Any]] = []
    current = parent_id
    guard = 0
    while current and current != ROOT_ID and guard < 30:
        cur.execute(
            """
            SELECT item_id,name,parent_id,type
            FROM items
            WHERE item_id=%s AND owner_user_id=%s AND trashed_at IS NULL
            """,
            (current, uid),
        )
        row = cur.fetchone()
        if not row:
            break
        crumbs.append(dict(row))
        current = row["parent_id"]
        guard += 1
    crumbs.reverse()
    return crumbs


def _collect_descendants(cur, uid: str, root_item_id: str) -> List[Dict[str, Any]]:
    cur.execute(
        """
        WITH RECURSIVE tree AS (
            SELECT item_id,parent_id,type,name,trashed_at,owner_user_id
            FROM items
            WHERE item_id=%s AND owner_user_id=%s
            UNION ALL
            SELECT i.item_id,i.parent_id,i.type,i.name,i.trashed_at,i.owner_user_id
            FROM items i
            JOIN tree t ON i.parent_id = t.item_id
            WHERE i.owner_user_id=%s
        )
        SELECT * FROM tree
        """,
        (root_item_id, uid, uid),
    )
    return [dict(row) for row in cur.fetchall()]


def _next_version_no(cur, item_id: str) -> int:
    """次の版番号を返す。

    psycopg3 の dict_row カーソルでは fetchone() の戻り値が dict 型になるため、
    row[0] では KeyError(0) になる。
    tuple カーソルと dict_row カーソルの両方で動くようにしておく。
    """
    cur.execute(
        """
        SELECT COALESCE(MAX(version_no), 0) AS max_version_no
        FROM item_versions
        WHERE item_id=%s
        """,
        (item_id,),
    )
    row = cur.fetchone()
    if not row:
        return 1
    try:
        value = row["max_version_no"]
    except (KeyError, TypeError):
        value = row[0]
    return int(value or 0) + 1


def snapshot_item_version(
    cur,
    item_id: str,
    uid: str,
    *,
    source: str,
    restore_from_version_id: Optional[str] = None,
) -> Dict[str, Any]:
    item = _fetch_item(cur, item_id, uid, allow_trashed=True)
    version_no = _next_version_no(cur, item_id)
    version_id = str(uuid.uuid4())
    created = int(now_ts())
    cur.execute(
        """
        INSERT INTO item_versions(
            version_id,item_id,version_no,file_object_id,name,size_bytes,
            created_at,created_by_user_id,source,restore_from_version_id
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            version_id,
            item_id,
            version_no,
            item.get("file_object_id"),
            item["name"],
            int(item.get("size_bytes") or 0),
            created,
            uid,
            source,
            restore_from_version_id,
        ),
    )

    if not item.get("file_object_id"):
        cur.execute(
            """
            SELECT part_index,file_object_id,part_offset,part_size
            FROM item_parts
            WHERE item_id=%s
            ORDER BY part_index ASC
            """,
            (item_id,),
        )
        for p in cur.fetchall():
            cur.execute(
                """
                INSERT INTO item_version_parts(version_id,part_index,file_object_id,part_offset,part_size)
                VALUES (%s,%s,%s,%s,%s)
                """,
                (version_id, int(p["part_index"]), str(p["file_object_id"]), int(p["part_offset"]), int(p["part_size"])),
            )
    return {"version_id": version_id, "version_no": version_no, "created_at": created}


def apply_uploaded_item_as_new_current(cur, target_item_id: str, uploaded_item_id: str, uid: str, *, keep_name: Optional[str] = None) -> Dict[str, Any]:
    target = _fetch_item(cur, target_item_id, uid, allow_trashed=False)
    uploaded = _fetch_item(cur, uploaded_item_id, uid, allow_trashed=False)
    snapshot_item_version(cur, target_item_id, uid, source="replace")

    next_name = keep_name if keep_name is not None else uploaded["name"]
    now = int(now_ts())
    cur.execute("DELETE FROM item_parts WHERE item_id=%s", (target_item_id,))
    if uploaded.get("file_object_id") is None:
        cur.execute(
            """
            SELECT part_index,file_object_id,part_offset,part_size
            FROM item_parts WHERE item_id=%s ORDER BY part_index ASC
            """,
            (uploaded_item_id,),
        )
        parts = [dict(r) for r in cur.fetchall()]
        for p in parts:
            cur.execute(
                """
                INSERT INTO item_parts(item_id,part_index,file_object_id,part_offset,part_size)
                VALUES (%s,%s,%s,%s,%s)
                """,
                (target_item_id, int(p["part_index"]), str(p["file_object_id"]), int(p["part_offset"]), int(p["part_size"])),
            )

    cur.execute(
        """
        UPDATE items
        SET name=%s, size_bytes=%s, file_object_id=%s, updated_at=%s, trashed_at=NULL, trash_batch_id=NULL
        WHERE item_id=%s AND owner_user_id=%s
        RETURNING item_id,type,parent_id,name,size_bytes,file_object_id,created_at,updated_at,trashed_at,trash_batch_id,owner_user_id
        """,
        (
            next_name,
            int(uploaded.get("size_bytes") or 0),
            uploaded.get("file_object_id"),
            now,
            target_item_id,
            uid,
        ),
    )
    merged = dict(cur.fetchone())
    cur.execute("DELETE FROM item_parts WHERE item_id=%s", (uploaded_item_id,))
    cur.execute("DELETE FROM items WHERE item_id=%s AND owner_user_id=%s", (uploaded_item_id, uid))
    return merged


def _sort_sql(sort: str) -> Tuple[str, str]:
    sort_key, _, sort_dir = sort.partition(":")
    allowed_cols = {
        "name": "name",
        "updated_at": "updated_at",
        "size_bytes": "size_bytes",
        "trashed_at": "trashed_at",
    }
    return allowed_cols.get(sort_key, "updated_at"), ("ASC" if sort_dir.lower() == "asc" else "DESC")




def _backup_target_remote_item_ids(cur, uid: str) -> set[str]:
    try:
        cur.execute(
            """
            SELECT remote_item_id
            FROM backup_targets
            WHERE user_id=%s
              AND status='active'
              AND remote_item_id IS NOT NULL
              AND remote_item_id <> ''
            """,
            (uid,),
        )
        return {str(row["remote_item_id"]) for row in cur.fetchall() if row.get("remote_item_id")}
    except Exception:
        return set()


def _item_under_any_backup_target(cur, item_id: str, backup_root_ids: set[str]) -> bool:
    if not backup_root_ids:
        return False
    current = str(item_id)
    guard = 0
    while current and current != ROOT_ID and guard < 128:
        if current in backup_root_ids:
            return True
        cur.execute("SELECT parent_id FROM items WHERE item_id=%s", (current,))
        row = cur.fetchone()
        if not row:
            return False
        current = str(row.get("parent_id") or ROOT_ID)
        guard += 1
    return False


def _filter_backup_namespace_items(cur, uid: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    backup_root_ids = _backup_target_remote_item_ids(cur, uid)
    if not backup_root_ids:
        return items
    return [item for item in items if not _item_under_any_backup_target(cur, str(item["item_id"]), backup_root_ids)]

# ---------- list / search ----------
@router.get("/items", response_model=ListOut)
def list_items(parent_id: Optional[str] = None, sort: str = Query(default="updated_at:desc"), uid: str = Depends(current_user_id)) -> ListOut:
    target_parent = _normalize_parent_id(parent_id)
    sort_col, sort_order = _sort_sql(sort)
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if target_parent != ROOT_ID:
                _assert_folder_owner(cur, uid, target_parent)
            cur.execute(
                f"""
                SELECT i.item_id,i.type,i.parent_id,i.name,i.size_bytes,i.file_object_id,
                       i.created_at,i.updated_at,i.trashed_at,i.trash_batch_id,i.owner_user_id,
                       COALESCE(v.version_count, 0) AS version_count
                FROM items i
                LEFT JOIN (
                    SELECT item_id, COUNT(*) AS version_count
                    FROM item_versions
                    GROUP BY item_id
                ) v ON v.item_id = i.item_id
                WHERE i.owner_user_id=%s
                  AND COALESCE(i.parent_id, %s) = %s
                  AND i.trashed_at IS NULL
                ORDER BY CASE WHEN i.type='folder' THEN 0 ELSE 1 END, {sort_col} {sort_order}, i.name ASC
                """,
                (uid, ROOT_ID, target_parent),
            )
            items = _filter_backup_namespace_items(cur, uid, [dict(row) for row in cur.fetchall()])
            parent = None if target_parent == ROOT_ID else _fetch_item(cur, target_parent, uid, allow_trashed=False)
            breadcrumbs = _build_breadcrumbs(cur, uid, target_parent)
            return ListOut(items=items, parent=parent, breadcrumbs=breadcrumbs)


@router.get("/items/recent", response_model=ListOut)
def list_recent_items(uid: str = Depends(current_user_id)) -> ListOut:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT i.item_id,i.type,i.parent_id,i.name,i.size_bytes,i.file_object_id,
                       i.created_at,i.updated_at,i.trashed_at,i.trash_batch_id,i.owner_user_id,
                       COALESCE(v.version_count, 0) AS version_count
                FROM items i
                LEFT JOIN (
                    SELECT item_id, COUNT(*) AS version_count
                    FROM item_versions
                    GROUP BY item_id
                ) v ON v.item_id = i.item_id
                WHERE i.owner_user_id=%s AND i.trashed_at IS NULL
                ORDER BY i.updated_at DESC NULLS LAST, i.created_at DESC
                LIMIT 50
                """,
                (uid,),
            )
            return ListOut(items=[dict(row) for row in cur.fetchall()], parent=None, breadcrumbs=[])


@router.get("/items/shared", response_model=ListOut)
def list_shared_items(uid: str = Depends(current_user_id)) -> ListOut:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT DISTINCT i.item_id,i.type,i.parent_id,i.name,i.size_bytes,i.file_object_id,
                       i.created_at,i.updated_at,i.trashed_at,i.trash_batch_id,i.owner_user_id,
                       COALESCE(v.version_count, 0) AS version_count
                FROM shares s
                JOIN items i ON i.item_id = s.item_id
                LEFT JOIN (
                    SELECT item_id, COUNT(*) AS version_count
                    FROM item_versions
                    GROUP BY item_id
                ) v ON v.item_id = i.item_id
                WHERE s.owner_user_id=%s
                  AND s.revoked_at IS NULL
                  AND (s.expires_at IS NULL OR s.expires_at > %s)
                  AND i.trashed_at IS NULL
                ORDER BY i.updated_at DESC NULLS LAST
                LIMIT 100
                """,
                (uid, int(now_ts())),
            )
            return ListOut(items=[dict(row) for row in cur.fetchall()], parent=None, breadcrumbs=[])


@router.get("/search", response_model=SearchOut)
def search_items(q: str, parent_id: Optional[str] = None, sort: str = Query(default="updated_at:desc"), uid: str = Depends(current_user_id)) -> SearchOut:
    query = q.strip()
    if not query:
        return SearchOut(items=[], q=q, total=0)
    target_parent = _normalize_parent_id(parent_id)
    sort_col, sort_order = _sort_sql(sort)
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            params: List[Any] = [uid, f"%{query}%"]
            where_parent = ""
            if target_parent != ROOT_ID:
                _assert_folder_owner(cur, uid, target_parent)
                where_parent = "AND COALESCE(i.parent_id, %s) = %s"
                params.extend([ROOT_ID, target_parent])
            cur.execute(
                f"""
                SELECT i.item_id,i.type,i.parent_id,i.name,i.size_bytes,i.file_object_id,
                       i.created_at,i.updated_at,i.trashed_at,i.trash_batch_id,i.owner_user_id,
                       COALESCE(v.version_count, 0) AS version_count
                FROM items i
                LEFT JOIN (
                    SELECT item_id, COUNT(*) AS version_count
                    FROM item_versions
                    GROUP BY item_id
                ) v ON v.item_id = i.item_id
                WHERE i.owner_user_id=%s
                  AND i.trashed_at IS NULL
                  AND i.name ILIKE %s
                  {where_parent}
                ORDER BY CASE WHEN i.type='folder' THEN 0 ELSE 1 END, {sort_col} {sort_order}, i.name ASC
                LIMIT 200
                """,
                params,
            )
            items = [dict(r) for r in cur.fetchall()]
            return SearchOut(items=items, q=q, total=len(items))


# ---------- create / update ----------
@router.post("/items/folder")
def create_folder(inp: FolderCreateIn, uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    name = inp.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="folder name required")
    item_id = str(uuid.uuid4())
    created = int(now_ts())
    parent_id = _normalize_parent_id(inp.parent_id)
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if parent_id != ROOT_ID:
                _assert_folder_owner(cur, uid, parent_id)
            cur.execute(
                """
                INSERT INTO items(item_id,type,parent_id,name,size_bytes,file_object_id,created_at,updated_at,trashed_at,trash_batch_id,owner_user_id)
                VALUES (%s,'folder',%s,%s,0,NULL,%s,%s,NULL,NULL,%s)
                RETURNING item_id,type,parent_id,name,size_bytes,file_object_id,created_at,updated_at,trashed_at,trash_batch_id,owner_user_id
                """,
                (item_id, parent_id, name, created, created, uid),
            )
            row = dict(cur.fetchone())
        conn.commit()
    row["version_count"] = 0
    return row


@router.patch("/items/{item_id}")
def patch_item(item_id: str, inp: ItemPatchIn, uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    if inp.name is None and inp.parent_id is None:
        raise HTTPException(status_code=400, detail="nothing to update")
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            item = _fetch_item(cur, item_id, uid, allow_trashed=False)
            next_name = inp.name.strip() if inp.name is not None else item["name"]
            next_parent = _normalize_parent_id(inp.parent_id) if inp.parent_id is not None else item["parent_id"]
            if next_parent and next_parent != ROOT_ID:
                _assert_folder_owner(cur, uid, str(next_parent))
                if str(next_parent) == item_id:
                    raise HTTPException(status_code=400, detail="item cannot move to itself")
            cur.execute(
                """
                UPDATE items
                SET name=%s, parent_id=%s, updated_at=%s
                WHERE item_id=%s AND owner_user_id=%s
                RETURNING item_id,type,parent_id,name,size_bytes,file_object_id,created_at,updated_at,trashed_at,trash_batch_id,owner_user_id
                """,
                (next_name, next_parent, int(now_ts()), item_id, uid),
            )
            updated = dict(cur.fetchone())
        conn.commit()
    updated["version_count"] = item.get("version_count", 0)
    return updated


# ---------- trash / restore ----------
@router.get("/trash/items", response_model=ListOut)
def list_trashed_items(sort: str = Query(default="trashed_at:desc"), uid: str = Depends(current_user_id)) -> ListOut:
    sort_col, sort_order = _sort_sql(sort)
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT i.item_id,i.type,i.parent_id,i.name,i.size_bytes,i.file_object_id,
                       i.created_at,i.updated_at,i.trashed_at,i.trash_batch_id,i.owner_user_id,
                       COALESCE(v.version_count, 0) AS version_count
                FROM items i
                LEFT JOIN (
                    SELECT item_id, COUNT(*) AS version_count
                    FROM item_versions
                    GROUP BY item_id
                ) v ON v.item_id = i.item_id
                WHERE i.owner_user_id=%s
                  AND i.trashed_at IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM items p
                      WHERE p.owner_user_id=i.owner_user_id
                        AND p.item_id=i.parent_id
                        AND p.trashed_at IS NOT NULL
                  )
                ORDER BY {sort_col} {sort_order}, i.name ASC
                LIMIT 200
                """,
                (uid,),
            )
            return ListOut(items=[dict(r) for r in cur.fetchall()], parent=None, breadcrumbs=[])


@router.post("/items/{item_id}/trash")
def trash_item(item_id: str, uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    trashed_at = int(now_ts())
    batch_id = str(uuid.uuid4())
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            root = _fetch_item(cur, item_id, uid, allow_trashed=False)
            nodes = _collect_descendants(cur, uid, item_id)
            ids = [n["item_id"] for n in nodes]
            cur.execute(
                """
                UPDATE items
                SET trashed_at=%s, trash_batch_id=%s, updated_at=%s
                WHERE owner_user_id=%s AND item_id = ANY(%s)
                """,
                (trashed_at, batch_id, trashed_at, uid, ids),
            )
        conn.commit()
    root["trashed_at"] = trashed_at
    root["trash_batch_id"] = batch_id
    return root


@router.post("/items/{item_id}/restore")
def restore_item(item_id: str, uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            root = _fetch_item(cur, item_id, uid, allow_trashed=True)
            if root.get("trashed_at") is None:
                return root
            batch_id = root.get("trash_batch_id")
            ids: List[str] = [item_id]
            if batch_id:
                cur.execute(
                    "SELECT item_id FROM items WHERE owner_user_id=%s AND trash_batch_id=%s",
                    (uid, batch_id),
                )
                ids = [str(r["item_id"]) for r in cur.fetchall()]

            root_parent = root.get("parent_id")
            if root_parent and root_parent != ROOT_ID:
                cur.execute("SELECT trashed_at FROM items WHERE item_id=%s AND owner_user_id=%s", (root_parent, uid))
                parent_row = cur.fetchone()
                if parent_row and parent_row["trashed_at"] is not None:
                    cur.execute("UPDATE items SET parent_id=%s WHERE item_id=%s AND owner_user_id=%s", (ROOT_ID, item_id, uid))

            cur.execute(
                """
                UPDATE items
                SET trashed_at=NULL, trash_batch_id=NULL, updated_at=%s
                WHERE owner_user_id=%s AND item_id = ANY(%s)
                RETURNING item_id,type,parent_id,name,size_bytes,file_object_id,created_at,updated_at,trashed_at,trash_batch_id,owner_user_id
                """,
                (int(now_ts()), uid, ids),
            )
            restored_rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
    for r in restored_rows:
        if r["item_id"] == item_id:
            r["version_count"] = root.get("version_count", 0)
            return r
    return root


@router.delete("/items/{item_id}/purge")
def purge_item(item_id: str, uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            root = _fetch_item(cur, item_id, uid, allow_trashed=True)
            if root.get("trashed_at") is None:
                raise HTTPException(status_code=400, detail="only trashed items can be purged")
            nodes = _collect_descendants(cur, uid, item_id)
            ids = [str(n["item_id"]) for n in nodes]
            candidate_file_object_ids = collect_file_object_ids_for_items(cur, ids)
            cur.execute("SELECT version_id FROM item_versions WHERE item_id = ANY(%s)", (ids,))
            version_ids = [str(row["version_id"]) for row in cur.fetchall()]
            if version_ids:
                cur.execute("DELETE FROM item_version_parts WHERE version_id = ANY(%s)", (version_ids,))
            cur.execute("DELETE FROM item_versions WHERE item_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM item_parts WHERE item_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM items WHERE owner_user_id=%s AND item_id = ANY(%s)", (uid, ids))
            gc_result = gc_unreferenced_objects(
                cur,
                candidate_file_object_ids,
                reason="manual_item_purge",
            )
        conn.commit()
    return {"purged_item_id": item_id, "count": len(ids), "object_gc": gc_result}


# ---------- versions ----------
@router.get("/items/{item_id}/versions", response_model=VersionsOut)
def list_versions(item_id: str, uid: str = Depends(current_user_id)) -> VersionsOut:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            item = _fetch_item(cur, item_id, uid, allow_trashed=True)
            cur.execute(
                """
                SELECT v.version_id,v.version_no,v.file_object_id,v.name,v.size_bytes,v.created_at,
                       v.created_by_user_id,v.source,v.restore_from_version_id,
                       COALESCE(p.part_count, 0) AS part_count
                FROM item_versions v
                LEFT JOIN (
                    SELECT version_id, COUNT(*) AS part_count
                    FROM item_version_parts
                    GROUP BY version_id
                ) p ON p.version_id = v.version_id
                WHERE v.item_id=%s
                ORDER BY v.version_no DESC, v.created_at DESC
                """,
                (item_id,),
            )
            versions = [dict(r) for r in cur.fetchall()]
            current_entry = {
                "version_id": "current",
                "version_no": None,
                "file_object_id": item.get("file_object_id"),
                "name": item["name"],
                "size_bytes": item.get("size_bytes") or 0,
                "created_at": item.get("updated_at") or item.get("created_at"),
                "created_by_user_id": uid,
                "source": "current",
                "restore_from_version_id": None,
                "part_count": 0,
                "is_current": True,
            }
            if not item.get("file_object_id"):
                cur.execute("SELECT COUNT(*) FROM item_parts WHERE item_id=%s", (item_id,))
                current_entry["part_count"] = int(cur.fetchone()[0])
            for v in versions:
                v["is_current"] = False
            return VersionsOut(item=item, versions=[current_entry] + versions)


@router.post("/items/{item_id}/versions/{version_id}/restore")
def restore_version(item_id: str, version_id: str, uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    if version_id == "current":
        raise HTTPException(status_code=400, detail="current cannot be restored")
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            item = _fetch_item(cur, item_id, uid, allow_trashed=False)
            cur.execute(
                """
                SELECT version_id,version_no,file_object_id,name,size_bytes,created_at,source,restore_from_version_id
                FROM item_versions
                WHERE item_id=%s AND version_id=%s
                """,
                (item_id, version_id),
            )
            version = cur.fetchone()
            if not version:
                raise HTTPException(status_code=404, detail="version not found")
            snapshot_item_version(cur, item_id, uid, source="before_restore", restore_from_version_id=version_id)
            cur.execute("DELETE FROM item_parts WHERE item_id=%s", (item_id,))
            if version["file_object_id"] is None:
                cur.execute(
                    """
                    SELECT part_index,file_object_id,part_offset,part_size
                    FROM item_version_parts
                    WHERE version_id=%s
                    ORDER BY part_index ASC
                    """,
                    (version_id,),
                )
                for p in cur.fetchall():
                    cur.execute(
                        """
                        INSERT INTO item_parts(item_id,part_index,file_object_id,part_offset,part_size)
                        VALUES (%s,%s,%s,%s,%s)
                        """,
                        (item_id, int(p["part_index"]), str(p["file_object_id"]), int(p["part_offset"]), int(p["part_size"])),
                    )
            cur.execute(
                """
                UPDATE items
                SET file_object_id=%s, name=%s, size_bytes=%s, updated_at=%s
                WHERE item_id=%s AND owner_user_id=%s
                RETURNING item_id,type,parent_id,name,size_bytes,file_object_id,created_at,updated_at,trashed_at,trash_batch_id,owner_user_id
                """,
                (
                    version["file_object_id"],
                    str(version["name"]),
                    int(version["size_bytes"] or 0),
                    int(now_ts()),
                    item_id,
                    uid,
                ),
            )
            restored = dict(cur.fetchone())
        conn.commit()
    restored["version_count"] = item.get("version_count", 0) + 1
    return restored


# ---------- sync endpoints ----------
@router.get("/sync/tree")
def sync_tree(uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT item_id,type,parent_id,name,size_bytes,file_object_id,created_at,updated_at,owner_user_id
                FROM items
                WHERE owner_user_id=%s AND trashed_at IS NULL
                ORDER BY created_at ASC
                """,
                (uid,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            by_id = {r["item_id"]: r for r in rows}
            children: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for row in rows:
                children[str(row.get("parent_id") or ROOT_ID)].append(row)

            memo: Dict[str, str] = {}

            def build_path(item: Dict[str, Any]) -> str:
                iid = str(item["item_id"])
                if iid in memo:
                    return memo[iid]
                pid = str(item.get("parent_id") or ROOT_ID)
                if pid == ROOT_ID or pid not in by_id:
                    memo[iid] = item["name"]
                    return memo[iid]
                base = build_path(by_id[pid])
                memo[iid] = f"{base}/{item['name']}"
                return memo[iid]

            items = []
            for row in rows:
                row["path"] = build_path(row)
                row["version_count"] = 0
                items.append(row)

            cur.execute("SELECT item_id, COUNT(*) AS c FROM item_versions GROUP BY item_id")
            counts = {str(r["item_id"]): int(r["c"]) for r in cur.fetchall()}
            for item in items:
                item["version_count"] = counts.get(str(item["item_id"]), 0)

            cur.execute(
                """
                SELECT user_id,local_root_display,sync_mode,polling_interval_sec,ignore_hidden,created_at,updated_at
                FROM sync_profiles WHERE user_id=%s
                """,
                (uid,),
            )
            profile = dict(cur.fetchone()) if cur.rowcount else None
    return {"items": items, "profile": profile}


@router.get("/sync/profile")
def get_sync_profile(uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT user_id,local_root_display,sync_mode,polling_interval_sec,ignore_hidden,created_at,updated_at
                FROM sync_profiles WHERE user_id=%s
                """,
                (uid,),
            )
            row = cur.fetchone()
            profile = dict(row) if row else {
                "user_id": uid,
                "local_root_display": "~/Phase1 Drive",
                "sync_mode": "mirror",
                "polling_interval_sec": 5,
                "ignore_hidden": True,
                "created_at": None,
                "updated_at": None,
            }

            cur.execute(
                """
                SELECT client_id,user_id,local_root_display,status,sync_mode,pending_changes,app_version,last_seen,last_sync_at
                FROM sync_clients
                WHERE user_id=%s
                ORDER BY last_seen DESC
                LIMIT 5
                """,
                (uid,),
            )
            clients = [dict(r) for r in cur.fetchall()]
    return {"profile": profile, "clients": clients}


@router.post("/sync/profile")
def save_sync_profile(inp: SyncProfileIn, uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    created = int(now_ts())
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO sync_profiles(user_id,local_root_display,sync_mode,polling_interval_sec,ignore_hidden,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (user_id) DO UPDATE SET
                  local_root_display=EXCLUDED.local_root_display,
                  sync_mode=EXCLUDED.sync_mode,
                  polling_interval_sec=EXCLUDED.polling_interval_sec,
                  ignore_hidden=EXCLUDED.ignore_hidden,
                  updated_at=EXCLUDED.updated_at
                RETURNING user_id,local_root_display,sync_mode,polling_interval_sec,ignore_hidden,created_at,updated_at
                """,
                (uid, inp.local_root_display, inp.sync_mode, inp.polling_interval_sec, inp.ignore_hidden, created, created),
            )
            row = dict(cur.fetchone())
        conn.commit()
    return row


@router.post("/sync/heartbeat")
def sync_heartbeat(inp: SyncHeartbeatIn, uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    seen = int(now_ts())
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO sync_clients(client_id,user_id,local_root_display,status,sync_mode,pending_changes,app_version,last_seen,last_sync_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (client_id) DO UPDATE SET
                  user_id=EXCLUDED.user_id,
                  local_root_display=EXCLUDED.local_root_display,
                  status=EXCLUDED.status,
                  sync_mode=EXCLUDED.sync_mode,
                  pending_changes=EXCLUDED.pending_changes,
                  app_version=EXCLUDED.app_version,
                  last_seen=EXCLUDED.last_seen,
                  last_sync_at=EXCLUDED.last_sync_at
                RETURNING client_id,user_id,local_root_display,status,sync_mode,pending_changes,app_version,last_seen,last_sync_at
                """,
                (inp.client_id, uid, inp.local_root_display, inp.status, inp.sync_mode, inp.pending_changes, inp.app_version, seen, inp.last_sync_at),
            )
            row = dict(cur.fetchone())
        conn.commit()
    return row


# ---------- sync download helpers ----------
@router.get("/items/{item_id}/download_manifest")
def download_manifest(item_id: str, uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            item = _fetch_item(cur, item_id, uid, allow_trashed=False)
            manifest = {
                "item_id": item_id,
                "name": item["name"],
                "size_bytes": int(item.get("size_bytes") or 0),
                "file_object_id": item.get("file_object_id"),
                "parts": [],
            }
            if not item.get("file_object_id"):
                cur.execute(
                    """
                    SELECT part_index,file_object_id,part_offset,part_size
                    FROM item_parts
                    WHERE item_id=%s
                    ORDER BY part_index ASC
                    """,
                    (item_id,),
                )
                manifest["parts"] = [dict(r) for r in cur.fetchall()]
            manifest["is_multipart"] = len(manifest["parts"]) > 0
            return manifest


@router.post("/objects/{file_object_id}/download_token")
def object_download_token(file_object_id: str, uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    created = int(now_ts())
    expires_at = created + 600
    token = uuid.uuid4().hex
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT 1
                FROM objects o
                WHERE o.file_object_id=%s AND o.owner_user_id=%s
                """,
                (file_object_id, uid),
            )
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="object not found or forbidden")
            cur.execute(
                """
                INSERT INTO download_tokens(token,file_object_id,owner_user_id,charge_user_id,is_shared,expires_at,created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (token, file_object_id, uid, uid, False, expires_at, created),
            )
        conn.commit()
    return {"download_token": token, "expires_at": expires_at, "file_object_id": file_object_id}
