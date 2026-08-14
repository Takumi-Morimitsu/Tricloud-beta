# -*- coding: utf-8 -*-
"""
phase5_library_patch_share_email_integrated.py

追加内容:
- Home / Folder / Shared / Recent のライブラリ系ビュー
- 最近使用したアイテムを「更新日時」ではなく「ユーザーが開いた日時」で記録
- 他ユーザーから共有された項目の受け皿(shared_item_inbox)
- 共有受信アイテムの一覧 / 子階層表示 / ダウンロード token
- share_id を自分の共有アイテム一覧へ追加する claim API
- メールアドレス指定の複数人共有 API
- 共有時の任意メッセージ保存

方針:
- 既存 items / shares / download_tokens を壊さずに薄く追加する
- 最近使用は recent_item_opens で管理する
- 共有受信は shared_item_inbox を介して明示的に管理する
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr
from psycopg.rows import dict_row

from admin_controls import AdminControlsUnavailable, restriction_for_request
from meta_db_pg import db_conn, now_ts
from items_phase2_patch import ROOT_ID, current_user_id

router = APIRouter(tags=["phase5-library"])

DDL = [
    """
    CREATE TABLE IF NOT EXISTS recent_item_opens (
        user_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        opened_at INTEGER NOT NULL,
        PRIMARY KEY(user_id, item_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_recent_item_opens_user_opened ON recent_item_opens(user_id, opened_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS shared_item_inbox (
        user_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        owner_user_id TEXT NOT NULL,
        share_id TEXT,
        role TEXT NOT NULL DEFAULT 'viewer',
        added_at INTEGER NOT NULL,
        message TEXT,
        PRIMARY KEY(user_id, item_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_shared_item_inbox_user_added ON shared_item_inbox(user_id, added_at DESC)",
    "ALTER TABLE shared_item_inbox ADD COLUMN IF NOT EXISTS message TEXT",
]


def init_phase5_library_schema() -> None:
    """Phase5 library / shared inbox / recent-open 用の追加テーブルを作成する。

    メインアプリ側が FastAPI lifespan を使うため、router の startup event には依存しない。
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            for stmt in DDL:
                cur.execute(stmt)
        conn.commit()


class ListOut(BaseModel):
    items: List[Dict[str, Any]]
    parent: Optional[Dict[str, Any]] = None
    breadcrumbs: List[Dict[str, Any]] = []


class SearchOut(BaseModel):
    items: List[Dict[str, Any]]
    q: str
    total: int


class ClaimShareIn(BaseModel):
    share_id: str


class ShareSendByEmailIn(BaseModel):
    item_id: str
    recipient_email: Optional[EmailStr] = None
    recipient_emails: List[EmailStr] = []
    role: str = "viewer"
    message: Optional[str] = None


class OpenOut(BaseModel):
    item_id: str
    opened_at: int


class DownloadTokenOut(BaseModel):
    download_token: str
    expires_at: int
    file_object_id: str
    charge_user_id: str
    is_shared: bool


_VERSION_JOIN = """
LEFT JOIN (
    SELECT item_id, COUNT(*) AS version_count
    FROM item_versions
    GROUP BY item_id
) v ON v.item_id = i.item_id
"""


def _normalize_parent_id(parent_id: Optional[str]) -> str:
    return str(parent_id or ROOT_ID)


def _normalize_email(email: str) -> str:
    value = str(email or "").strip().lower()
    if not value:
        raise HTTPException(status_code=400, detail="recipient_email required")
    return value


def _normalize_email_list(recipient_email: Optional[str], recipient_emails: Optional[List[str]]) -> List[str]:
    values: List[str] = []
    if recipient_email:
        values.append(_normalize_email(str(recipient_email)))
    for value in recipient_emails or []:
        normalized = _normalize_email(str(value))
        if normalized not in values:
            values.append(normalized)
    if not values:
        raise HTTPException(status_code=400, detail="recipient_email required")
    return values


def _ensure_role_value(role: str) -> str:
    value = str(role or "").strip().lower()
    if value not in {"viewer", "editor"}:
        raise HTTPException(status_code=400, detail="role must be 'viewer' or 'editor'")
    return value


def _normalize_share_message(message: Optional[str]) -> Optional[str]:
    value = str(message or "").strip()
    if not value:
        return None
    if len(value) > 1000:
        raise HTTPException(status_code=400, detail="message must be 1000 characters or fewer")
    return value


def _sort_key(sort: str) -> Tuple[str, str]:
    key, _, direction = sort.partition(":")
    if key not in {"name", "updated_at", "size_bytes", "trashed_at", "opened_at"}:
        key = "updated_at"
    direction = "asc" if direction.lower() == "asc" else "desc"
    return key, direction


def _sort_items(items: List[Dict[str, Any]], sort: str, *, keep_folder_first: bool = True) -> List[Dict[str, Any]]:
    key, direction = _sort_key(sort)
    reverse = direction == "desc"

    def sort_value(row: Dict[str, Any]):
        if key == "name":
            return str(row.get("name") or "")
        return int(row.get(key) or 0)

    data = list(items)
    if keep_folder_first:
        data.sort(key=lambda r: (0 if r.get("type") == "folder" else 1,))
        data.sort(key=sort_value, reverse=reverse)
        data.sort(key=lambda r: 0 if r.get("type") == "folder" else 1)
        return data
    data.sort(key=sort_value, reverse=reverse)
    return data


def _fetch_item_any(cur, item_id: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        f"""
        SELECT i.item_id,i.type,i.parent_id,i.name,i.size_bytes,i.file_object_id,
               i.created_at,i.updated_at,i.trashed_at,i.trash_batch_id,i.owner_user_id,
               COALESCE(v.version_count, 0) AS version_count
        FROM items i
        {_VERSION_JOIN}
        WHERE i.item_id=%s
        """,
        (item_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _fetch_owned_items(cur, uid: str) -> List[Dict[str, Any]]:
    cur.execute(
        f"""
        SELECT i.item_id,i.type,i.parent_id,i.name,i.size_bytes,i.file_object_id,
               i.created_at,i.updated_at,i.trashed_at,i.trash_batch_id,i.owner_user_id,
               COALESCE(v.version_count, 0) AS version_count
        FROM items i
        {_VERSION_JOIN}
        WHERE i.owner_user_id=%s AND i.trashed_at IS NULL
        """,
        (uid,),
    )
    return [dict(r) for r in cur.fetchall()]




def _fetch_owned_root_items(cur, uid: str) -> List[Dict[str, Any]]:
    """Return only top-level owned items for Explorer/Drive-like root listing.

    _fetch_owned_items() intentionally still returns all owned items because search,
    recent-related logic, and permission checks may need the full tree.  The root
    screens (/library/home and /library/owned) should not flatten children of
    uploaded folders into the first view.
    """
    cur.execute(
        f"""
        SELECT i.item_id,i.type,i.parent_id,i.name,i.size_bytes,i.file_object_id,
               i.created_at,i.updated_at,i.trashed_at,i.trash_batch_id,i.owner_user_id,
               COALESCE(v.version_count, 0) AS version_count
        FROM items i
        {_VERSION_JOIN}
        WHERE i.owner_user_id=%s
          AND COALESCE(i.parent_id, %s)=%s
          AND i.trashed_at IS NULL
        """,
        (uid, ROOT_ID, ROOT_ID),
    )
    return [dict(r) for r in cur.fetchall()]


def _fetch_shared_received_roots(cur, uid: str) -> List[Dict[str, Any]]:
    cur.execute(
        f"""
        SELECT i.item_id,i.type,i.parent_id,i.name,i.size_bytes,i.file_object_id,
               i.created_at,i.updated_at,i.trashed_at,i.trash_batch_id,i.owner_user_id,
               COALESCE(v.version_count, 0) AS version_count,
               si.added_at,
               si.message AS share_message
        FROM shared_item_inbox si
        JOIN items i ON i.item_id = si.item_id
        {_VERSION_JOIN}
        WHERE si.user_id=%s
          AND i.trashed_at IS NULL
          AND i.owner_user_id <> %s
        """,
        (uid, uid),
    )
    return [dict(r) for r in cur.fetchall()]


def _find_shared_root_for_item(cur, uid: str, item_id: str) -> Optional[str]:
    current = item_id
    guard = 0
    while current and current != ROOT_ID and guard < 64:
        cur.execute("SELECT 1 FROM shared_item_inbox WHERE user_id=%s AND item_id=%s", (uid, current))
        if cur.fetchone():
            return str(current)
        cur.execute("SELECT parent_id FROM items WHERE item_id=%s", (current,))
        row = cur.fetchone()
        if not row:
            break
        current = str(row["parent_id"] or ROOT_ID)
        guard += 1
    return None


def _can_access_item(cur, uid: str, item_id: str) -> Tuple[Optional[Dict[str, Any]], str]:
    item = _fetch_item_any(cur, item_id)
    if not item or item.get("trashed_at") is not None:
        return None, "none"
    if str(item.get("owner_user_id")) == uid:
        return item, "owned"
    if _find_shared_root_for_item(cur, uid, item_id):
        return item, "shared"
    return None, "none"


def _shared_breadcrumbs(cur, uid: str, parent_id: str) -> List[Dict[str, Any]]:
    root_id = _find_shared_root_for_item(cur, uid, parent_id)
    if not root_id:
        return []
    crumbs: List[Dict[str, Any]] = []
    current = parent_id
    guard = 0
    while current and current != ROOT_ID and guard < 64:
        row = _fetch_item_any(cur, current)
        if not row:
            break
        crumbs.append({
            "item_id": row["item_id"],
            "name": row["name"],
            "parent_id": row["parent_id"],
            "type": row["type"],
            "owner_user_id": row["owner_user_id"],
        })
        if current == root_id:
            break
        current = str(row.get("parent_id") or ROOT_ID)
        guard += 1
    crumbs.reverse()
    return crumbs


def _fetch_shared_children(cur, uid: str, parent_id: str) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    parent, access_kind = _can_access_item(cur, uid, parent_id)
    if not parent or access_kind != "shared":
        raise HTTPException(status_code=403, detail="shared folder access denied")
    if parent.get("type") != "folder":
        raise HTTPException(status_code=400, detail="parent must be folder")
    cur.execute(
        f"""
        SELECT i.item_id,i.type,i.parent_id,i.name,i.size_bytes,i.file_object_id,
               i.created_at,i.updated_at,i.trashed_at,i.trash_batch_id,i.owner_user_id,
               COALESCE(v.version_count, 0) AS version_count
        FROM items i
        {_VERSION_JOIN}
        WHERE i.owner_user_id=%s
          AND COALESCE(i.parent_id, %s)=%s
          AND i.trashed_at IS NULL
        """,
        (parent["owner_user_id"], ROOT_ID, parent_id),
    )
    children = [dict(r) for r in cur.fetchall()]
    return children, parent, _shared_breadcrumbs(cur, uid, parent_id)


def _merge_unique(*groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for group in groups:
        for item in group:
            merged[str(item["item_id"])] = dict(item)
    return list(merged.values())


def _backup_target_remote_item_ids(cur, uid: str) -> set[str]:
    """Return item_ids that belong to the user's backup namespace.

    backup_targets is optional in older/dev databases, so this helper is
    defensive.  If the table/column is not available yet, it simply returns an
    empty set and normal listings continue to work.
    """
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


def _sharing_disabled(uid: str) -> bool:
    """Return whether received-share data must be hidden for this user."""
    try:
        return restriction_for_request(uid, "/shared") == "sharing_disabled"
    except AdminControlsUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="account controls are temporarily unavailable",
        ) from exc


def _fetch_recent_opened(
    cur,
    uid: str,
    *,
    include_shared: bool = True,
) -> List[Dict[str, Any]]:
    cur.execute(
        f"""
        SELECT rio.item_id, rio.opened_at,
               i.item_id AS i_item_id,i.type,i.parent_id,i.name,i.size_bytes,i.file_object_id,
               i.created_at,i.updated_at,i.trashed_at,i.trash_batch_id,i.owner_user_id,
               COALESCE(v.version_count, 0) AS version_count
        FROM recent_item_opens rio
        JOIN items i ON i.item_id = rio.item_id
        {_VERSION_JOIN}
        WHERE rio.user_id=%s AND i.trashed_at IS NULL
        ORDER BY rio.opened_at DESC
        LIMIT 200
        """,
        (uid,),
    )
    results: List[Dict[str, Any]] = []
    for row in cur.fetchall():
        record = dict(row)
        item_id = str(record["item_id"])
        item, access_kind = _can_access_item(cur, uid, item_id)
        if not item or access_kind == "none":
            continue
        if access_kind == "shared" and not include_shared:
            continue
        item["opened_at"] = int(record.get("opened_at") or 0)
        results.append(item)
    return results


@router.get("/library/home", response_model=ListOut)
def list_home(sort: str = Query(default="updated_at:desc"), uid: str = Depends(current_user_id)) -> ListOut:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            owned = _filter_backup_namespace_items(cur, uid, _fetch_owned_root_items(cur, uid))
            items = owned if _sharing_disabled(uid) else _merge_unique(owned, _fetch_shared_received_roots(cur, uid))
    return ListOut(items=_sort_items(items, sort), parent=None, breadcrumbs=[])


@router.get("/library/owned", response_model=ListOut)
def list_owned(sort: str = Query(default="updated_at:desc"), uid: str = Depends(current_user_id)) -> ListOut:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            items = _filter_backup_namespace_items(cur, uid, _fetch_owned_root_items(cur, uid))
    return ListOut(items=_sort_items(items, sort), parent=None, breadcrumbs=[])


@router.get("/library/shared_received", response_model=ListOut)
def list_shared_received(sort: str = Query(default="updated_at:desc"), uid: str = Depends(current_user_id)) -> ListOut:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            items = _fetch_shared_received_roots(cur, uid)
    return ListOut(items=_sort_items(items, sort), parent=None, breadcrumbs=[])


@router.get("/library/shared_children", response_model=ListOut)
def list_shared_children(parent_id: str, sort: str = Query(default="updated_at:desc"), uid: str = Depends(current_user_id)) -> ListOut:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            items, parent, breadcrumbs = _fetch_shared_children(cur, uid, parent_id)
    return ListOut(items=_sort_items(items, sort), parent=parent, breadcrumbs=breadcrumbs)


@router.get("/library/recent_opened", response_model=ListOut)
def list_recent_opened(uid: str = Depends(current_user_id)) -> ListOut:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            items = _fetch_recent_opened(cur, uid, include_shared=not _sharing_disabled(uid))
    return ListOut(items=_sort_items(items, "opened_at:desc", keep_folder_first=False), parent=None, breadcrumbs=[])


@router.get("/library/search", response_model=SearchOut)
def library_search(
    q: str,
    scope: str = Query(default="home"),
    uid: str = Depends(current_user_id),
) -> SearchOut:
    query = q.strip().lower()
    if not query:
        return SearchOut(items=[], q=q, total=0)
    sharing_disabled = _sharing_disabled(uid)
    if sharing_disabled and scope == "shared":
        raise HTTPException(status_code=403, detail="sharing_disabled")
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if scope == "owned":
                items = _fetch_owned_items(cur, uid)
            elif scope == "shared":
                items = _fetch_shared_received_roots(cur, uid)
            elif scope == "recent":
                items = _fetch_recent_opened(cur, uid, include_shared=not sharing_disabled)
            else:
                owned = _fetch_owned_items(cur, uid)
                items = owned if sharing_disabled else _merge_unique(owned, _fetch_shared_received_roots(cur, uid))
    filtered = [item for item in items if query in str(item.get("name") or "").lower()]
    if scope == "recent":
        filtered = _sort_items(filtered, "opened_at:desc", keep_folder_first=False)
    else:
        filtered = _sort_items(filtered, "updated_at:desc")
    return SearchOut(items=filtered[:200], q=q, total=len(filtered))


@router.post("/share/claim")
def claim_share(inp: ClaimShareIn, uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    share_id = inp.share_id.strip()
    if not share_id:
        raise HTTPException(status_code=400, detail="share_id required")
    created = int(now_ts())
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT s.share_id, s.item_id, s.owner_user_id, s.role, s.expires_at, s.revoked_at,
                       i.type, i.name, i.trashed_at
                FROM shares s
                JOIN items i ON i.item_id = s.item_id
                WHERE s.share_id=%s
                """,
                (share_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="share not found")
            if row["revoked_at"] is not None:
                raise HTTPException(status_code=410, detail="share revoked")
            if row["expires_at"] is not None and int(row["expires_at"]) < created:
                raise HTTPException(status_code=410, detail="share expired")
            if row["trashed_at"] is not None:
                raise HTTPException(status_code=410, detail="shared item is trashed")
            if str(row["owner_user_id"]) == uid:
                raise HTTPException(status_code=400, detail="cannot claim your own share")
            cur.execute(
                """
                INSERT INTO shared_item_inbox(user_id,item_id,owner_user_id,share_id,role,added_at)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (user_id, item_id)
                DO UPDATE SET share_id=EXCLUDED.share_id, role=EXCLUDED.role, added_at=EXCLUDED.added_at
                """,
                (uid, str(row["item_id"]), str(row["owner_user_id"]), share_id, str(row["role"]), created),
            )
        conn.commit()
    return {"ok": True, "share_id": share_id, "item_id": str(row["item_id"]), "name": str(row["name"])}


@router.post("/share/send_by_email")
def send_share_by_email(inp: ShareSendByEmailIn, uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    item_id = str(inp.item_id).strip()
    recipient_emails = _normalize_email_list(
        str(inp.recipient_email) if inp.recipient_email is not None else None,
        [str(value) for value in (inp.recipient_emails or [])],
    )
    role = _ensure_role_value(inp.role)
    share_message = _normalize_share_message(inp.message)
    created = int(now_ts())

    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT item_id, name, owner_user_id, trashed_at
                FROM items
                WHERE item_id=%s
                """,
                (item_id,),
            )
            item = cur.fetchone()
            if not item or str(item["owner_user_id"]) != uid:
                raise HTTPException(status_code=404, detail="item not found")
            if item["trashed_at"] is not None:
                raise HTTPException(status_code=400, detail="shared item is trashed")

            recipient_rows: List[Dict[str, Any]] = []
            for recipient_email in recipient_emails:
                cur.execute("SELECT user_id, email FROM users WHERE lower(email)=%s", (recipient_email,))
                recipient = cur.fetchone()
                if not recipient:
                    raise HTTPException(status_code=404, detail=f"recipient user not found: {recipient_email}")
                recipient_user_id = str(recipient["user_id"])
                if recipient_user_id == uid:
                    raise HTTPException(status_code=400, detail="cannot share to yourself")
                recipient_rows.append({
                    "recipient_user_id": recipient_user_id,
                    "recipient_email": str(recipient["email"]),
                })

            results: List[Dict[str, Any]] = []
            for recipient in recipient_rows:
                share_id = uuid.uuid4().hex
                cur.execute(
                    """
                    INSERT INTO shares(share_id,item_id,owner_user_id,role,expires_at,revoked_at,created_at)
                    VALUES (%s,%s,%s,%s,NULL,NULL,%s)
                    """,
                    (share_id, item_id, uid, role, created),
                )
                cur.execute(
                    """
                    INSERT INTO shared_item_inbox(user_id,item_id,owner_user_id,share_id,role,added_at,message)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (user_id, item_id)
                    DO UPDATE SET
                      owner_user_id=EXCLUDED.owner_user_id,
                      share_id=EXCLUDED.share_id,
                      role=EXCLUDED.role,
                      added_at=EXCLUDED.added_at,
                      message=EXCLUDED.message
                    """,
                    (recipient["recipient_user_id"], item_id, uid, share_id, role, created, share_message),
                )
                results.append({
                    "share_id": share_id,
                    "recipient_user_id": recipient["recipient_user_id"],
                    "recipient_email": recipient["recipient_email"],
                    "item_id": item_id,
                    "name": str(item["name"]),
                    "role": role,
                    "message": share_message,
                    "added_at": created,
                })
        conn.commit()

    return {
        "ok": True,
        "item_id": item_id,
        "name": str(item["name"]),
        "message": share_message,
        "recipients": results,
    }


@router.post("/items/{item_id}/open", response_model=OpenOut)
def mark_item_opened(item_id: str, uid: str = Depends(current_user_id)) -> OpenOut:
    opened_at = int(now_ts())
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            item, access_kind = _can_access_item(cur, uid, item_id)
            if not item or access_kind == "none":
                raise HTTPException(status_code=403, detail="item access denied")
            if access_kind == "shared" and _sharing_disabled(uid):
                raise HTTPException(status_code=403, detail="sharing_disabled")
            cur.execute(
                """
                INSERT INTO recent_item_opens(user_id,item_id,opened_at)
                VALUES (%s,%s,%s)
                ON CONFLICT (user_id, item_id)
                DO UPDATE SET opened_at=EXCLUDED.opened_at
                """,
                (uid, item_id, opened_at),
            )
        conn.commit()
    return OpenOut(item_id=item_id, opened_at=opened_at)


@router.post("/library/items/{item_id}/download_token", response_model=DownloadTokenOut)
def download_token_shared_or_owned(item_id: str, uid: str = Depends(current_user_id)) -> DownloadTokenOut:
    created = int(now_ts())
    expires_at = created + 600
    token = uuid.uuid4().hex
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            item, access_kind = _can_access_item(cur, uid, item_id)
            if not item or access_kind == "none":
                raise HTTPException(status_code=403, detail="item access denied")
            if access_kind == "shared" and _sharing_disabled(uid):
                raise HTTPException(status_code=403, detail="sharing_disabled")
            if not item.get("file_object_id"):
                raise HTTPException(status_code=400, detail="folder download token is unsupported")
            is_shared = access_kind == "shared"
            owner = str(item["owner_user_id"])
            charge_user_id = owner if is_shared else uid
            cur.execute(
                """
                INSERT INTO download_tokens(
                    token,file_object_id,owner_user_id,charge_user_id,is_shared,expires_at,created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (token, item["file_object_id"], owner, charge_user_id, is_shared, expires_at, created),
            )
        conn.commit()
    return DownloadTokenOut(
        download_token=token,
        expires_at=expires_at,
        file_object_id=str(item["file_object_id"]),
        charge_user_id=charge_user_id,
        is_shared=is_shared,
    )
