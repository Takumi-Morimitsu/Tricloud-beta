# -*- coding: utf-8 -*-
"""
ui_upload_bridge_v2.py

既存の /ui/upload bridge を拡張し、
- 同名ファイルが同一フォルダにある場合は「新しい版」として差し替える
- target_item_id を明示された場合も、その item を差し替える
- multipart 論理ファイルでも item_parts を移し替えて履歴を残す

これにより、Web UI とデスクトップ同期クライアントの両方で
"同じ場所に保存すると新しい版になる" 挙動を実現する。
"""

from __future__ import annotations

import os
import tempfile
import hashlib
import traceback
import uuid
from typing import Any, Dict, List, Optional

import zmq
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from psycopg.rows import dict_row

from meta_db_pg import db_conn, now_ts
from auth_util import JWT_SECRET, jwt_decode
from crypto_common_keywrap import (
    dumps_params,
    encrypt_chunk,
    jdump,
    jload,
    loads_params,
    new_file_key,
    new_master_key,
    sha256_hex,
    unwrap_master_key_with_password,
    wrap_key,
    wrap_master_key_with_password,
)
from items_phase2_patch import ROOT_ID, _assert_folder_owner, _fetch_item, apply_uploaded_item_as_new_current

CHUNK_SIZE = int(os.environ.get("UI_UPLOAD_CHUNK_SIZE", str(4 * 1024 * 1024)))
DATA_SERVER_ENDPOINT = os.environ.get("DATA_SERVER_ENDPOINT", "tcp://127.0.0.1:8888")
DATA_SERVER_TIMEOUT_MS = int(os.environ.get("DATA_SERVER_TIMEOUT_MS", "15000"))
router = APIRouter(tags=["phase2-ui-upload"])


def _recv_ds_json(sock: zmq.Socket, phase: str) -> Dict[str, Any]:
    """DataServerからJSON応答を受け取る。応答が返らない場合は読み込み中で固まらせずエラーにする。"""
    try:
        frames = sock.recv_multipart()
    except zmq.Again as exc:
        raise RuntimeError({"status": "timeout", "phase": phase, "message": f"DataServer response timeout during {phase}"}) from exc
    except zmq.ZMQError as exc:
        raise RuntimeError({"status": "zmq_error", "phase": phase, "message": str(exc)}) from exc
    if len(frames) < 2:
        raise RuntimeError({"status": "invalid_response", "phase": phase, "frames": len(frames)})
    try:
        return jload(frames[1])
    except Exception as exc:
        raise RuntimeError({"status": "invalid_json", "phase": phase, "message": str(exc)}) from exc


def bearer_token(authorization: Optional[str] = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return authorization.split(" ", 1)[1].strip()


def current_user_id(token: str = Depends(bearer_token)) -> str:
    td = jwt_decode(token, JWT_SECRET)
    return td.sub


def _save_upload_to_temp(upload: UploadFile) -> tuple[str, int]:
    suffix = os.path.splitext(upload.filename or "upload.bin")[1]
    fd, temp_path = tempfile.mkstemp(prefix="phase2_ui_", suffix=suffix)
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                total += len(chunk)
    except Exception:
        try:
            os.unlink(temp_path)
        except Exception:
            pass
        raise
    return temp_path, total


def _server_wrapping_key(uid: str) -> bytes:
    """操作ごとのパスワード入力を廃止するためのサーバー側ラップ鍵。

    デモ版では、ログイン済みユーザーのアクセストークン認可を前提に、
    JWT_SECRET と user_id から安定した32byte鍵を導出し、DEKをラップする。
    将来の本番版では、OSキーチェーン/端末鍵/回復鍵方式へ置き換え可能。
    """
    material = f"tri-cloud:file-wrap:v1:{uid}".encode("utf-8")
    secret = str(JWT_SECRET).encode("utf-8")
    return hashlib.sha256(secret + b"\0" + material).digest()


def _ensure_master_key_for_user(uid: str, password: str) -> bytes:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT wrapped_master_key, salt, params_json FROM user_master_keys WHERE user_id=%s", (uid,))
            row = cur.fetchone()
            if row:
                return unwrap_master_key_with_password(bytes(row["wrapped_master_key"]), password, bytes(row["salt"]), loads_params(str(row.get("params_json") or "{}")))
            umk = new_master_key()
            wrapped_umk, salt, params = wrap_master_key_with_password(umk, password)
            created = int(now_ts())
            cur.execute(
                """
                INSERT INTO user_master_keys(user_id,kdf,salt,params_json,wrapped_master_key,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,NULL)
                """,
                (uid, str(params.get("kdf", "")), salt, dumps_params(params), wrapped_umk, created),
            )
        conn.commit()
    return umk


def _put_wrapped_dek(uid: str, file_object_id: str, wrapped_dek: bytes) -> None:
    created = int(now_ts())
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO file_wrapped_keys(file_object_id,owner_user_id,wrapped_dek,created_at)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (file_object_id) DO UPDATE SET wrapped_dek=EXCLUDED.wrapped_dek, created_at=EXCLUDED.created_at
                """,
                (file_object_id, uid, wrapped_dek, created),
            )
        conn.commit()


def _find_existing_same_name_file(uid: str, parent_id: str, name: str) -> Optional[str]:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT item_id
                FROM items
                WHERE owner_user_id=%s
                  AND COALESCE(parent_id, %s)=%s
                  AND type='file'
                  AND trashed_at IS NULL
                  AND name=%s
                ORDER BY updated_at DESC NULLS LAST
                LIMIT 1
                """,
                (uid, ROOT_ID, parent_id, name),
            )
            row = cur.fetchone()
            return str(row["item_id"]) if row else None



def _normalize_remote_path_for_backup(value: Optional[str]) -> str:
    return str(value or "").replace("\\", "/").strip().strip("/")


def _safe_upload_display_name(filename: Optional[str], remote_path: Optional[str] = None) -> str:
    """items.name に保存する表示名をファイル名単体へ正規化する。

    フォルダアップロードでは、環境によって file.filename に
    "フォルダ名/ファイル名" のような相対パスが入ることがある。
    DB上の items.name には親フォルダ名を含めず、末尾のファイル名だけを保存する。
    """
    for value in (filename, remote_path):
        normalized = str(value or "").replace("\\", "/").strip().strip("/")
        if not normalized:
            continue
        base = normalized.split("/")[-1].strip()
        if base:
            return base
    return "upload.bin"


def _active_item_paths(cur, uid: str) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT item_id,type,parent_id,name,size_bytes,file_object_id,created_at,updated_at,owner_user_id
        FROM items
        WHERE owner_user_id=%s AND trashed_at IS NULL
        ORDER BY created_at ASC, updated_at ASC NULLS LAST
        """,
        (uid,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    by_id = {str(r["item_id"]): r for r in rows}
    memo: Dict[str, str] = {}

    def build_path(row: Dict[str, Any]) -> str:
        item_id = str(row["item_id"])
        if item_id in memo:
            return memo[item_id]
        parent_id = str(row.get("parent_id") or ROOT_ID)
        if parent_id == ROOT_ID or parent_id not in by_id:
            memo[item_id] = str(row.get("name") or "")
            return memo[item_id]
        base = build_path(by_id[parent_id])
        memo[item_id] = f"{base}/{row.get('name') or ''}" if base else str(row.get("name") or "")
        return memo[item_id]

    for row in rows:
        row["path"] = _normalize_remote_path_for_backup(build_path(row))
    return rows


def _find_existing_file_by_remote_path(cur, uid: str, remote_path: Optional[str]) -> Optional[str]:
    normalized = _normalize_remote_path_for_backup(remote_path)
    if not normalized:
        return None
    matches = [
        row for row in _active_item_paths(cur, uid)
        if row.get("type") == "file" and row.get("path") == normalized
    ]
    if not matches:
        return None
    # 自動バックアップでは同じパスに複数ある状態自体が異常なので、最古の1件を正とする。
    matches.sort(key=lambda row: (int(row.get("created_at") or 0), str(row.get("item_id") or "")))
    return str(matches[0]["item_id"])


def _trash_duplicate_backup_files_by_path(cur, uid: str, canonical_item_id: str, remote_path: Optional[str]) -> int:
    normalized = _normalize_remote_path_for_backup(remote_path)
    if not normalized:
        return 0
    duplicates = [
        row for row in _active_item_paths(cur, uid)
        if row.get("type") == "file"
        and row.get("path") == normalized
        and str(row.get("item_id")) != str(canonical_item_id)
    ]
    if not duplicates:
        return 0
    now = int(now_ts())
    batch_id = str(uuid.uuid4())
    ids = [str(row["item_id"]) for row in duplicates]
    cur.execute(
        """
        UPDATE items
        SET trashed_at=%s, trash_batch_id=%s, updated_at=%s
        WHERE owner_user_id=%s AND item_id = ANY(%s)
        """,
        (now, batch_id, now, uid, ids),
    )
    return len(ids)


def _trash_duplicate_backup_files_by_parent_name(cur, uid: str, canonical_item_id: str, parent_id: Optional[str], name: str) -> int:
    now = int(now_ts())
    batch_id = str(uuid.uuid4())
    cur.execute(
        """
        SELECT item_id
        FROM items
        WHERE owner_user_id=%s
          AND COALESCE(parent_id, %s)=%s
          AND type='file'
          AND trashed_at IS NULL
          AND name=%s
          AND item_id<>%s
        """,
        (uid, ROOT_ID, str(parent_id or ROOT_ID), name, str(canonical_item_id)),
    )
    ids = [str(row["item_id"]) for row in cur.fetchall()]
    if not ids:
        return 0
    cur.execute(
        """
        UPDATE items
        SET trashed_at=%s, trash_batch_id=%s, updated_at=%s
        WHERE owner_user_id=%s AND item_id = ANY(%s)
        """,
        (now, batch_id, now, uid, ids),
    )
    return len(ids)


def _move_item_to_parent(uid: str, item_id: str, parent_id: str) -> None:
    if parent_id == ROOT_ID:
        return
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            _assert_folder_owner(cur, uid, parent_id)
            _fetch_item(cur, item_id, uid, allow_trashed=False)
            cur.execute("UPDATE items SET parent_id=%s, updated_at=%s WHERE item_id=%s AND owner_user_id=%s", (parent_id, int(now_ts()), item_id, uid))
        conn.commit()


def _finalize_uploaded_item_metadata(uid: str, item_id: str, parent_id: str, display_name: str) -> None:
    """DataServerが作成した一時itemの表示名・親フォルダをUI向けに確定する。

    ノード/DataServer内部では一時ファイル名を使ってもよいが、items.name は
    ユーザーが選択した元ファイル名を保持する。これにより一覧表示と同名
    ファイルの版管理が自然に動く。
    """
    now = int(now_ts())
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if parent_id != ROOT_ID:
                _assert_folder_owner(cur, uid, parent_id)
            _fetch_item(cur, item_id, uid, allow_trashed=False)
            cur.execute(
                """
                UPDATE items
                SET name=%s, parent_id=%s, updated_at=%s
                WHERE item_id=%s AND owner_user_id=%s
                """,
                (display_name, None if parent_id == ROOT_ID else parent_id, now, item_id, uid),
            )
        conn.commit()


def _zmq_upload_existing_path(server_endpoint: str, access_token: str, uid: str, path: str, display_name: Optional[str] = None) -> Dict[str, Any]:
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.DEALER)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, DATA_SERVER_TIMEOUT_MS)
    sock.setsockopt(zmq.SNDTIMEO, DATA_SERVER_TIMEOUT_MS)
    sock.connect(server_endpoint)

    # DataServerへ渡すfile_nameは、保存用の一時ファイル名ではなく、
    # ユーザーが選択した元ファイル名を使う。
    file_name = display_name or os.path.basename(path)
    file_size = os.path.getsize(path)
    sock.send_multipart([
        b"json",
        jdump({"op": "init_upload", "access_token": access_token, "file_name": file_name, "file_size": file_size, "chunk_size": CHUNK_SIZE}),
    ])
    rep = _recv_ds_json(sock, "init_upload")

    umk = _server_wrapping_key(uid)
    dek = new_file_key()
    wrapped_dek = wrap_key(umk, dek)

    if rep.get("status") == "ready":
        session_id = str(rep["session_id"])
        file_object_id = str(rep["file_object_id"])
        with open(path, "rb") as f:
            cid = 0
            while True:
                plain = f.read(CHUNK_SIZE)
                if not plain:
                    break
                aad = f"{file_object_id}:{cid}".encode("utf-8")
                blob = encrypt_chunk(dek, plain, aad)
                h = sha256_hex(blob)
                sock.send_multipart([b"data", session_id.encode(), str(cid).encode(), h.encode(), blob])
                ack = _recv_ds_json(sock, f"chunk_ack:{cid}")
                if ack.get("status") != "ack":
                    raise RuntimeError({"chunk_failed": cid, "ack": ack})
                cid += 1
        sock.send_multipart([b"json", session_id.encode(), jdump({"op": "commit"})])
        done = _recv_ds_json(sock, "commit")
        if done.get("status") not in ("uploaded", "replaced"):
            raise RuntimeError(done)
        _put_wrapped_dek(uid, file_object_id, wrapped_dek)
        done["file_object_id"] = file_object_id
        return done

    if rep.get("status") == "ready_multipart":
        upload_id = str(rep["upload_id"])
        parts = list(rep["parts"])
        chunk_size = int(rep.get("chunk_size", CHUNK_SIZE))
        with open(path, "rb") as f:
            for p in parts:
                session_id = str(p["session_id"])
                file_object_id = str(p["file_object_id"])
                remaining = int(p["part_size"])
                cid = 0
                while remaining > 0:
                    read_n = min(chunk_size, remaining)
                    plain = f.read(read_n)
                    if not plain:
                        raise RuntimeError({"status": "unexpected_eof", "part": p})
                    aad = f"{file_object_id}:{cid}".encode("utf-8")
                    blob = encrypt_chunk(dek, plain, aad)
                    h = sha256_hex(blob)
                    sock.send_multipart([b"data", session_id.encode(), str(cid).encode(), h.encode(), blob])
                    ack = _recv_ds_json(sock, f"chunk_ack:{cid}")
                    if ack.get("status") != "ack":
                        raise RuntimeError({"chunk_failed": cid, "ack": ack, "part_index": p.get("part_index")})
                    remaining -= len(plain)
                    cid += 1
                sock.send_multipart([b"json", session_id.encode(), jdump({"op": "commit"})])
                done = _recv_ds_json(sock, "commit")
                if done.get("status") != "part_uploaded":
                    raise RuntimeError({"part_commit_failed": done})
        sock.send_multipart([b"json", jdump({"op": "commit_multipart", "upload_id": upload_id})])
        fin = _recv_ds_json(sock, "commit_multipart")
        if fin.get("status") != "uploaded":
            raise RuntimeError({"multipart_finalize_failed": fin})
        for p in parts:
            _put_wrapped_dek(uid, str(p["file_object_id"]), wrapped_dek)
        return fin

    raise RuntimeError(rep)


@router.post("/ui/upload")
async def ui_upload(
    file: UploadFile = File(...),
    parent_id: Optional[str] = Form(default=None),
    target_item_id: Optional[str] = Form(default=None),
    replace_existing: bool = Form(default=True),
    upload_context: Optional[str] = Form(default=None),
    remote_path: Optional[str] = Form(default=None),
    token: str = Depends(bearer_token),
    uid: str = Depends(current_user_id),
) -> Dict[str, Any]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="file is required")
    target_parent = str(parent_id or ROOT_ID)
    temp_path = None
    try:
        if target_parent != ROOT_ID:
            with db_conn() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    _assert_folder_owner(cur, uid, target_parent)
        temp_path, file_size = _save_upload_to_temp(file)
        if file_size <= 0:
            raise HTTPException(status_code=400, detail="empty file is not allowed")

        upload_context_norm = str(upload_context or "normal").strip().lower()
        remote_path_norm = _normalize_remote_path_for_backup(remote_path)
        display_name = _safe_upload_display_name(file.filename, remote_path_norm)

        uploaded = _zmq_upload_existing_path(DATA_SERVER_ENDPOINT, token, uid, temp_path, display_name=display_name)
        uploaded_item_id = str(uploaded.get("item_id") or "")
        if not uploaded_item_id:
            raise HTTPException(status_code=500, detail="upload succeeded but item_id is missing")

        # 自動バックアップの定期更新は「既存クラウドitemの上書き」を最優先する。
        # target_item_id が欠けた場合でも remote_path で既存itemを解決し、
        # それでも見つからない場合だけ同一親フォルダ・同名ファイルを置換対象にする。
        effective_target = str(target_item_id or "").strip() or None
        if replace_existing and upload_context_norm == "backup":
            with db_conn() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    if not effective_target and remote_path_norm:
                        effective_target = _find_existing_file_by_remote_path(cur, uid, remote_path_norm)
                    if not effective_target:
                        effective_target = _find_existing_same_name_file(uid, target_parent, display_name)
                    if effective_target == uploaded_item_id:
                        effective_target = None
        elif not effective_target and replace_existing:
            effective_target = _find_existing_same_name_file(uid, target_parent, display_name)
            if effective_target == uploaded_item_id:
                effective_target = None

        with db_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                if effective_target:
                    merged = apply_uploaded_item_as_new_current(cur, effective_target, uploaded_item_id, uid, keep_name=display_name)
                    if target_parent != ROOT_ID and str(merged.get("parent_id") or ROOT_ID) != target_parent:
                        cur.execute("UPDATE items SET parent_id=%s, updated_at=%s WHERE item_id=%s AND owner_user_id=%s", (target_parent, int(now_ts()), effective_target, uid))
                        merged["parent_id"] = target_parent
                    duplicate_count = 0
                    if upload_context_norm == "backup":
                        duplicate_count += _trash_duplicate_backup_files_by_path(cur, uid, effective_target, remote_path_norm)
                        duplicate_count += _trash_duplicate_backup_files_by_parent_name(cur, uid, effective_target, merged.get("parent_id"), display_name)
                    conn.commit()
                    return {
                        "status": "versioned_replace",
                        "item_id": effective_target,
                        "parent_id": target_parent,
                        "name": display_name,
                        "size_bytes": file_size,
                        "file_object_id": merged.get("file_object_id"),
                        "versioned": True,
                        "deduped": duplicate_count,
                    }
            conn.commit()

        _finalize_uploaded_item_metadata(uid, uploaded_item_id, target_parent, display_name)
        deduped = 0
        if upload_context_norm == "backup":
            with db_conn() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    deduped += _trash_duplicate_backup_files_by_path(cur, uid, uploaded_item_id, remote_path_norm)
                    deduped += _trash_duplicate_backup_files_by_parent_name(cur, uid, uploaded_item_id, target_parent, display_name)
                conn.commit()
        return {
            "status": str(uploaded.get("status") or "uploaded"),
            "item_id": uploaded_item_id,
            "file_object_id": uploaded.get("file_object_id"),
            "upload_id": uploaded.get("upload_id"),
            "parent_id": target_parent,
            "name": display_name,
            "size_bytes": file_size,
            "versioned": False,
            "deduped": deduped,
        }
    except HTTPException:
        raise
    except Exception as exc:
        print("[ui_upload_bridge] upload failed", repr(exc))
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"upload bridge failed: {exc}")
    finally:
        try:
            await file.close()
        except Exception:
            pass
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception:
                pass
