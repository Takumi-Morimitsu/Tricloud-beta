# auth_util.py
# -*- coding: utf-8 -*-
"""
Auth utility (Phase3)
- Password hashing: prefer Argon2id (argon2-cffi). Fallback to PBKDF2-HMAC-SHA256 if argon2-cffi not installed.
- JWT (HS256): implemented with stdlib only (RFC 7519).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

# ----------------------
# Password hashing
# ----------------------
try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError
    _HAS_ARGON2 = True
    _PH = PasswordHasher()  # Argon2id default (Type.ID)
except Exception:  # pragma: no cover
    _HAS_ARGON2 = False
    _PH = None
    VerifyMismatchError = Exception

_PBKDF2_ITER = 210_000  # adjust per environment
TRICLOUD_ENV = os.environ.get("TRICLOUD_ENV", "development").strip().lower()
_configured_jwt_secret = os.environ.get("JWT_SECRET", "").strip()

if TRICLOUD_ENV == "production" and (
    not _configured_jwt_secret or _configured_jwt_secret == "dev-only-change-me"
):
    raise RuntimeError("JWT_SECRET must be set to a non-default value in production")
if TRICLOUD_ENV == "production" and len(_configured_jwt_secret) < 32:
    raise RuntimeError("JWT_SECRET must be at least 32 characters in production")

JWT_SECRET = _configured_jwt_secret or "dev-only-change-me"

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

def _pad64(s: str) -> bytes:
    s2 = s + "=" * ((4 - (len(s) % 4)) % 4)
    return s2.encode("ascii")

def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(_pad64(s))

def hash_password(password: str) -> str:
    if _HAS_ARGON2:
        return _PH.hash(password)

    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER, dklen=32)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ITER,
        _b64url(salt),
        _b64url(dk),
    )

def verify_password(password: str, stored: str) -> bool:
    if stored.startswith("$argon2") and _HAS_ARGON2:
        try:
            return _PH.verify(stored, password)
        except VerifyMismatchError:
            return False

    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, it_s, salt_s, dk_s = stored.split("$", 3)
            it = int(it_s)
            salt = _b64url_decode(salt_s)
            dk = _b64url_decode(dk_s)
            dk2 = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, it, dklen=len(dk))
            return hmac.compare_digest(dk, dk2)
        except Exception:
            return False

    return False

# ----------------------
# JWT (HS256)
# ----------------------
@dataclass
class TokenData:
    sub: str
    exp: int
    iat: int
    extra: Dict[str, Any]

class JWTError(Exception):
    pass

def jwt_encode(payload: Dict[str, Any], secret: str, exp_seconds: Optional[int] = None) -> str:
    now = int(time.time())
    body = dict(payload)
    body.setdefault("iat", now)
    if exp_seconds is not None:
        body["exp"] = now + int(exp_seconds)

    header = {"alg": "HS256", "typ": "JWT"}
    h = _b64url(json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    p = _b64url(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    msg = f"{h}.{p}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url(sig)}"

def jwt_decode(token: str, secret: str) -> TokenData:
    try:
        h_b64, p_b64, s_b64 = token.split(".", 2)
    except ValueError:
        raise JWTError("bad token format")

    msg = f"{h_b64}.{p_b64}".encode("ascii")
    sig = _b64url_decode(s_b64)
    expect = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expect):
        raise JWTError("bad signature")

    payload = json.loads(_b64url_decode(p_b64).decode("utf-8"))
    now = int(time.time())
    exp = int(payload.get("exp", 0))
    if exp and now > exp:
        raise JWTError("token expired")

    sub = str(payload.get("sub", ""))
    if not sub:
        raise JWTError("sub missing")

    iat = int(payload.get("iat", 0) or 0)
    extra = {k: v for k, v in payload.items() if k not in ("sub", "exp", "iat")}
    return TokenData(sub=sub, exp=exp, iat=iat, extra=extra)

def issue_access_token(user_id: str, secret: str, ttl_sec: int = 30 * 60) -> str:
    now = int(time.time())
    payload = {"sub": user_id, "iat": now, "exp": now + int(ttl_sec)}
    return jwt_encode(payload, secret)

def extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None
