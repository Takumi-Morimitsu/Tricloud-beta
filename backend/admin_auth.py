# -*- coding: utf-8 -*-
"""Authentication helpers for the separate Tricloud admin API."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

from fastapi import Header, HTTPException, Request
from psycopg.rows import dict_row

from auth_util import JWT_SECRET, JWTError, jwt_decode, jwt_encode, verify_password
from meta_db_pg import db_conn


TRICLOUD_ENV = os.environ.get("TRICLOUD_ENV", "development").strip().lower()
_configured_admin_secret = os.environ.get("ADMIN_JWT_SECRET", "").strip()

if TRICLOUD_ENV == "production" and not _configured_admin_secret:
    raise RuntimeError("ADMIN_JWT_SECRET is required in production")

ADMIN_JWT_SECRET = _configured_admin_secret or JWT_SECRET
if TRICLOUD_ENV == "production" and ADMIN_JWT_SECRET in {"", "dev-only-change-me"}:
    raise RuntimeError("ADMIN_JWT_SECRET must be a non-default secret in production")
if TRICLOUD_ENV == "production" and len(ADMIN_JWT_SECRET) < 32:
    raise RuntimeError("ADMIN_JWT_SECRET must be at least 32 characters in production")
if TRICLOUD_ENV == "production" and ADMIN_JWT_SECRET == JWT_SECRET:
    raise RuntimeError("ADMIN_JWT_SECRET must be different from JWT_SECRET in production")

ADMIN_SESSION_TTL_SEC = max(300, min(3600, int(os.environ.get("ADMIN_SESSION_TTL_SEC", "900"))))
ADMIN_TOKEN_ISSUER = "tricloud-admin-api"
ADMIN_TOKEN_AUDIENCE = "tricloud-admin-web"


@dataclass(frozen=True)
class AdminPrincipal:
    user_id: str
    session_id: str
    expires_at: int
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None


def request_ip(request: Request) -> Optional[str]:
    """Use the direct peer address; trusted-proxy handling belongs at deployment."""
    if request.client is None:
        return None
    return str(request.client.host or "")[:200] or None


def request_user_agent(request: Request) -> Optional[str]:
    value = str(request.headers.get("user-agent") or "").strip()
    return value[:500] or None


def _normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def authenticate_admin(email: str, password: str) -> Optional[str]:
    """Return the user id only when credentials and the DB admin role match."""
    normalized = _normalize_email(email)
    if not normalized or not password:
        return None
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT u.user_id, u.password_hash,
                       EXISTS (
                           SELECT 1 FROM user_roles r
                           WHERE r.user_id=u.user_id AND r.role='admin'
                       ) AS is_admin,
                       COALESCE(c.suspended,FALSE) AS suspended
                FROM users u
                LEFT JOIN admin_user_controls c ON c.user_id=u.user_id
                WHERE lower(u.email)=lower(%s)
                """,
                (normalized,),
            )
            row = cur.fetchone()
            if not row:
                return None
            if not verify_password(str(password), str(row["password_hash"])):
                return None
            if not bool(row["is_admin"]) or bool(row["suspended"]):
                return None
            return str(row["user_id"])


def create_admin_session(
    *,
    user_id: str,
    ip_address: Optional[str],
    user_agent: Optional[str],
    ttl_sec: int = ADMIN_SESSION_TTL_SEC,
) -> Tuple[str, AdminPrincipal]:
    now = int(time.time())
    ttl = max(300, min(3600, int(ttl_sec)))
    expires_at = now + ttl
    session_id = str(uuid.uuid4())
    token = jwt_encode(
        {
            "sub": str(user_id),
            "sid": session_id,
            "typ": "admin",
            "iss": ADMIN_TOKEN_ISSUER,
            "aud": ADMIN_TOKEN_AUDIENCE,
        },
        ADMIN_JWT_SECRET,
        exp_seconds=ttl,
    )

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO admin_sessions(
                    session_id,admin_user_id,created_at,expires_at,last_seen_at,
                    revoked_at,ip_address,user_agent
                ) VALUES (%s,%s,%s,%s,%s,NULL,%s,%s)
                """,
                (
                    session_id,
                    str(user_id),
                    now,
                    expires_at,
                    now,
                    ip_address,
                    user_agent,
                ),
            )
        conn.commit()

    return token, AdminPrincipal(
        user_id=str(user_id),
        session_id=session_id,
        expires_at=expires_at,
        ip_address=ip_address,
        user_agent=user_agent,
    )


def decode_admin_token(token: str) -> Tuple[str, str, int]:
    try:
        token_data = jwt_decode(str(token), ADMIN_JWT_SECRET)
    except (JWTError, ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=401, detail="invalid admin session") from exc

    extra = token_data.extra
    if (
        str(extra.get("typ") or "") != "admin"
        or str(extra.get("iss") or "") != ADMIN_TOKEN_ISSUER
        or str(extra.get("aud") or "") != ADMIN_TOKEN_AUDIENCE
    ):
        raise HTTPException(status_code=401, detail="invalid admin session")
    session_id = str(extra.get("sid") or "")
    if not session_id:
        raise HTTPException(status_code=401, detail="invalid admin session")
    return str(token_data.sub), session_id, int(token_data.exp or 0)


def require_admin(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> AdminPrincipal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="admin authentication required")
    token = authorization.split(" ", 1)[1].strip()
    user_id, session_id, token_expires_at = decode_admin_token(token)
    now = int(time.time())

    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT s.admin_user_id,s.expires_at,s.revoked_at,s.last_seen_at,
                       EXISTS (
                           SELECT 1 FROM user_roles r
                           WHERE r.user_id=s.admin_user_id AND r.role='admin'
                       ) AS is_admin,
                       COALESCE(c.suspended,FALSE) AS suspended
                FROM admin_sessions s
                LEFT JOIN admin_user_controls c ON c.user_id=s.admin_user_id
                WHERE s.session_id=%s AND s.admin_user_id=%s
                """,
                (session_id, user_id),
            )
            row = cur.fetchone()
            if (
                not row
                or row["revoked_at"] is not None
                or int(row["expires_at"] or 0) <= now
                or (token_expires_at and token_expires_at <= now)
                or not bool(row["is_admin"])
                or bool(row["suspended"])
            ):
                raise HTTPException(status_code=401, detail="admin session expired or revoked")
            if now - int(row["last_seen_at"] or 0) >= 30:
                cur.execute(
                    "UPDATE admin_sessions SET last_seen_at=%s WHERE session_id=%s",
                    (now, session_id),
                )
        conn.commit()

    request.state.admin_user_id = user_id
    request.state.admin_session_id = session_id
    return AdminPrincipal(
        user_id=user_id,
        session_id=session_id,
        expires_at=min(int(row["expires_at"]), token_expires_at or int(row["expires_at"])),
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )


def verify_admin_password(user_id: str, password: str) -> bool:
    if not password:
        return False
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT password_hash FROM users WHERE user_id=%s", (str(user_id),))
            row = cur.fetchone()
            return bool(row) and verify_password(str(password), str(row["password_hash"]))


def revoke_admin_session(session_id: str, *, ts: Optional[int] = None) -> bool:
    timestamp = int(time.time() if ts is None else ts)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE admin_sessions SET revoked_at=%s
                WHERE session_id=%s AND revoked_at IS NULL
                """,
                (timestamp, str(session_id)),
            )
            changed = cur.rowcount > 0
        conn.commit()
    return changed
