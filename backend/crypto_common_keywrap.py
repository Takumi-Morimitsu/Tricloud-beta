# -*- coding: utf-8 -*-
"""
crypto_common_keywrap.py
- 共通ユーティリティ（JSON / hash / token / AES-GCM）
- Key Wrapping ユーティリティ（UMK / DEK / password-derived KEK）

統合元:
- 共通部分.py
- マスターキー作成.py

このファイルを使うことで、common.py と keywrap_util.py の責務を1ファイルに集約できます。
既存コードからの移行をしやすくするため、元ファイルの公開関数名は可能な限り維持しています。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from typing import Any, Dict, Tuple

# cryptography is intentionally imported lazily inside the functions that need it.
# The storage-node process only needs JSON/hash helpers from this file, so importing
# cryptography at module load time would unnecessarily break the bundled runtime.


# ------------------------
# JSON helpers
# ------------------------
def jdump(obj: Dict[str, Any]) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def jload(bts: bytes) -> Dict[str, Any]:
    return json.loads(bts.decode("utf-8"))


def b(x: str) -> bytes:
    return x.encode("utf-8")


def s(x: bytes) -> str:
    return x.decode("utf-8")


def now_ts() -> float:
    return time.time()


# ------------------------
# Base64 helpers (urlsafe)
# ------------------------
def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64d(data: str) -> bytes:
    pad = "=" * ((4 - (len(data) % 4)) % 4)
    return base64.urlsafe_b64decode((data + pad).encode("ascii"))


# ------------------------
# Hash / math / tokens
# ------------------------
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ceil_div(a: int, b: int) -> int:
    return (a + b - 1) // b


def new_share_id() -> str:
    return secrets.token_urlsafe(32)


def new_token() -> str:
    return secrets.token_urlsafe(32)


# ------------------------
# AES-GCM helpers
# ------------------------
def new_file_key() -> bytes:
    # AES-256
    return os.urandom(32)


def encrypt_chunk(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aes = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aes.encrypt(nonce, plaintext, aad)
    return nonce + ciphertext


def decrypt_chunk(key: bytes, blob: bytes, aad: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(blob) < 13:
        raise ValueError("cipher blob too short")
    nonce = blob[:12]
    ciphertext = blob[12:]
    aes = AESGCM(key)
    return aes.decrypt(nonce, ciphertext, aad)


# ------------------------
# Master key helpers
# ------------------------
def new_master_key() -> bytes:
    # 256-bit master key
    return os.urandom(32)


# ------------------------
# Password -> PDK (Key Encryption Key)
# ------------------------
def derive_pdk(password: str, salt: bytes, *, params: Dict[str, Any]) -> bytes:
    """
    32-byte KEK を返す。
    params:
      {"kdf":"argon2id","time_cost":...,"memory_cost":...,"parallelism":...}
      or {"kdf":"pbkdf2","iterations":...}
    """
    kdf = str(params.get("kdf") or "").lower()
    if kdf == "argon2id":
        try:
            from argon2.low_level import Type, hash_secret_raw  # type: ignore

            t = int(params.get("time_cost", 3))
            m = int(params.get("memory_cost", 65536))
            p = int(params.get("parallelism", 1))
            return hash_secret_raw(
                secret=password.encode("utf-8"),
                salt=salt,
                time_cost=t,
                memory_cost=m,
                parallelism=p,
                hash_len=32,
                type=Type.ID,
            )
        except Exception:
            pass

    iterations = int(params.get("iterations", 210_000))
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)



def default_kdf_params() -> Dict[str, Any]:
    """Argon2id が使えれば優先し、なければ PBKDF2 にフォールバックする。"""
    try:
        import argon2  # noqa: F401

        return {"kdf": "argon2id", "time_cost": 3, "memory_cost": 65536, "parallelism": 1}
    except Exception:
        return {"kdf": "pbkdf2", "iterations": 210_000}


# ------------------------
# Wrap / unwrap
# ------------------------
def wrap_key(wrapping_key: bytes, key_to_wrap: bytes) -> bytes:
    from cryptography.hazmat.primitives.keywrap import aes_key_wrap_with_padding
    return aes_key_wrap_with_padding(wrapping_key, key_to_wrap)


def unwrap_key(wrapping_key: bytes, wrapped_key: bytes) -> bytes:
    from cryptography.hazmat.primitives.keywrap import aes_key_unwrap_with_padding
    return aes_key_unwrap_with_padding(wrapping_key, wrapped_key)



def wrap_master_key_with_password(master_key: bytes, password: str) -> Tuple[bytes, bytes, Dict[str, Any]]:
    salt = os.urandom(16)
    params = default_kdf_params()
    pdk = derive_pdk(password, salt, params=params)
    wrapped = wrap_key(pdk, master_key)
    return wrapped, salt, params



def unwrap_master_key_with_password(
    wrapped_master_key: bytes,
    password: str,
    salt: bytes,
    params: Dict[str, Any],
) -> bytes:
    pdk = derive_pdk(password, salt, params=params)
    return unwrap_key(pdk, wrapped_master_key)



def dumps_params(params: Dict[str, Any]) -> str:
    return json.dumps(params, ensure_ascii=False, separators=(",", ":"))



def loads_params(payload: str) -> Dict[str, Any]:
    try:
        data = json.loads(payload)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
