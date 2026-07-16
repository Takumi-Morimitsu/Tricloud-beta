# node.py
# -*- coding: utf-8 -*-
"""
ストレージノード（DEALER）
- server(node_router=ROUTER) に接続し、identity=node_id で識別される。
  identityはconnect前に設定するのが定石。:contentReference[oaicite:3]{index=3}

対応op:
- heartbeat
- init_session / chunk_ack / commit_check / commit_finalize
- stream_object_begin / stream_object_resend（ダウンロード安定化）
- store_object_begin / store / store_object_end（修復・コピーの受け皿）
- delete_object
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import traceback
from datetime import datetime, timezone

# psutil が利用できる場合は優先して使用（より詳細な環境差分を吸収しやすい）
try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None  # type: ignore

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set

_TRICLOUD_DLL_HANDLES = []


def _configure_console_streams() -> None:
    """Electron のログファイルへ即時出力できるよう stdout/stderr を行単位でフラッシュする。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", line_buffering=True, write_through=True)
        except Exception:
            pass


def _log(level: str, message: str, **fields: object) -> None:
    timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
    suffix = ""
    if fields:
        try:
            suffix = " " + json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            suffix = " " + repr(fields)
    print(f"[tricloud-node {timestamp}] {level.upper()} {message}{suffix}", flush=True)


_configure_console_streams()

def _tricloud_ensure_bundled_python_runtime_paths() -> None:
    try:
        exe_dir = Path(sys.executable).resolve().parent  # type: ignore[name-defined]
    except Exception:
        exe_dir = Path()
    candidates = []
    if exe_dir:
        candidates.append(exe_dir / "Lib" / "site-packages")
    candidates.append(Path(__file__).resolve().parent.parent / "runtime" / "python" / "Lib" / "site-packages")
    for site_packages in candidates:
        try:
            if not site_packages.is_dir():
                continue
            sp = str(site_packages)
            if sp not in sys.path:  # type: ignore[name-defined]
                sys.path.insert(0, sp)  # type: ignore[name-defined]
            dll_dirs = [site_packages / "pyzmq.libs", site_packages / "zmq.libs"]
            try:
                dll_dirs.extend([p for p in site_packages.iterdir() if p.is_dir() and p.name.endswith(".libs")])
            except Exception:
                pass
            for dll_dir in dll_dirs:
                if not dll_dir.is_dir():
                    continue
                dll_s = str(dll_dir)
                if hasattr(os, "add_dll_directory"):
                    try:
                        _TRICLOUD_DLL_HANDLES.append(os.add_dll_directory(dll_s))
                    except Exception:
                        pass
                old_path = os.environ.get("PATH", "")
                parts = [x for x in old_path.split(os.pathsep) if x]
                if dll_s not in parts:
                    os.environ["PATH"] = dll_s + os.pathsep + old_path
        except Exception:
            pass

_tricloud_ensure_bundled_python_runtime_paths()

import zmq

from crypto_common_keywrap import (
    jdump, jload, now_ts, sha256_hex, b, s, ceil_div
)

# ------------------------
# パス管理
# ------------------------
def obj_dir(base: str, file_object_id: str) -> str:
    return os.path.join(base, "objects", file_object_id)

def chunk_path(base: str, file_object_id: str, cid: int) -> str:
    return os.path.join(obj_dir(base, file_object_id), "chunks", f"{cid}.bin")

def meta_path(base: str, file_object_id: str) -> str:
    return os.path.join(obj_dir(base, file_object_id), "meta.json")

def session_tmp_dir(base: str, session_id: str) -> str:
    return os.path.join(base, "tmp", "sessions", session_id)

def transfer_tmp_dir(base: str, transfer_id: str) -> str:
    return os.path.join(base, "tmp", "transfers", transfer_id)

# ------------------------
# 状態
# ------------------------
@dataclass
class UploadSession:
    session_id: str
    file_object_id: str
    file_size: int
    chunk_size: int
    expires_at: float

@dataclass
class TransferSession:
    transfer_id: str
    file_object_id: str
    file_size: int
    chunk_size: int
    total_chunks: int

# ------------------------
# Node
# ------------------------
class Node:
    def __init__(self, node_id: str, server: str, storage_base: str, capacity_bytes: int, node_api_key: str) -> None:
        self.node_id = node_id
        self.server = server
        self.base = storage_base
        self.capacity_bytes = capacity_bytes
        self.node_api_key = node_api_key

        os.makedirs(os.path.join(self.base, "objects"), exist_ok=True)
        os.makedirs(os.path.join(self.base, "tmp", "sessions"), exist_ok=True)
        os.makedirs(os.path.join(self.base, "tmp", "transfers"), exist_ok=True)

        self.ctx = zmq.Context.instance()
        self.sock = self.ctx.socket(zmq.DEALER)

        # 接続断後の復帰を早め、未接続中の送信を無制限にキューへ積まない。
        # すべて connect() より前に設定する。
        socket_option_specs = [
            ("IDENTITY", b(self.node_id)),
            ("LINGER", 0),
            ("IMMEDIATE", 1),
            ("CONNECT_TIMEOUT", 5_000),
            ("HANDSHAKE_IVL", 5_000),
            ("RECONNECT_IVL", 1_000),
            ("RECONNECT_IVL_MAX", 5_000),
            ("HEARTBEAT_IVL", 3_000),
            ("HEARTBEAT_TIMEOUT", 10_000),
            ("HEARTBEAT_TTL", 15_000),
            ("RCVTIMEO", 1_000),
            ("SNDTIMEO", 1_000),
        ]
        for option_name, value in socket_option_specs:
            option = getattr(zmq, option_name, None)
            if option is None:
                _log("WARN", "socket option is unavailable in this PyZMQ build", option=option_name)
                continue
            try:
                self.sock.setsockopt(option, value)
            except zmq.ZMQError as exc:
                _log("WARN", "socket option could not be applied", option=option_name, error=str(exc))

        self.upload_sessions: Dict[str, UploadSession] = {}
        self.transfers: Dict[str, TransferSession] = {}

        self._stop = False
        self._heartbeat_ever_succeeded = False
        self._heartbeat_failing = False
        self._last_heartbeat_error = ""
        self._last_heartbeat_error_log_at = 0.0
        self._last_receive_error_log_at = 0.0

        _log(
            "INFO",
            "node initialized",
            node_id=self.node_id,
            server=self.server,
            storage_base=os.path.abspath(self.base),
            capacity_bytes=int(self.capacity_bytes),
            api_key_present=bool(self.node_api_key),
            pyzmq_version=getattr(zmq, "__version__", "unknown"),
        )
        self.sock.connect(self.server)
        _log("INFO", "ZeroMQ connect initiated", server=self.server)

    # ------------------------
    # Heartbeat
    # ------------------------
    def _send_heartbeat(self) -> bool:
        """DataServer にノード生存通知を送り、送信キュー投入の成否を記録する。

        APIキーそのものはログへ出さない。通信断中は同じエラーを大量に出さず、
        状態変化時または30秒ごとにだけ記録する。
        """
        try:
            self.sock.send_multipart([b"json", jdump({
                "op": "heartbeat",
                "capacity_bytes": int(self.capacity_bytes),
                "node_api_key": self.node_api_key,
                "meta_json": "{}",
            })])
            if not self._heartbeat_ever_succeeded:
                _log("INFO", "first heartbeat queued", server=self.server)
            elif self._heartbeat_failing:
                _log("INFO", "heartbeat sending recovered", server=self.server)
            self._heartbeat_ever_succeeded = True
            self._heartbeat_failing = False
            self._last_heartbeat_error = ""
            return True
        except (zmq.Again, zmq.ZMQError) as exc:
            now_mono = time.monotonic()
            error_text = f"{type(exc).__name__}: {exc}"
            should_log = (
                not self._heartbeat_failing
                or error_text != self._last_heartbeat_error
                or now_mono - self._last_heartbeat_error_log_at >= 30.0
            )
            if should_log:
                _log(
                    "WARN",
                    "heartbeat send failed; waiting for ZeroMQ reconnect",
                    server=self.server,
                    error=error_text,
                )
                self._last_heartbeat_error_log_at = now_mono
            self._heartbeat_failing = True
            self._last_heartbeat_error = error_text
            return False

    def _heartbeat_loop(self) -> None:
        """旧実装との互換用。新規コードでは使用しない。"""
        while not self._stop:
            self._send_heartbeat()
            time.sleep(3.0)

    # ------------------------
    # Upload helpers
    # ------------------------
    def _write_tmp_chunk(self, session_id: str, cid: int, data: bytes) -> None:
        d = session_tmp_dir(self.base, session_id)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{cid}.bin")
        with open(p, "wb") as f:
            f.write(data)

    def _has_tmp_chunk(self, session_id: str, cid: int) -> bool:
        return os.path.exists(os.path.join(session_tmp_dir(self.base, session_id), f"{cid}.bin"))

    def _finalize_upload(self, session: UploadSession) -> None:
        total = ceil_div(session.file_size, session.chunk_size)
        dst_chunks_dir = os.path.join(obj_dir(self.base, session.file_object_id), "chunks")
        os.makedirs(dst_chunks_dir, exist_ok=True)

        src_dir = session_tmp_dir(self.base, session.session_id)
        for i in range(total):
            src = os.path.join(src_dir, f"{i}.bin")
            dst = os.path.join(dst_chunks_dir, f"{i}.bin")
            shutil.move(src, dst)

        # meta
        mp = meta_path(self.base, session.file_object_id)
        with open(mp, "wb") as f:
            f.write(jdump({
                "file_size": session.file_size,
                "chunk_size": session.chunk_size,
                "total_chunks": total,
                "created_at": now_ts(),
            }))

        shutil.rmtree(src_dir, ignore_errors=True)

    def _load_meta(self, file_object_id: str) -> Optional[dict]:
        mp = meta_path(self.base, file_object_id)
        if not os.path.exists(mp):
            return None
        with open(mp, "rb") as f:
            return jload(f.read())

    # ------------------------
    # Store (repair/copy receive)
    # ------------------------
    def _transfer_write_chunk(self, transfer_id: str, cid: int, data: bytes) -> None:
        d = transfer_tmp_dir(self.base, transfer_id)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{cid}.bin")
        with open(p, "wb") as f:
            f.write(data)

    def _transfer_finalize(self, t: TransferSession) -> None:
        total = t.total_chunks
        dst_chunks_dir = os.path.join(obj_dir(self.base, t.file_object_id), "chunks")
        os.makedirs(dst_chunks_dir, exist_ok=True)

        src_dir = transfer_tmp_dir(self.base, t.transfer_id)
        for i in range(total):
            src = os.path.join(src_dir, f"{i}.bin")
            dst = os.path.join(dst_chunks_dir, f"{i}.bin")
            if not os.path.exists(src):
                raise RuntimeError(f"missing stored chunk: {i}")
            shutil.move(src, dst)

        with open(meta_path(self.base, t.file_object_id), "wb") as f:
            f.write(jdump({
                "file_size": t.file_size,
                "chunk_size": t.chunk_size,
                "total_chunks": t.total_chunks,
                "created_at": now_ts(),
            }))
        shutil.rmtree(src_dir, ignore_errors=True)

    # ------------------------
    # Stream send (download)
    # ------------------------
    def _send_stream_chunks(self, transfer_id: str, file_object_id: str, cids: List[int]) -> None:
        for cid in cids:
            p = chunk_path(self.base, file_object_id, cid)
            if not os.path.exists(p):
                continue
            with open(p, "rb") as f:
                data = f.read()
            h = sha256_hex(data)
            # [b"stream", transfer_id, chunk_id, hash_hex, data]
            self.sock.send_multipart([b"stream", b(transfer_id), b(str(cid)), b(h), data])

    # ------------------------
    # delete
    # ------------------------
    def _delete_object(self, file_object_id: str) -> None:
        d = obj_dir(self.base, file_object_id)
        shutil.rmtree(d, ignore_errors=True)

    # ------------------------
    # main loop
    # ------------------------
    def serve_forever(self) -> None:
        # PyZMQ/ZeroMQ の Socket はスレッドセーフではない。
        # 以前は heartbeat 用スレッドと main loop が同じ DEALER socket を共有していたため、
        # heartbeat が止まる・不安定になる可能性があった。
        # ここでは heartbeat も受信処理も同じスレッドで行う。
        next_heartbeat_at = 0.0
        _log("INFO", "node event loop started", heartbeat_interval_seconds=3.0)

        while True:
            now = time.monotonic()
            if now >= next_heartbeat_at:
                self._send_heartbeat()
                next_heartbeat_at = now + 3.0

            try:
                parts = self.sock.recv_multipart()
            except zmq.Again:
                continue
            except KeyboardInterrupt:
                _log("INFO", "keyboard interrupt received; stopping node")
                break
            except zmq.ZMQError as exc:
                if getattr(exc, "errno", None) == getattr(zmq, "ETERM", None):
                    _log("INFO", "ZeroMQ context terminated; stopping node")
                    break
                now_mono = time.monotonic()
                if now_mono - self._last_receive_error_log_at >= 30.0:
                    _log("WARN", "ZeroMQ receive failed", error=f"{type(exc).__name__}: {exc}")
                    self._last_receive_error_log_at = now_mono
                continue

            if not parts:
                continue

            kind = parts[0]

            # -------- JSON control --------
            if kind == b"json":
                msg = jload(parts[1])
                op = msg.get("op")

                if op == "init_session":
                    session_id = str(msg["session_id"])
                    file_object_id = str(msg["file_object_id"])
                    file_size = int(msg["file_size"])
                    chunk_size = int(msg["chunk_size"])
                    expires_at = float(msg.get("expires_at", now_ts() + 3600))

                    self.upload_sessions[session_id] = UploadSession(
                        session_id=session_id,
                        file_object_id=file_object_id,
                        file_size=file_size,
                        chunk_size=chunk_size,
                        expires_at=expires_at,
                    )
                    os.makedirs(session_tmp_dir(self.base, session_id), exist_ok=True)
                    self.sock.send_multipart([b"json", jdump({
                        "op": "init_session_reply",
                        "session_id": session_id,
                        "status": "ready",
                    })])

                elif op == "commit_check":
                    session_id = str(msg["session_id"])
                    sess = self.upload_sessions.get(session_id)
                    if not sess:
                        self.sock.send_multipart([b"json", jdump({
                            "op":"commit_check_reply", "session_id": session_id, "status":"error", "missing":[]
                        })])
                        continue

                    total = ceil_div(sess.file_size, sess.chunk_size)
                    missing = [i for i in range(total) if not self._has_tmp_chunk(session_id, i)]
                    self.sock.send_multipart([b"json", jdump({
                        "op": "commit_check_reply",
                        "session_id": session_id,
                        "status": "ok",
                        "missing": missing,
                    })])

                elif op == "commit_finalize":
                    session_id = str(msg["session_id"])
                    sess = self.upload_sessions.get(session_id)
                    if not sess:
                        self.sock.send_multipart([b"json", jdump({
                            "op":"commit_finalize_reply", "session_id": session_id, "status":"error"
                        })])
                        continue
                    try:
                        self._finalize_upload(sess)
                        self.sock.send_multipart([b"json", jdump({
                            "op":"commit_finalize_reply",
                            "session_id": session_id,
                            "status":"ok",
                        })])
                    except Exception as e:
                        self.sock.send_multipart([b"json", jdump({
                            "op":"commit_finalize_reply",
                            "session_id": session_id,
                            "status":"error",
                            "message": str(e),
                        })])
                    finally:
                        # session掃除（tmpはfinalizeで消える）
                        self.upload_sessions.pop(session_id, None)

                elif op == "delete_session":
                    session_id = str(msg["session_id"])
                    self.upload_sessions.pop(session_id, None)
                    shutil.rmtree(session_tmp_dir(self.base, session_id), ignore_errors=True)

                elif op == "delete_object":
                    file_object_id = str(msg["file_object_id"])
                    self._delete_object(file_object_id)
                    self.sock.send_multipart([b"json", jdump({
                        "op":"delete_object_reply",
                        "file_object_id": file_object_id,
                        "status":"ok"
                    })])

                # ---- download stream begin ----
                elif op == "stream_object_begin":
                    transfer_id = str(msg["transfer_id"])
                    file_object_id = str(msg["file_object_id"])

                    meta = self._load_meta(file_object_id)
                    if not meta:
                        self.sock.send_multipart([b"json", jdump({
                            "op":"stream_object_ready",
                            "transfer_id": transfer_id,
                            "status":"error",
                        })])
                        continue

                    total_chunks = int(meta["total_chunks"])
                    self.sock.send_multipart([b"json", jdump({
                        "op":"stream_object_ready",
                        "transfer_id": transfer_id,
                        "status":"ready",
                        "total_chunks": total_chunks,
                    })])

                    self._send_stream_chunks(transfer_id, file_object_id, list(range(total_chunks)))
                    self.sock.send_multipart([b"json", jdump({
                        "op":"stream_object_end",
                        "transfer_id": transfer_id,
                        "status":"ok",
                    })])

                elif op == "stream_object_resend":
                    transfer_id = str(msg["transfer_id"])
                    file_object_id = str(msg["file_object_id"])
                    missing = msg.get("missing", [])
                    meta = self._load_meta(file_object_id)
                    if not meta:
                        self.sock.send_multipart([b"json", jdump({
                            "op":"stream_object_resend_reply",
                            "transfer_id": transfer_id,
                            "status":"error",
                        })])
                        continue
                    cids = [int(x) for x in missing if isinstance(x, (int, float, str))]
                    self._send_stream_chunks(transfer_id, file_object_id, cids)
                    self.sock.send_multipart([b"json", jdump({
                        "op":"stream_object_resend_reply",
                        "transfer_id": transfer_id,
                        "status":"ok",
                    })])

                # ---- store begin/end (repair/copy receiver) ----
                elif op == "store_object_begin":
                    transfer_id = str(msg["transfer_id"])
                    file_object_id = str(msg["file_object_id"])
                    file_size = int(msg["file_size"])
                    chunk_size = int(msg["chunk_size"])
                    total = ceil_div(file_size, chunk_size)

                    self.transfers[transfer_id] = TransferSession(
                        transfer_id=transfer_id,
                        file_object_id=file_object_id,
                        file_size=file_size,
                        chunk_size=chunk_size,
                        total_chunks=total,
                    )
                    os.makedirs(transfer_tmp_dir(self.base, transfer_id), exist_ok=True)
                    self.sock.send_multipart([b"json", jdump({
                        "op":"store_object_ready",
                        "transfer_id": transfer_id,
                        "status":"ready",
                        "total_chunks": total,
                    })])

                elif op == "store_object_end":
                    transfer_id = str(msg["transfer_id"])
                    t = self.transfers.get(transfer_id)
                    if not t:
                        self.sock.send_multipart([b"json", jdump({
                            "op":"store_object_end_reply",
                            "transfer_id": transfer_id,
                            "status":"error",
                        })])
                        continue
                    try:
                        self._transfer_finalize(t)
                        self.sock.send_multipart([b"json", jdump({
                            "op":"store_object_end_reply",
                            "transfer_id": transfer_id,
                            "status":"ok",
                        })])
                    except Exception as e:
                        self.sock.send_multipart([b"json", jdump({
                            "op":"store_object_end_reply",
                            "transfer_id": transfer_id,
                            "status":"error",
                            "message": str(e),
                        })])
                    finally:
                        self.transfers.pop(transfer_id, None)
                        shutil.rmtree(transfer_tmp_dir(self.base, transfer_id), ignore_errors=True)

                else:
                    # 未知opは無視
                    continue

            # -------- upload data chunk --------
            elif kind == b"data":
                # [b"data", session_id, chunk_id, hash_hex, data]
                if len(parts) != 5:
                    continue
                session_id = s(parts[1])
                cid = int(s(parts[2]))
                h = s(parts[3])
                data = parts[4]

                sess = self.upload_sessions.get(session_id)
                if not sess:
                    # 形式的にack（サーバ側の再送制御に任せる）
                    self.sock.send_multipart([b"json", jdump({
                        "op":"chunk_ack", "session_id": session_id, "chunk_id": cid, "status":"ack"
                    })])
                    continue

                # hash検証（暗号文に対して）
                if sha256_hex(data) != h:
                    # 受け取ったが不正 → ackしつつ commit_checkでmissing扱いにするため保存しない
                    self.sock.send_multipart([b"json", jdump({
                        "op":"chunk_ack", "session_id": session_id, "chunk_id": cid, "status":"ack"
                    })])
                    continue

                self._write_tmp_chunk(session_id, cid, data)
                self.sock.send_multipart([b"json", jdump({
                    "op":"chunk_ack", "session_id": session_id, "chunk_id": cid, "status":"ack"
                })])

            # -------- store chunk (repair/copy) --------
            elif kind == b"store":
                # [b"store", transfer_id, chunk_id, hash_hex, data]
                if len(parts) != 5:
                    continue
                transfer_id = s(parts[1])
                cid = int(s(parts[2]))
                h = s(parts[3])
                data = parts[4]
                t = self.transfers.get(transfer_id)
                if not t:
                    # ignore
                    continue
                if sha256_hex(data) != h:
                    continue
                self._transfer_write_chunk(transfer_id, cid, data)
                # ACKは必須ではないが、デバッグ用に返せる
                self.sock.send_multipart([b"json", jdump({
                    "op":"store_ack", "transfer_id": transfer_id, "chunk_id": cid, "status":"ack"
                })])

            else:
                continue

        self._stop = True
        try:
            self.sock.close(linger=0)
        except Exception as exc:
            _log("WARN", "socket close failed", error=str(exc))
        _log("INFO", "node event loop stopped")



def _disk_usage_bytes(path: str) -> tuple[int, int, int]:
    """指定パスが属するボリュームの (total, used, free) を bytes で返す。

    - 可能なら psutil.disk_usage を使用
    - psutil が無い場合は shutil.disk_usage にフォールバック
    """
    abs_path = os.path.abspath(path)
    if psutil is not None:
        du = psutil.disk_usage(abs_path)
        return int(du.total), int(du.used), int(du.free)
    du2 = shutil.disk_usage(abs_path)
    return int(du2.total), int(du2.used), int(du2.free)


def _bytes_to_gib_floor(b: int) -> int:
    return max(0, int(b // (1024 * 1024 * 1024)))


def _prompt_int(prompt: str, default: int) -> int:
    """コンソール入力（空ならdefault）。非対話環境はdefaultを返す。"""
    try:
        s = input(prompt).strip()
    except Exception:
        return default
    if not s:
        return default
    try:
        return int(s)
    except ValueError:
        return default


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--node-id", required=True)
    ap.add_argument("--node-api-key", default=os.environ.get("TRICLOUD_NODE_API_KEY", ""))
    ap.add_argument("--server", default="tcp://127.0.0.1:9999")
    ap.add_argument("--storage-dir", default="./node_store")
    ap.add_argument("--capacity-gb", type=int, default=None)
    ap.add_argument("--auto-capacity", action="store_true", help="空き容量の90%を自動で提供容量に設定")
    ap.add_argument("--interactive-capacity", action="store_true", help="空き容量の90%を上限として対話的に提供容量(GB)を入力")

    args = ap.parse_args()

    base = os.path.join(args.storage_dir, args.node_id)

    # --- 提供可能容量の計測（ディスク実空きの 90% を上限として提示） ---
    # 全空きを提供すると OS や他アプリの書き込みが詰まりやすいので、あえて 90% に丸めます。
    total_b, used_b, free_b = _disk_usage_bytes(args.storage_dir)
    offerable_b = int(free_b * 0.90)
    max_gb = _bytes_to_gib_floor(offerable_b)

    print("=== ノード用ストレージ情報 ===")
    print(f"対象パス: {os.path.abspath(args.storage_dir)}")
    print(f"総容量: {total_b / (1024**3):.2f} GiB")
    print(f"実空き容量: {free_b / (1024**3):.2f} GiB")
    print(f"提供可能として表示する空き(90%): {offerable_b / (1024**3):.2f} GiB")
    print("※ 全提供にするとOSが死ぬ（ディスク逼迫で不安定化する）ので、本来の空き容量の90%までしか表示していません。")

    # --- 提供容量(GB)の決定 ---
    cap_gb: Optional[int] = args.capacity_gb
    if cap_gb is None:
        # 指定が無い場合：interactive を優先、次に auto、最後にデフォルト(20GB)
        default_gb = min(20, max_gb) if max_gb > 0 else 0
        if args.interactive_capacity:
            cap_gb = _prompt_int(
                f"このノードが提供する容量(GB)を入力してください [最大 {max_gb}GB] (Enterで既定 {default_gb}GB): ",
                default_gb,
            )
        elif args.auto_capacity:
            cap_gb = default_gb if default_gb == max_gb else max_gb
        else:
            cap_gb = default_gb

    # 上限チェック（超えていたら丸めて警告）
    if max_gb > 0 and cap_gb > max_gb:
        print(f"[WARN] 指定容量 {cap_gb}GB は上限 {max_gb}GB を超えています。上限に丸めます。")
        cap_gb = max_gb
    if cap_gb <= 0:
        raise SystemExit("提供容量が 0GB です。--capacity-gb で指定するか、--interactive-capacity を使用してください。")

    cap = int(cap_gb) * 1024 * 1024 * 1024

    if not args.node_api_key:
        raise SystemExit("--node-api-key または TRICLOUD_NODE_API_KEY が必要です。")

    n = Node(
        node_id=args.node_id,
        server=args.server,
        storage_base=base,
        capacity_bytes=cap,
        node_api_key=args.node_api_key,
    )
    n.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _log("INFO", "node process interrupted")
    except Exception as exc:
        _log("ERROR", "unhandled node exception", error=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        raise
