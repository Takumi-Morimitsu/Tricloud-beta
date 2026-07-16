# -*- coding: utf-8 -*-
"""
phase4_copy_patch.py

追加内容:
- 単一アイテムのコピー
- 複数アイテムの一括コピー
- ファイル / フォルダの両対応
- multipart 論理ファイルは item_parts を複製し、既存 object を参照する
- フォルダは配下を再帰的に複製する

方針:
- ノード側へ新しい object コピー命令は送らず、まずは metadata copy として実装する
- 同一 owner 内のコピー前提なので、既存 file_object_id / item_parts をそのまま参照できる
- 名前衝突時は「〜 コピー」「〜 コピー (2)」で解決する
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from psycopg.rows import dict_row

from meta_db_pg import db_conn, now_ts
from items_phase2_patch import ROOT_ID, current_user_id, _assert_folder_owner, _collect_descendants, _fetch_item

router = APIRouter(tags=["phase4-copy"])


class CopyIn(BaseModel):
    parent_id: Optional[str] = None


class BatchCopyIn(BaseModel):
    item_ids: List[str] = Field(default_factory=list)
    parent_id: Optional[str] = None


def _normalize_parent_id(parent_id: Optional[str]) -> str:
    if parent_id in (None, "", ROOT_ID):
        return ROOT_ID
    return str(parent_id)


def _split_name_for_copy(name: str, is_folder: bool) -> Tuple[str, str]:
    if is_folder:
        return name, ""
    p = Path(name)
    suffix = "".join(p.suffixes)
    if suffix:
        base = name[: -len(suffix)]
    else:
        base = name
    return base, suffix


def _existing_name(cur, uid: str, parent_id: str, name: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM items
        WHERE owner_user_id=%s
          AND COALESCE(parent_id, %s)=%s
          AND trashed_at IS NULL
          AND name=%s
        LIMIT 1
        """,
        (uid, ROOT_ID, parent_id, name),
    )
    return cur.fetchone() is not None


def _dedupe_copy_name(cur, uid: str, parent_id: str, original_name: str, is_folder: bool) -> str:
    base, suffix = _split_name_for_copy(original_name, is_folder)
    candidate = f"{base} コピー{suffix}"
    if not _existing_name(cur, uid, parent_id, candidate):
        return candidate
    idx = 2
    while True:
        candidate = f"{base} コピー ({idx}){suffix}"
        if not _existing_name(cur, uid, parent_id, candidate):
            return candidate
        idx += 1


def _insert_item(
    cur,
    *,
    uid: str,
    item_type: str,
    parent_id: str,
    name: str,
    size_bytes: int,
    file_object_id: Optional[str],
) -> Dict[str, Any]:
    item_id = str(uuid.uuid4())
    ts = int(now_ts())
    cur.execute(
        """
        INSERT INTO items(
            item_id,type,parent_id,name,size_bytes,file_object_id,
            created_at,updated_at,trashed_at,trash_batch_id,owner_user_id
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL,NULL,%s)
        RETURNING item_id,type,parent_id,name,size_bytes,file_object_id,created_at,updated_at,trashed_at,trash_batch_id,owner_user_id
        """,
        (
            item_id,
            item_type,
            None if parent_id == ROOT_ID else parent_id,
            name,
            int(size_bytes or 0),
            file_object_id,
            ts,
            ts,
            uid,
        ),
    )
    row = dict(cur.fetchone())
    row["version_count"] = 0
    return row


def _copy_file_parts(cur, source_item_id: str, new_item_id: str) -> None:
    cur.execute(
        """
        SELECT part_index,file_object_id,part_offset,part_size
        FROM item_parts
        WHERE item_id=%s
        ORDER BY part_index ASC
        """,
        (source_item_id,),
    )
    for p in cur.fetchall():
        cur.execute(
            """
            INSERT INTO item_parts(item_id,part_index,file_object_id,part_offset,part_size)
            VALUES (%s,%s,%s,%s,%s)
            """,
            (new_item_id, int(p["part_index"]), str(p["file_object_id"]), int(p["part_offset"]), int(p["part_size"])),
        )


def _copy_file(cur, uid: str, source: Dict[str, Any], dest_parent_id: str, *, name_override: Optional[str] = None) -> Dict[str, Any]:
    name = name_override or _dedupe_copy_name(cur, uid, dest_parent_id, str(source["name"]), False)
    created = _insert_item(
        cur,
        uid=uid,
        item_type="file",
        parent_id=dest_parent_id,
        name=name,
        size_bytes=int(source.get("size_bytes") or 0),
        file_object_id=source.get("file_object_id"),
    )
    if not source.get("file_object_id"):
        _copy_file_parts(cur, str(source["item_id"]), str(created["item_id"]))
    return created


def _copy_folder_recursive(cur, uid: str, source_root: Dict[str, Any], dest_parent_id: str) -> Dict[str, Any]:
    descendants = _collect_descendants(cur, uid, str(source_root["item_id"]))
    by_id = {str(node["item_id"]): dict(node) for node in descendants}

    def depth(node: Dict[str, Any]) -> int:
        d = 0
        pid = node.get("parent_id")
        while pid and str(pid) in by_id:
            d += 1
            pid = by_id[str(pid)].get("parent_id")
        return d

    ordered = sorted(by_id.values(), key=lambda n: (depth(n), n["type"] != "folder", str(n["name"])))
    mapping: Dict[str, str] = {}
    created_root: Optional[Dict[str, Any]] = None

    for node in ordered:
        old_id = str(node["item_id"])
        if old_id == str(source_root["item_id"]):
            next_parent = dest_parent_id
            next_name = _dedupe_copy_name(cur, uid, next_parent, str(node["name"]), True)
        else:
            old_parent = str(node.get("parent_id") or ROOT_ID)
            if old_parent not in mapping:
                continue
            next_parent = mapping[old_parent]
            next_name = str(node["name"])

        created = _insert_item(
            cur,
            uid=uid,
            item_type=str(node["type"]),
            parent_id=next_parent,
            name=next_name,
            size_bytes=int(node.get("size_bytes") or 0),
            file_object_id=node.get("file_object_id"),
        )
        mapping[old_id] = str(created["item_id"])
        if node["type"] == "file" and not node.get("file_object_id"):
            _copy_file_parts(cur, old_id, str(created["item_id"]))
        if old_id == str(source_root["item_id"]):
            created_root = created

    if not created_root:
        raise HTTPException(status_code=500, detail="folder copy failed")
    return created_root


@router.post("/items/{item_id}/copy")
def copy_item(item_id: str, inp: CopyIn, uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    target_parent = _normalize_parent_id(inp.parent_id)
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            source = _fetch_item(cur, item_id, uid, allow_trashed=False)
            if target_parent != ROOT_ID:
                _assert_folder_owner(cur, uid, target_parent)
            else:
                target_parent = _normalize_parent_id(source.get("parent_id"))

            if source["type"] == "folder":
                copied = _copy_folder_recursive(cur, uid, source, target_parent)
            else:
                copied = _copy_file(cur, uid, source, target_parent)
        conn.commit()
    return {"item": copied, "count": 1}


@router.post("/items/copy_batch")
def copy_items_batch(inp: BatchCopyIn, uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    item_ids = [str(x) for x in inp.item_ids if str(x).strip()]
    if not item_ids:
        raise HTTPException(status_code=400, detail="item_ids required")

    copied_items: List[Dict[str, Any]] = []
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            explicit_parent = _normalize_parent_id(inp.parent_id) if inp.parent_id not in (None, "") else None
            if explicit_parent and explicit_parent != ROOT_ID:
                _assert_folder_owner(cur, uid, explicit_parent)

            for item_id in item_ids:
                source = _fetch_item(cur, item_id, uid, allow_trashed=False)
                target_parent = explicit_parent or _normalize_parent_id(source.get("parent_id"))
                if source["type"] == "folder":
                    copied = _copy_folder_recursive(cur, uid, source, target_parent)
                else:
                    copied = _copy_file(cur, uid, source, target_parent)
                copied_items.append(copied)
        conn.commit()
    return {"items": copied_items, "count": len(copied_items)}
