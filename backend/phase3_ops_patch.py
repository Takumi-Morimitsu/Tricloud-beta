# -*- coding: utf-8 -*-
"""
phase3_ops_patch.py

フェーズ3向けの追加:
- 右クリックメニューから使う move API（循環移動防止つき）
- ごみ箱の保持期間ポリシー取得
- 30日経過したごみ箱アイテムの自動整理用 API

既存の items_phase2_patch を壊さずに薄く追加する。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from psycopg.rows import dict_row

from meta_db_pg import db_conn, now_ts
from items_phase2_patch import ROOT_ID, current_user_id, _assert_folder_owner, _collect_descendants, _fetch_item
from object_gc import collect_file_object_ids_for_items, gc_unreferenced_objects

TRASH_RETENTION_DAYS = int(os.environ.get("TRASH_RETENTION_DAYS", "30"))
TRASH_RETENTION_SECONDS = TRASH_RETENTION_DAYS * 24 * 60 * 60

router = APIRouter(tags=["phase3-ops"])


class MoveIn(BaseModel):
    parent_id: Optional[str] = None


class TrashPolicyOut(BaseModel):
    retention_days: int
    retention_seconds: int
    now_ts: int


class ExpiredTrashPurgeOut(BaseModel):
    purged_roots: List[str]
    purged_count: int
    cutoff_ts: int
    retention_days: int



def _normalize_parent_id(parent_id: Optional[str]) -> str:
    if parent_id in (None, "", ROOT_ID):
        return ROOT_ID
    return str(parent_id)



def _top_level_expired_roots(cur, uid: str, cutoff_ts: int) -> List[str]:
    cur.execute(
        """
        WITH candidates AS (
            SELECT item_id,parent_id
            FROM items
            WHERE owner_user_id=%s
              AND trashed_at IS NOT NULL
              AND trashed_at <= %s
        )
        SELECT c.item_id
        FROM candidates c
        LEFT JOIN candidates p ON c.parent_id = p.item_id
        WHERE p.item_id IS NULL
        ORDER BY c.item_id ASC
        """,
        (uid, cutoff_ts),
    )
    return [str(row["item_id"]) for row in cur.fetchall()]



def _purge_root_items(cur, uid: str, root_ids: List[str]) -> Dict[str, Any]:
    if not root_ids:
        return {"purged_count": 0, "object_gc": {}}

    all_ids: Set[str] = set()
    for root_id in root_ids:
        for node in _collect_descendants(cur, uid, root_id):
            all_ids.add(str(node["item_id"]))

    if not all_ids:
        return {"purged_count": 0, "object_gc": {}}

    ids = sorted(all_ids)
    candidate_file_object_ids = collect_file_object_ids_for_items(cur, ids)
    cur.execute("SELECT version_id FROM item_versions WHERE item_id = ANY(%s)", (ids,))
    version_ids = [str(row["version_id"]) for row in cur.fetchall()]
    if version_ids:
        cur.execute("DELETE FROM item_version_parts WHERE version_id = ANY(%s)", (version_ids,))
    cur.execute("DELETE FROM item_versions WHERE item_id = ANY(%s)", (ids,))
    cur.execute("DELETE FROM item_parts WHERE item_id = ANY(%s)", (ids,))
    cur.execute("DELETE FROM items WHERE owner_user_id=%s AND item_id = ANY(%s)", (uid, ids))
    gc_result = gc_unreferenced_objects(cur, candidate_file_object_ids, reason="trash_purge")
    return {"purged_count": len(ids), "object_gc": gc_result}


@router.post("/items/{item_id}/move")
def move_item(item_id: str, inp: MoveIn, uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    target_parent = _normalize_parent_id(inp.parent_id)
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            item = _fetch_item(cur, item_id, uid, allow_trashed=False)
            if target_parent != ROOT_ID:
                _assert_folder_owner(cur, uid, target_parent)

            if item["type"] == "folder":
                blocked = {str(node["item_id"]) for node in _collect_descendants(cur, uid, item_id)}
                if target_parent in blocked:
                    raise HTTPException(status_code=400, detail="folder cannot move into itself or its descendants")

            cur.execute(
                """
                UPDATE items
                SET parent_id=%s, updated_at=%s
                WHERE item_id=%s AND owner_user_id=%s
                RETURNING item_id,type,parent_id,name,size_bytes,file_object_id,created_at,updated_at,trashed_at,trash_batch_id,owner_user_id
                """,
                (None if target_parent == ROOT_ID else target_parent, int(now_ts()), item_id, uid),
            )
            moved = dict(cur.fetchone())
        conn.commit()
    moved["version_count"] = item.get("version_count", 0)
    return moved




@router.delete("/items/{item_id}/purge_permanent")
def purge_item_permanent(item_id: str, uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            item = _fetch_item(cur, item_id, uid, allow_trashed=True)
            if item.get("trashed_at") is None:
                raise HTTPException(status_code=400, detail="only trashed items can be purged")
            purge_result = _purge_root_items(cur, uid, [item_id])
        conn.commit()
    return {"purged_item_id": item_id, "count": purge_result["purged_count"], "object_gc": purge_result.get("object_gc", {})}

@router.get("/trash/policy", response_model=TrashPolicyOut)
def get_trash_policy(uid: str = Depends(current_user_id)) -> TrashPolicyOut:
    return TrashPolicyOut(
        retention_days=TRASH_RETENTION_DAYS,
        retention_seconds=TRASH_RETENTION_SECONDS,
        now_ts=int(now_ts()),
    )


@router.post("/trash/purge_expired", response_model=ExpiredTrashPurgeOut)
def purge_expired_trash(uid: str = Depends(current_user_id)) -> ExpiredTrashPurgeOut:
    cutoff_ts = int(now_ts()) - TRASH_RETENTION_SECONDS
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            root_ids = _top_level_expired_roots(cur, uid, cutoff_ts)
            purge_result = _purge_root_items(cur, uid, root_ids)
        conn.commit()
    return ExpiredTrashPurgeOut(
        purged_roots=root_ids,
        purged_count=purge_result["purged_count"],
        cutoff_ts=cutoff_ts,
        retention_days=TRASH_RETENTION_DAYS,
    )
