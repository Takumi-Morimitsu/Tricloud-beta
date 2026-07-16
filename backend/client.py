# client.py
# -*- coding: utf-8 -*-
"""
client（テスト用）
- upload: ファイルをチャンクAES-GCM暗号化してアップロード（nodeは暗号文しか保持しない）
- download: token -> node→server→client でストリーム受信し、欠損があれば download_resend で埋める
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, Any, Set

import requests


def login(control_base: str, email: str, password: str) -> str:
    r = requests.post(f"{control_base}/auth/login", json={"email": email, "password": password})
    r.raise_for_status()
    return r.json()["access_token"]

import zmq

from crypto_common_keywrap import (
    jdump, jload, sha256_hex,
    encrypt_chunk, decrypt_chunk, new_file_key
)

from crypto_common_keywrap import (
    new_master_key,
    wrap_key,
    unwrap_key,
    wrap_master_key_with_password,
    unwrap_master_key_with_password,
    b64e,
    b64d,
    dumps_params,
    loads_params,
)

CHUNK_SIZE = 4 * 1024 * 1024


def _api_get(control_base: str, path: str, token: str):
    r = requests.get(f"{control_base}{path}", headers={"Authorization": f"Bearer {token}"})
    return r

def _api_post(control_base: str, path: str, token: str, payload: dict):
    r = requests.post(f"{control_base}{path}", headers={"Authorization": f"Bearer {token}"}, json=payload)
    return r

def _ensure_master_key(control_base: str, token: str, password: str) -> bytes:
    # 1) try fetch
    r = _api_get(control_base, "/keys/master", token)
    if r.status_code == 404:
        # 2) bootstrap
        umk = new_master_key()  # 256-bit
        wrapped_umk, salt, params = wrap_master_key_with_password(umk, password)
        payload = {
            "wrapped_master_key_b64": b64e(wrapped_umk),
            "salt_b64": b64e(salt),
            "kdf": str(params.get("kdf", "")),
            "params_json": dumps_params(params),
        }
        r2 = _api_post(control_base, "/keys/master", token, payload)
        r2.raise_for_status()
        return umk
    r.raise_for_status()
    j = r.json()
    wrapped_umk = b64d(j["wrapped_master_key_b64"])
    salt = b64d(j["salt_b64"])
    params = loads_params(j.get("params_json", "{}"))
    return unwrap_master_key_with_password(wrapped_umk, password, salt, params)

def _put_wrapped_dek(control_base: str, token: str, file_object_id: str, wrapped_dek: bytes) -> None:
    r = _api_post(control_base, f"/keys/file/{file_object_id}", token, {"wrapped_dek_b64": b64e(wrapped_dek)})
    r.raise_for_status()

def _get_dek(control_base: str, token: str, password: str, file_object_id: str) -> bytes:
    umk = _ensure_master_key(control_base, token, password)
    r = _api_get(control_base, f"/keys/file/{file_object_id}", token)
    r.raise_for_status()
    wrapped_dek = b64d(r.json()["wrapped_dek_b64"])
    return unwrap_key(umk, wrapped_dek)


def upload_file(server_endpoint: str, control_base: str, access_token: str, password: str, path: str) -> Dict[str, Any]:
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.DEALER)
    sock.connect(server_endpoint)

    file_name = os.path.basename(path)
    file_size = os.path.getsize(path)

    # init_upload
    sock.send_multipart([b"json", jdump({
        "op": "init_upload",
        "access_token": access_token,
        "file_name": file_name,
        "file_size": file_size,
        "chunk_size": CHUNK_SIZE
    })])

    frames = sock.recv_multipart()
    rep = jload(frames[1])

    if rep.get("status") == "ready":
        # ---- 従来（単一オブジェクト） ----
        session_id = rep["session_id"]
        file_object_id = rep["file_object_id"]

        umk = _ensure_master_key(control_base, access_token, password)
        dek = new_file_key()
        wrapped_dek = wrap_key(umk, dek)

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
                ack = jload(sock.recv_multipart()[1])
                if ack.get("status") != "ack":
                    raise RuntimeError({"chunk_failed": cid, "ack": ack})
                cid += 1

        sock.send_multipart([b"control", session_id.encode(), jdump({"op": "commit"})])
        done = jload(sock.recv_multipart()[1])
        if done.get("status") == "incomplete":
            raise RuntimeError(done)
        if done.get("status") not in ("uploaded", "replaced"):
            raise RuntimeError(done)

        # store wrapped DEK (server-side, still encrypted)
        _put_wrapped_dek(control_base, access_token, file_object_id, wrapped_dek)
        print("uploaded:", done)
        return done

    if rep.get("status") == "ready_multipart":
        # ---- 複数オブジェクト（multipart） ----
        upload_id = rep["upload_id"]
        parts = rep["parts"]
        chunk_size = int(rep.get("chunk_size", CHUNK_SIZE))

        umk = _ensure_master_key(control_base, access_token, password)
        # 1ファイル=同一DEKで暗号化
        dek = new_file_key()
        wrapped_dek = wrap_key(umk, dek)

        with open(path, "rb") as f:
            for p in parts:
                session_id = p["session_id"]
                file_object_id = p["file_object_id"]
                part_size = int(p["part_size"])

                remaining = part_size
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

                    ack = jload(sock.recv_multipart()[1])
                    if ack.get("status") != "ack":
                        raise RuntimeError({"chunk_failed": cid, "ack": ack, "part_index": p.get("part_index")})
                    remaining -= len(plain)
                    cid += 1

                # commit each part
                sock.send_multipart([b"control", session_id.encode(), jdump({"op": "commit"})])
                done = jload(sock.recv_multipart()[1])
                if done.get("status") != "part_uploaded":
                    raise RuntimeError({"part_commit_failed": done})

        # finalize multipart -> logical item
        sock.send_multipart([b"json", jdump({"op": "commit_multipart", "upload_id": upload_id})])
        fin = jload(sock.recv_multipart()[1])
        if fin.get("status") != "uploaded":
            raise RuntimeError({"multipart_finalize_failed": fin})

        # store wrapped DEK for each part object
        for p in parts:
            _put_wrapped_dek(control_base, access_token, p["file_object_id"], wrapped_dek)
        print("uploaded multipart:", fin)
        return fin

    raise RuntimeError(rep)


def download_file(server_endpoint: str, control_base: str, email: str, password: str, item_id: str, out_path: str) -> None:
    # 1) token
    token = login(control_base, email, password)
    r = requests.post(f"{control_base}/items/{item_id}/download_token", headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    tok = r.json()
    token = tok["token"]
    file_object_id = tok["file_object_id"]

    dek = _get_dek(control_base, token, password, file_object_id)

    # 2) download_begin
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.DEALER)
    sock.connect(server_endpoint)

    sock.send_multipart([b"json", jdump({"op":"download_begin", "token": token})])

    ready = jload(sock.recv_multipart()[1])
    if ready.get("status") != "ready":
        raise RuntimeError(ready)

    transfer_id = ready["transfer_id"]
    total_chunks = int(ready["total_chunks"])

    # 3) receive loop（順序崩れOK）
    chunks: Dict[int, bytes] = {}
    got: Set[int] = set()

    def handle_stream(frames):
        # [b"stream", transfer_id, chunk_id, hash, data]
        _, tid_b, cid_b, hash_b, data = frames
        if tid_b.decode() != transfer_id:
            return
        cid = int(cid_b.decode())
        if sha256_hex(data) != hash_b.decode():
            # 破損は欠損扱いにして後でresendで埋める
            return
        aad = f"{file_object_id}:{cid}".encode("utf-8")
        plain = decrypt_chunk(dek, data, aad)
        chunks[cid] = plain
        got.add(cid)

    while True:
        frames = sock.recv_multipart()
        if frames[0] == b"stream":
            handle_stream(frames)
            continue

        if frames[0] == b"json":
            j = jload(frames[1])

            if j.get("status") == "incomplete" and j.get("transfer_id") == transfer_id:
                missing = [int(x) for x in j.get("missing", [])]
                if not missing:
                    continue
                # resend
                sock.send_multipart([b"json", jdump({
                    "op":"download_resend",
                    "transfer_id": transfer_id,
                    "missing": missing
                })])
                continue

            if j.get("status") == "done" and j.get("transfer_id") == transfer_id:
                # 念のためローカルでも欠損チェック
                missing2 = [i for i in range(total_chunks) if i not in got]
                if missing2:
                    sock.send_multipart([b"json", jdump({
                        "op":"download_resend",
                        "transfer_id": transfer_id,
                        "missing": missing2
                    })])
                    continue
                break

            if j.get("status") == "error":
                raise RuntimeError(j)

    # 4) assemble
    with open(out_path, "wb") as out:
        for cid in range(total_chunks):
            out.write(chunks[cid])

    print("downloaded:", out_path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["upload","download"], required=True)
    ap.add_argument("--server", default="tcp://127.0.0.1:8888")
    ap.add_argument("--control", default="http://127.0.0.1:8000")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--file", help="upload file path")
    ap.add_argument("--item-id", help="download item_id")
    ap.add_argument("--out", default="download.bin")
    args = ap.parse_args()

    if args.mode == "upload":
        if not args.file:
            print("--file required")
            sys.exit(1)
        token = login(args.control, args.email, args.password)
        res = upload_file(args.server, args.control, token, args.password, args.file)
        print("IMPORTANT: item_id =", res["item_id"])  # これがdownloadに必要
    else:
        if not args.item_id:
            print("--item-id required")
            sys.exit(1)
        download_file(args.server, args.control, args.email, args.password, args.item_id, args.out)


if __name__ == "__main__":
    main()
