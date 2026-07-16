# -*- coding: utf-8 -*-
"""
ui_download_bridge_v2.py

UI/Electron向けのダウンロードブリッジ。
DataServer(ZMQ)から暗号化チャンクを受け取り、ユーザーのログイン時パスワードで
DEKを復号し、元ファイルのバイト列としてHTTPレスポンスへ返す。

注意:
- 画面でタブ/ウィンドウを開くためのものではない。
- Electron main process またはブラウザ側fetchから呼び、保存処理は呼び出し側が行う。
"""

from __future__ import annotations

import mimetypes
import os
import hashlib
import re
import tempfile
from typing import Any, Dict, Optional, Set
from urllib.parse import quote

import zmq
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from psycopg.rows import dict_row

from auth_util import JWT_SECRET, jwt_decode
from crypto_common_keywrap import (
    b64d,
    decrypt_chunk,
    jdump,
    jload,
    loads_params,
    sha256_hex,
    unwrap_key,
    unwrap_master_key_with_password,
)
from meta_db_pg import db_conn, now_ts

DATA_SERVER_ENDPOINT = os.environ.get("DATA_SERVER_ENDPOINT", "tcp://127.0.0.1:8888")
DATA_SERVER_TIMEOUT_MS = int(os.environ.get("DATA_SERVER_TIMEOUT_MS", "15000"))
DOWNLOAD_TEMP_PREFIX = "tri_cloud_download_"
router = APIRouter(tags=["phase2-ui-download"])


class UiDownloadIn(BaseModel):
    download_token: str = Field(..., min_length=8)
    password: Optional[str] = None
    file_name: Optional[str] = None


def bearer_token(authorization: Optional[str] = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return authorization.split(" ", 1)[1].strip()


def current_user_id(token: str = Depends(bearer_token)) -> str:
    td = jwt_decode(token, JWT_SECRET)
    return td.sub


def _safe_download_filename(value: Optional[str], fallback: str = "download.bin") -> str:
    name = str(value or "").strip()
    if not name:
        name = fallback
    # Windows/macOS/Linuxで問題になりやすい文字を置換する
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip().strip(".")
    return name or fallback


def _shared_root_for_item(cur, uid: str, item_id: str) -> Optional[str]:
    """uid が item_id またはその親階層を共有受信しているか確認する。

    phase5_library_patch.py の _find_shared_root_for_item と同じ考え方を、
    /ui/download 側でも使えるようにする小さなローカル実装。
    """
    current = str(item_id or "")
    guard = 0
    while current and current != "root" and guard < 64:
        cur.execute(
            "SELECT 1 FROM shared_item_inbox WHERE user_id=%s AND item_id=%s",
            (uid, current),
        )
        if cur.fetchone():
            return current
        cur.execute("SELECT parent_id FROM items WHERE item_id=%s", (current,))
        row = cur.fetchone()
        if not row:
            break
        current = str(row.get("parent_id") or "root")
        guard += 1
    return None


def _find_downloadable_item_for_object(cur, *, uid: str, file_object_id: str, owner_user_id: str) -> Optional[Dict[str, Any]]:
    """download token の file_object_id に対応する、uid が読める現行 item を探す。

    download_tokens は file_object_id 単位で発行されるため、/ui/download では
    その object を参照している items を逆引きし、所有者本人または共有受信者
    としてアクセスできるかをここで再確認する。
    """
    cur.execute(
        """
        SELECT item_id,name,parent_id,owner_user_id,trashed_at
        FROM items
        WHERE file_object_id=%s
          AND owner_user_id=%s
          AND type='file'
          AND trashed_at IS NULL
        ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
        """,
        (file_object_id, owner_user_id),
    )
    rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return None

    if uid == owner_user_id:
        return rows[0]

    for row in rows:
        if _shared_root_for_item(cur, uid, str(row["item_id"])):
            return row
    return None


def _lookup_download_context(download_token: str, uid: str, requested_name: Optional[str]) -> Dict[str, Any]:
    now = int(now_ts())
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT token,file_object_id,owner_user_id,charge_user_id,is_shared,expires_at
                FROM download_tokens
                WHERE token=%s
                """,
                (download_token,),
            )
            row = cur.fetchone()
            if not row or int(row["expires_at"]) < now:
                raise HTTPException(status_code=404, detail="download token invalid or expired")

            file_object_id = str(row["file_object_id"])
            owner_user_id = str(row["owner_user_id"])
            charge_user_id = str(row.get("charge_user_id") or owner_user_id)
            is_shared = bool(row.get("is_shared"))

            item_row = _find_downloadable_item_for_object(
                cur,
                uid=uid,
                file_object_id=file_object_id,
                owner_user_id=owner_user_id,
            )

            # 通常DLは token の所有者/課金対象者本人だけを許可する。
            # 共有DLは shared_item_inbox で共有受信済みのユーザーにも許可する。
            direct_user_allowed = uid in {owner_user_id, charge_user_id}
            shared_user_allowed = is_shared and item_row is not None
            if not direct_user_allowed and not shared_user_allowed:
                raise HTTPException(status_code=403, detail="download token is not for current user")

            cur.execute(
                "SELECT wrapped_dek, owner_user_id FROM file_wrapped_keys WHERE file_object_id=%s",
                (file_object_id,),
            )
            key_row = cur.fetchone()
            if not key_row:
                raise HTTPException(status_code=404, detail="wrapped DEK not found")

            # 現行のパスワードレス方式では DEK はアップロード所有者の
            # server wrapping key で包まれている。共有受信者本人の user_id では
            # 復号できないため、key_owner_user_id を使って復号する。
            key_owner = str(key_row["owner_user_id"])
            if key_owner != owner_user_id:
                raise HTTPException(status_code=403, detail="download key owner mismatch")

            cur.execute(
                "SELECT wrapped_master_key, salt, params_json FROM user_master_keys WHERE user_id=%s",
                (key_owner,),
            )
            mk_row = cur.fetchone()

    return {
        "file_object_id": file_object_id,
        "owner_user_id": owner_user_id,
        "charge_user_id": charge_user_id,
        "is_shared": is_shared,
        "key_owner_user_id": key_owner,
        "wrapped_dek": bytes(key_row["wrapped_dek"]),
        "wrapped_master_key": bytes(mk_row["wrapped_master_key"]) if mk_row else None,
        "salt": bytes(mk_row["salt"]) if mk_row else None,
        "params_json": str(mk_row.get("params_json") or "{}") if mk_row else "{}",
        "file_name": _safe_download_filename(requested_name or (item_row and item_row.get("name")) or None),
    }


def _server_wrapping_key(uid: str) -> bytes:
    material = f"tri-cloud:file-wrap:v1:{uid}".encode("utf-8")
    secret = str(JWT_SECRET).encode("utf-8")
    return hashlib.sha256(secret + b"\0" + material).digest()


def _unwrap_dek(ctx: Dict[str, Any], uid: str, password: Optional[str] = None) -> bytes:
    # 新方式: アップロード/ダウンロードごとのパスワード入力を廃止し、
    # 認可済みユーザーからの要求に対して、DEK の実際のラップ所有者で復号する。
    # 共有受信者の uid ではなく key_owner_user_id を使う点が重要。
    key_owner = str(ctx.get("key_owner_user_id") or ctx.get("owner_user_id") or uid)
    try:
        return unwrap_key(_server_wrapping_key(key_owner), ctx["wrapped_dek"])
    except Exception:
        pass

    # 旧方式互換: 以前パスワード付きでアップロードしたファイルだけ、
    # password が渡された場合に従来のユーザーマスターキー復号へフォールバックする。
    # この場合も、共有受信者ではなく key_owner のマスターキーを使う。
    if password and ctx.get("wrapped_master_key") and ctx.get("salt"):
        try:
            umk = unwrap_master_key_with_password(
                ctx["wrapped_master_key"],
                password,
                ctx["salt"],
                loads_params(ctx.get("params_json") or "{}"),
            )
            return unwrap_key(umk, ctx["wrapped_dek"])
        except Exception as exc:
            raise HTTPException(status_code=400, detail="wrapped key is invalid") from exc

    raise HTTPException(status_code=400, detail="download key is not compatible with passwordless mode; re-upload this file")


def _recv_ds(sock: zmq.Socket, phase: str) -> list[bytes]:
    try:
        return sock.recv_multipart()
    except zmq.Again as exc:
        raise RuntimeError({"status": "timeout", "phase": phase, "message": f"DataServer response timeout during {phase}"}) from exc
    except zmq.ZMQError as exc:
        raise RuntimeError({"status": "zmq_error", "phase": phase, "message": str(exc)}) from exc


def _recv_ds_json(sock: zmq.Socket, phase: str) -> Dict[str, Any]:
    frames = _recv_ds(sock, phase)
    if len(frames) < 2 or frames[0] != b"json":
        raise RuntimeError({"status": "invalid_response", "phase": phase, "frames": len(frames)})
    return jload(frames[1])


def _download_to_temp(download_token: str, dek: bytes, file_object_id: str, file_name: str) -> tuple[str, int]:
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.DEALER)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, DATA_SERVER_TIMEOUT_MS)
    sock.setsockopt(zmq.SNDTIMEO, DATA_SERVER_TIMEOUT_MS)
    sock.connect(DATA_SERVER_ENDPOINT)

    try:
        sock.send_multipart([b"json", jdump({"op": "download_begin", "token": download_token})])
        ready = _recv_ds_json(sock, "download_begin")
        if ready.get("status") != "ready":
            raise RuntimeError(ready)

        transfer_id = str(ready["transfer_id"])
        total_chunks = int(ready["total_chunks"])
        chunks: Dict[int, bytes] = {}
        got: Set[int] = set()

        def handle_stream(frames: list[bytes]) -> None:
            # [b"stream", transfer_id, chunk_id, hash, data]
            if len(frames) != 5:
                return
            _, tid_b, cid_b, hash_b, data = frames
            if tid_b.decode("utf-8") != transfer_id:
                return
            cid = int(cid_b.decode("utf-8"))
            if sha256_hex(data) != hash_b.decode("utf-8"):
                return
            aad = f"{file_object_id}:{cid}".encode("utf-8")
            chunks[cid] = decrypt_chunk(dek, data, aad)
            got.add(cid)

        while True:
            frames = _recv_ds(sock, "download_stream")
            if frames[0] == b"stream":
                handle_stream(frames)
                continue
            if frames[0] != b"json" or len(frames) < 2:
                continue
            j = jload(frames[1])
            if j.get("status") == "incomplete" and j.get("transfer_id") == transfer_id:
                missing = [int(x) for x in j.get("missing", [])]
                if missing:
                    sock.send_multipart([b"json", jdump({"op": "download_resend", "transfer_id": transfer_id, "missing": missing})])
                continue
            if j.get("status") == "done" and j.get("transfer_id") == transfer_id:
                missing2 = [i for i in range(total_chunks) if i not in got]
                if missing2:
                    sock.send_multipart([b"json", jdump({"op": "download_resend", "transfer_id": transfer_id, "missing": missing2})])
                    continue
                break
            if j.get("status") in {"error", "cap_reached"}:
                raise RuntimeError(j)

        suffix = os.path.splitext(file_name)[1] or ".bin"
        fd, temp_path = tempfile.mkstemp(prefix=DOWNLOAD_TEMP_PREFIX, suffix=suffix)
        total = 0
        with os.fdopen(fd, "wb") as out:
            for cid in range(total_chunks):
                data = chunks[cid]
                out.write(data)
                total += len(data)
        return temp_path, total
    finally:
        sock.close(0)


@router.post("/ui/download")
def ui_download(inp: UiDownloadIn, uid: str = Depends(current_user_id)) -> FileResponse:
    ctx = _lookup_download_context(inp.download_token, uid, inp.file_name)
    file_name = str(ctx["file_name"])
    dek = _unwrap_dek(ctx, uid, inp.password)
    try:
        temp_path, _total = _download_to_temp(inp.download_token, dek, str(ctx["file_object_id"]), file_name)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=exc.args[0] if exc.args else str(exc)) from exc

    media_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_name)}"}
    return FileResponse(temp_path, media_type=media_type, filename=file_name, headers=headers)
