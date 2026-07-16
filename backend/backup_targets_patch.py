# -*- coding: utf-8 -*-
"""
backup_targets_patch.py

自動バックアップ対象一覧をDBに永続化するための小さなパッチ。

目的:
- 通常アップロードと自動バックアップ対象を将来の使用量計量・管理画面で区別できるようにする
- 別PCでログインしてもバックアップ対象一覧を復元できるようにする
- Electron側の runtime state / localStorage だけに依存しない
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from psycopg.rows import dict_row

from meta_db_pg import db_conn, now_ts
from items_phase2_patch import current_user_id

router = APIRouter(prefix="/backup", tags=["backup-targets"])

DDL = [
    """
    CREATE TABLE IF NOT EXISTS backup_targets (
        target_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        local_path TEXT NOT NULL,
        remote_path TEXT NOT NULL,
        item_type TEXT NOT NULL CHECK (item_type IN ('file', 'folder')),
        display_name TEXT NOT NULL,
        source_device_label TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        last_scanned_at INTEGER,
        last_synced_at INTEGER,
        last_error TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        meta_json TEXT,
        remote_item_id TEXT,
        UNIQUE(user_id, local_path, item_type)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_backup_targets_user_status ON backup_targets(user_id, status, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_backup_targets_user_remote ON backup_targets(user_id, remote_path)",
    "ALTER TABLE backup_targets ADD COLUMN IF NOT EXISTS remote_item_id TEXT",
    "CREATE INDEX IF NOT EXISTS idx_backup_targets_user_device ON backup_targets(user_id, source_device_label)",
    "CREATE INDEX IF NOT EXISTS idx_backup_targets_user_remote_item ON backup_targets(user_id, remote_item_id)",
]


def init_backup_targets_schema() -> None:
    """backup_targets 用DDLを作成する。

    FastAPI lifespan から明示的に呼ばれる想定。
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            for stmt in DDL:
                cur.execute(stmt)
        conn.commit()


class BackupTargetIn(BaseModel):
    local_path: str = Field(..., min_length=1)
    remote_path: str = Field(..., min_length=1)
    item_type: str = Field(..., pattern="^(file|folder)$")
    display_name: str = Field(..., min_length=1)
    source_device_label: Optional[str] = None
    last_scanned_at: Optional[int] = None
    last_synced_at: Optional[int] = None
    last_error: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)
    remote_item_id: Optional[str] = None


class BackupTargetOut(BaseModel):
    target_id: str
    local_path: str
    remote_path: str
    item_type: str
    display_name: str
    source_device_label: Optional[str] = None
    status: str
    last_scanned_at: Optional[int] = None
    last_synced_at: Optional[int] = None
    last_error: Optional[str] = None
    created_at: int
    updated_at: int
    meta: Dict[str, Any] = Field(default_factory=dict)
    remote_item_id: Optional[str] = None


class BackupTargetsReplaceIn(BaseModel):
    targets: List[BackupTargetIn] = Field(default_factory=list)


class BackupTargetsOut(BaseModel):
    targets: List[BackupTargetOut]
    total: int


class BackupTargetsDedupeIn(BaseModel):
    targets: List[BackupTargetIn] = Field(default_factory=list)


class BackupTargetsDedupeOut(BaseModel):
    deduped_count: int


def _row_to_out(row: Dict[str, Any]) -> BackupTargetOut:
    meta: Dict[str, Any] = {}
    raw_meta = row.get("meta_json")
    if raw_meta:
        try:
            parsed = json.loads(str(raw_meta))
            if isinstance(parsed, dict):
                meta = parsed
        except Exception:
            meta = {}
    return BackupTargetOut(
        target_id=str(row["target_id"]),
        local_path=str(row["local_path"]),
        remote_path=str(row["remote_path"]),
        item_type=str(row["item_type"]),
        display_name=str(row["display_name"]),
        source_device_label=row.get("source_device_label"),
        status=str(row.get("status") or "active"),
        last_scanned_at=row.get("last_scanned_at"),
        last_synced_at=row.get("last_synced_at"),
        last_error=row.get("last_error"),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        meta=meta,
        remote_item_id=row.get("remote_item_id"),
    )


def _normalize_target(inp: BackupTargetIn) -> BackupTargetIn:
    local_path = inp.local_path.strip()
    remote_path = inp.remote_path.replace("\\", "/").strip().strip("/")
    display_name = inp.display_name.strip() or remote_path.split("/")[-1] or local_path.split("\\")[-1] or local_path.split("/")[-1]
    if not local_path:
        raise HTTPException(status_code=400, detail="local_path is required")
    if not remote_path:
        raise HTTPException(status_code=400, detail="remote_path is required")
    return BackupTargetIn(
        local_path=local_path,
        remote_path=remote_path,
        item_type=inp.item_type,
        display_name=display_name,
        source_device_label=(inp.source_device_label or None),
        last_scanned_at=inp.last_scanned_at,
        last_synced_at=inp.last_synced_at,
        last_error=inp.last_error,
        meta=inp.meta or {},
        remote_item_id=(inp.remote_item_id or None),
    )


def _normalize_remote_path(value: str) -> str:
    return str(value or "").replace("\\", "/").strip().strip("/")


def _active_item_paths(cur, uid: str) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT item_id,type,parent_id,name,created_at,updated_at
        FROM items
        WHERE owner_user_id=%s AND trashed_at IS NULL
        ORDER BY created_at ASC, updated_at ASC NULLS LAST
        """,
        (uid,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    by_id = {str(row["item_id"]): row for row in rows}
    memo: Dict[str, str] = {}

    def build_path(row: Dict[str, Any]) -> str:
        item_id = str(row["item_id"])
        if item_id in memo:
            return memo[item_id]
        parent_id = str(row.get("parent_id") or "root")
        if parent_id == "root" or parent_id not in by_id:
            memo[item_id] = str(row.get("name") or "")
            return memo[item_id]
        base = build_path(by_id[parent_id])
        memo[item_id] = f"{base}/{row.get('name') or ''}" if base else str(row.get("name") or "")
        return memo[item_id]

    for row in rows:
        row["path"] = _normalize_remote_path(build_path(row))
    return rows


def _target_value(target: Any, key: str, default: Any = None) -> Any:
    if isinstance(target, dict):
        return target.get(key, default)
    return getattr(target, key, default)


def _target_matches_active_item(target: Any, active_items: List[Dict[str, Any]]) -> bool:
    """target に対応するクラウド item が現在も active か判定する。

    自動バックアップ対象がごみ箱へ移動・完全削除された後も backup_targets に
    active 行が残ると、Electron が次回開始時に古い target を再読込してしまう。
    そのため、GET/PUT/POST の各入口で active items と照合する。
    """
    target_type = str(_target_value(target, "item_type", "") or "")
    target_remote_item_id = str(_target_value(target, "remote_item_id", "") or "").strip()
    target_path = _normalize_remote_path(str(_target_value(target, "remote_path", "") or _target_value(target, "display_name", "") or ""))

    for item in active_items:
        item_id = str(item.get("item_id") or "")
        item_type = str(item.get("type") or "")
        item_path = _normalize_remote_path(str(item.get("path") or item.get("name") or ""))
        if target_remote_item_id:
            return target_remote_item_id == item_id
        if target_path and target_type and item_type == target_type and item_path == target_path:
            return True
    return False


def _filter_targets_for_active_items(cur, uid: str, targets: List[Any]) -> List[Any]:
    if not targets:
        return []
    active_items = _active_item_paths(cur, uid)
    if not active_items:
        return []
    return [target for target in targets if _target_matches_active_item(target, active_items)]


def _mark_stale_backup_target_rows_removed(cur, rows: List[Dict[str, Any]], kept_rows: List[Dict[str, Any]]) -> None:
    kept_ids = {str(row.get("target_id") or "") for row in kept_rows}
    stale_ids = [str(row.get("target_id") or "") for row in rows if str(row.get("target_id") or "") not in kept_ids]
    stale_ids = [target_id for target_id in stale_ids if target_id]
    if not stale_ids:
        return
    cur.execute(
        """
        UPDATE backup_targets
        SET status='removed', updated_at=%s
        WHERE target_id = ANY(%s)
        """,
        (int(now_ts()), stale_ids),
    )


def _dedupe_backup_file_paths(cur, uid: str, targets: List[BackupTargetIn]) -> int:
    normalized_targets = [_normalize_target(target) for target in targets]
    if not normalized_targets:
        return 0

    target_paths = []
    for target in normalized_targets:
        root_path = _normalize_remote_path(target.remote_path)
        if root_path:
            target_paths.append((root_path, target.item_type))

    if not target_paths:
        return 0

    rows = _active_item_paths(cur, uid)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        if row.get("type") != "file":
            continue
        item_path = _normalize_remote_path(row.get("path") or "")
        if not item_path:
            continue
        in_scope = False
        for root_path, item_type in target_paths:
            if item_type == "file" and item_path == root_path:
                in_scope = True
                break
            if item_type == "folder" and (item_path == root_path or item_path.startswith(f"{root_path}/")):
                in_scope = True
                break
        if in_scope:
            grouped.setdefault(item_path, []).append(row)

    ids_to_trash: List[str] = []
    for _path, entries in grouped.items():
        if len(entries) <= 1:
            continue
        entries.sort(key=lambda row: (int(row.get("created_at") or 0), str(row.get("item_id") or "")))
        ids_to_trash.extend(str(row["item_id"]) for row in entries[1:])

    if not ids_to_trash:
        return 0

    now = int(now_ts())
    batch_id = str(uuid.uuid4())
    cur.execute(
        """
        UPDATE items
        SET trashed_at=%s, trash_batch_id=%s, updated_at=%s
        WHERE owner_user_id=%s AND item_id = ANY(%s)
        """,
        (now, batch_id, now, uid, ids_to_trash),
    )
    return len(ids_to_trash)



@router.get("/targets", response_model=BackupTargetsOut)
def list_backup_targets(uid: str = Depends(current_user_id)) -> BackupTargetsOut:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT *
                FROM backup_targets
                WHERE user_id = %s AND status = 'active'
                ORDER BY updated_at DESC, created_at DESC
                """,
                (uid,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            kept_rows = _filter_targets_for_active_items(cur, uid, rows)
            _mark_stale_backup_target_rows_removed(cur, rows, kept_rows)
        conn.commit()
    targets = [_row_to_out(r) for r in kept_rows]
    return BackupTargetsOut(targets=targets, total=len(targets))


@router.put("/targets", response_model=BackupTargetsOut)
def replace_backup_targets(inp: BackupTargetsReplaceIn, uid: str = Depends(current_user_id)) -> BackupTargetsOut:
    """ユーザーのバックアップ対象一覧を丸ごと置き換える。

    UI / Electron runtime state を正とし、DB側を同期する用途。
    既存行は削除せず status='removed' にするので、管理画面や監査で履歴を追いやすい。
    """
    now = now_ts()
    normalized: List[BackupTargetIn] = [_normalize_target(t) for t in inp.targets]

    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            normalized = _filter_targets_for_active_items(cur, uid, normalized)
            active_keys = {(t.local_path, t.item_type) for t in normalized}

            if active_keys:
                cur.execute(
                    """
                    SELECT target_id, local_path, item_type
                    FROM backup_targets
                    WHERE user_id = %s AND status = 'active'
                    """,
                    (uid,),
                )
                for row in cur.fetchall():
                    if (str(row["local_path"]), str(row["item_type"])) not in active_keys:
                        cur.execute(
                            """
                            UPDATE backup_targets
                            SET status = 'removed', updated_at = %s
                            WHERE target_id = %s
                            """,
                            (now, row["target_id"]),
                        )
            else:
                cur.execute(
                    """
                    UPDATE backup_targets
                    SET status = 'removed', updated_at = %s
                    WHERE user_id = %s AND status = 'active'
                    """,
                    (now, uid),
                )

            for target in normalized:
                target_id = str(uuid.uuid4())
                meta_json = json.dumps(target.meta or {}, ensure_ascii=False)
                cur.execute(
                    """
                    INSERT INTO backup_targets (
                        target_id, user_id, local_path, remote_path, item_type,
                        display_name, source_device_label, status,
                        last_scanned_at, last_synced_at, last_error,
                        created_at, updated_at, meta_json, remote_item_id
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (user_id, local_path, item_type) DO UPDATE SET
                        remote_path = EXCLUDED.remote_path,
                        display_name = EXCLUDED.display_name,
                        source_device_label = EXCLUDED.source_device_label,
                        status = 'active',
                        last_scanned_at = EXCLUDED.last_scanned_at,
                        last_synced_at = EXCLUDED.last_synced_at,
                        last_error = EXCLUDED.last_error,
                        updated_at = EXCLUDED.updated_at,
                        meta_json = EXCLUDED.meta_json,
                        remote_item_id = EXCLUDED.remote_item_id
                    """,
                    (
                        target_id, uid, target.local_path, target.remote_path, target.item_type,
                        target.display_name, target.source_device_label,
                        target.last_scanned_at, target.last_synced_at, target.last_error,
                        now, now, meta_json, target.remote_item_id,
                    ),
                )

            cur.execute(
                """
                SELECT *
                FROM backup_targets
                WHERE user_id = %s AND status = 'active'
                ORDER BY updated_at DESC, created_at DESC
                """,
                (uid,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            kept_rows = _filter_targets_for_active_items(cur, uid, rows)
            _mark_stale_backup_target_rows_removed(cur, rows, kept_rows)
        conn.commit()

    targets = [_row_to_out(r) for r in kept_rows]
    return BackupTargetsOut(targets=targets, total=len(targets))


@router.post("/targets/dedupe", response_model=BackupTargetsDedupeOut)
def dedupe_backup_targets(inp: BackupTargetsDedupeIn, uid: str = Depends(current_user_id)) -> BackupTargetsDedupeOut:
    """バックアップ対象配下で同一パスの重複ファイルを1件にまとめる。

    既存の重複を安全側に寄せて soft-trash するための自動バックアップ専用補助API。
    通常のマイドライブ全体には作用せず、渡されたバックアップ対象パスの配下だけを見る。
    """
    normalized = [_normalize_target(target) for target in inp.targets]
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            count = _dedupe_backup_file_paths(cur, uid, normalized)
        conn.commit()
    return BackupTargetsDedupeOut(deduped_count=count)


@router.post("/targets", response_model=BackupTargetOut)
def upsert_backup_target(inp: BackupTargetIn, uid: str = Depends(current_user_id)) -> BackupTargetOut:
    target = _normalize_target(inp)
    now = now_ts()
    target_id = str(uuid.uuid4())
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if not _filter_targets_for_active_items(cur, uid, [target]):
                raise HTTPException(status_code=400, detail="backup target item is not active")
            cur.execute(
                """
                INSERT INTO backup_targets (
                    target_id, user_id, local_path, remote_path, item_type,
                    display_name, source_device_label, status,
                    last_scanned_at, last_synced_at, last_error,
                    created_at, updated_at, meta_json, remote_item_id
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (user_id, local_path, item_type) DO UPDATE SET
                    remote_path = EXCLUDED.remote_path,
                    display_name = EXCLUDED.display_name,
                    source_device_label = EXCLUDED.source_device_label,
                    status = 'active',
                    last_scanned_at = EXCLUDED.last_scanned_at,
                    last_synced_at = EXCLUDED.last_synced_at,
                    last_error = EXCLUDED.last_error,
                    updated_at = EXCLUDED.updated_at,
                    meta_json = EXCLUDED.meta_json,
                    remote_item_id = EXCLUDED.remote_item_id
                RETURNING *
                """,
                (
                    target_id, uid, target.local_path, target.remote_path, target.item_type,
                    target.display_name, target.source_device_label,
                    target.last_scanned_at, target.last_synced_at, target.last_error,
                    now, now, json.dumps(target.meta or {}, ensure_ascii=False), target.remote_item_id,
                ),
            )
            row = cur.fetchone()
        conn.commit()
    return _row_to_out(row)
