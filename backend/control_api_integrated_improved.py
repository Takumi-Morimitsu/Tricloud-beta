
# -*- coding: utf-8 -*-
"""
control_api_integrated_improved.py

統合元:
- UIコード.py
- 決済コード.py

改善方針:
- FastAPI の起動処理を on_event("startup") から lifespan へ変更
- Stripe 連携を同一ファイル内で整理し、重複 import / 重複ヘッダを除去
- 既存の主要パス（/auth, /keys, /share, /items, /node, /billing/...）を維持
- Key Wrapping import を crypto_common_keywrap.py に統一
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional
import re

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from psycopg.rows import dict_row

# DB・使用量関連
from meta_db_pg import db_conn, ensure_default_plan, init_schema, now_ts
from usage_metering import (
    get_daily_egress_limit_bytes,
    get_daily_egress_used_bytes,
)

# 認証関連
from auth_util import (
    JWT_SECRET,
    hash_password,
    jwt_decode,
    jwt_encode,
    verify_password,
)

# 報酬・Stripe用スキーマ
from database_extensions import (
    init_auth_profile_schema,
    init_multipart_and_quota_schema,
    init_rewards_and_stripe_schema,
)

# Key Wrapping 共通ユーティリティ
from crypto_common_keywrap import b64d, b64e

# 追加パッチルーター / 初期化処理
from items_phase2_patch import init_phase2_items_schema, router as phase2_items_router
from node_heartbeat_stats_patch import init_node_heartbeat_stats_schema
from node_provider_v2 import init_phase1_node_provider_schema, router as node_provider_router
from object_gc import init_object_gc_schema
from replica_health_service import init_storage_maintenance_schema
from storage_audit_service import init_storage_audit_schema
from replica_repair_service import init_replica_repair_schema
from phase3_ops_patch import router as phase3_ops_router
from phase4_copy_patch_v2 import router as phase4_copy_router
from phase5_library_patch import (
    init_phase5_library_schema,
    router as phase5_library_router,
)
from ui_upload_bridge_v2 import router as ui_upload_router
from ui_download_bridge_v2 import router as ui_download_router
from backup_targets_patch import init_backup_targets_schema, router as backup_targets_router
from admin_controls import (
    AdminControlsUnavailable,
    restriction_for_request,
)

try:
    import stripe
except Exception:  # pragma: no cover
    stripe = None


# ==========================================
# 環境変数 / 定数
# ==========================================
APP_TITLE = "Control API (PostgreSQL) - Integrated + Stripe"
IS_PRODUCTION = os.getenv("TRICLOUD_ENV", "").strip().lower() == "production"
ROOT_ID = "root"
COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2}$")
TERMS_VERSION = os.environ.get("TERMS_VERSION", "2026-04")
PRIVACY_POLICY_VERSION = os.environ.get("PRIVACY_POLICY_VERSION", "2026-04")

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")

# 開発用CORS設定。Vite dev server から FastAPI へ fetch する際、
# ブラウザは POST の前に OPTIONS preflight を送るため、明示的に許可する。
CORS_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.environ.get(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173",
    ).split(",")
    if origin.strip()
]

if stripe is not None and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


# ==========================================
# 起動時初期化
# ==========================================
def init_keywrap_schema() -> None:
    """Key Wrapping 用の最低限のテーブルを作成する。"""
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS user_master_keys (
            user_id TEXT PRIMARY KEY,
            kdf TEXT NOT NULL,
            salt BYTEA NOT NULL,
            params_json TEXT NOT NULL,
            wrapped_master_key BYTEA NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS file_wrapped_keys (
            file_object_id TEXT PRIMARY KEY,
            owner_user_id TEXT NOT NULL,
            wrapped_dek BYTEA NOT NULL,
            created_at INTEGER NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_file_wrapped_keys_owner
        ON file_wrapped_keys(owner_user_id, created_at)
        """,
    ]
    with db_conn() as conn:
        with conn.cursor() as cur:
            for stmt in ddl:
                cur.execute(stmt)
        conn.commit()


def init_node_heartbeat_hourly_schema() -> None:
    """node_heartbeat_hourly を作成する。

    node_heartbeat_stats_patch はカーソルを受け取る薄い関数なので、
    メインAPIの lifespan から呼びやすいように no-arg wrapper を用意する。
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            init_node_heartbeat_stats_schema(cur)
        conn.commit()


def initialize_patch_schemas() -> None:
    """追加パッチ群のDDL初期化を FastAPI lifespan に集約する。

    FastAPI に lifespan を渡すと router の startup event には依存できないため、
    各パッチの init_* 関数をここから明示的に呼ぶ。
    """
    # multipart / item_parts は版履歴・UIアップロード・コピー機能の前提になる。
    init_multipart_and_quota_schema()

    # ストレージ提供ノード画面 / heartbeat 集計。
    init_phase1_node_provider_schema()
    init_node_heartbeat_hourly_schema()

    # ファイル一覧・ごみ箱・版履歴・同期クライアント。
    init_phase2_items_schema()

    # 完全削除後のオブジェクトGCキュー。
    init_object_gc_schema()

    # Phase 1 data-integrity tables.  Schema initialization never starts I/O;
    # audit/repair execution remains controlled by explicit feature flags.
    init_storage_maintenance_schema()
    init_storage_audit_schema()
    init_replica_repair_schema()

    # Home / Shared / Recent / メール指定共有。
    init_phase5_library_schema()
    init_backup_targets_schema()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """FastAPI 推奨の lifespan で初期化を行う。"""
    init_schema()
    ensure_default_plan()
    init_keywrap_schema()
    init_rewards_and_stripe_schema()
    init_auth_profile_schema()
    initialize_patch_schemas()
    yield


app = FastAPI(title=APP_TITLE, lifespan=lifespan, docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
    openapi_url=None if IS_PRODUCTION else "/openapi.json",)

@app.middleware("http")
async def enforce_phase2_user_controls(request: Request, call_next):
    """Apply DB-backed account/share/download restrictions to bearer requests."""
    authorization = str(request.headers.get("authorization") or "")
    if authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        try:
            user_id = jwt_decode(token, JWT_SECRET).sub
        except Exception:
            # Existing route dependencies return the authoritative auth error.
            user_id = ""
        if user_id:
            try:
                restriction = restriction_for_request(user_id, request.url.path)
            except AdminControlsUnavailable:
                return JSONResponse(
                    status_code=503,
                    content={"detail": "account controls are temporarily unavailable"},
                )
            if restriction:
                return JSONResponse(
                    status_code=403,
                    content={"detail": restriction},
                )
    return await call_next(request)


# Keep CORS outermost so middleware-generated 403/503 responses also receive
# the appropriate browser headers for explicitly allowed application origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 追加パッチのルートをメインAPIへ登録する。
# phase5_library_patch.py はメール共有機能付き統合版を正式採用する。
app.include_router(phase2_items_router)
app.include_router(phase3_ops_router)
app.include_router(phase4_copy_router)
app.include_router(phase5_library_router)
app.include_router(node_provider_router)
app.include_router(ui_upload_router)
app.include_router(ui_download_router)
app.include_router(backup_targets_router)


# ==========================================
# 共通ヘルパー
# ==========================================
def _json_dumps(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _clamp_limit(limit: int, min_value: int = 1, max_value: int = 200) -> int:
    return max(min_value, min(int(limit), max_value))


def _absolute_app_url(path: str) -> str:
    """
    APP_BASE_URL と結合する。
    path が絶対URLならそのまま返す。
    """
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return APP_BASE_URL + path


def _normalize_email(email: str) -> str:
    normalized = str(email or "").strip().lower()
    if not normalized:
        raise HTTPException(status_code=400, detail="email is required")
    return normalized


def _normalize_name(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    if len(normalized) > 100:
        raise HTTPException(status_code=400, detail=f"{field_name} is too long")
    return normalized


def _normalize_country_code(country_code: str) -> str:
    normalized = str(country_code or "").strip().upper()
    if not COUNTRY_CODE_RE.match(normalized):
        raise HTTPException(status_code=400, detail="country_code must be an ISO 3166-1 alpha-2 code")
    return normalized


def _validate_signup_password(password: str) -> str:
    value = str(password or "")
    if len(value) < 8:
        raise HTTPException(status_code=400, detail="password must be at least 8 characters")
    if len(value) > 256:
        raise HTTPException(status_code=400, detail="password is too long")
    return value


def _validate_terms_acceptance(accepted_terms: bool, accepted_privacy_policy: bool) -> None:
    if not bool(accepted_terms) or not bool(accepted_privacy_policy):
        raise HTTPException(status_code=400, detail="terms and privacy policy acceptance are required")


def bearer_token(authorization: Optional[str] = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return authorization.split(" ", 1)[1].strip()


def current_user_id(token: str = Depends(bearer_token)) -> str:
    td = jwt_decode(token, JWT_SECRET)
    return td.sub


def _require_stripe() -> None:
    if stripe is None:
        raise HTTPException(status_code=500, detail="stripe package not installed")
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="STRIPE_SECRET_KEY is not set")


def _ensure_role_value(role: str) -> str:
    role = str(role).strip().lower()
    if role not in {"viewer", "editor"}:
        raise HTTPException(status_code=400, detail="role must be 'viewer' or 'editor'")
    return role


def _get_or_create_stripe_customer(user_id: str, email: Optional[str]) -> str:
    """ユーザーに対応する Stripe Customer を取得し、なければ作成する。"""
    _require_stripe()
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT stripe_customer_id FROM stripe_customers WHERE user_id=%s",
                (user_id,),
            )
            row = cur.fetchone()
            if row and row["stripe_customer_id"]:
                return str(row["stripe_customer_id"])

            customer = stripe.Customer.create(
                email=email,
                metadata={"user_id": user_id},
            )
            cur.execute(
                """
                INSERT INTO stripe_customers(user_id, stripe_customer_id, created_at)
                VALUES (%s,%s,%s)
                ON CONFLICT (user_id) DO UPDATE SET
                  stripe_customer_id=EXCLUDED.stripe_customer_id
                """,
                (user_id, customer.id, int(now_ts())),
            )
        conn.commit()
    return str(customer.id)


def _get_or_create_connected_account(node_id: str, owner_user_id: str) -> str:
    """ノードに紐づく Express connected account を取得し、なければ作成する。"""
    _require_stripe()
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT stripe_connected_account_id
                FROM node_profiles
                WHERE node_id=%s AND owner_user_id=%s
                """,
                (node_id, owner_user_id),
            )
            row = cur.fetchone()
            if row and row["stripe_connected_account_id"]:
                return str(row["stripe_connected_account_id"])

            acct = stripe.Account.create(
                type="express",
                metadata={"user_id": owner_user_id, "node_id": node_id},
            )
            cur.execute(
                """
                UPDATE node_profiles
                SET stripe_connected_account_id=%s
                WHERE node_id=%s AND owner_user_id=%s
                """,
                (acct.id, node_id, owner_user_id),
            )
        conn.commit()
    return str(acct.id)


def _record_webhook_event(
    *,
    event_id: Optional[str],
    event_type: str,
    status: str,
    detail: Optional[str] = None,
) -> None:
    """Webhook の処理結果を記録する。event_id が無い場合は記録しない。"""
    if not event_id:
        return
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO stripe_webhook_events(event_id,event_type,processed_at,status,detail)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (event_id, event_type, int(now_ts()), status, detail),
            )
        conn.commit()


def _cap_exceeded_or_raise(charge_user_id: str, *, is_shared: bool) -> None:
    """
    枠超過時の扱い。
    - 通常DL: JST日次
    - 共有DL: 直近24hローリング
    """
    lim = get_daily_egress_limit_bytes(charge_user_id, is_shared=is_shared)
    if lim is None:
        return

    if is_shared:
        from usage_metering import get_rolling_egress_used_bytes

        used = get_rolling_egress_used_bytes(
            charge_user_id,
            window_seconds=86400,
            is_shared=True,
        )
    else:
        used = get_daily_egress_used_bytes(charge_user_id, is_shared=False)

    if used >= lim:
        raise HTTPException(
            status_code=429,
            detail="download quota exceeded, please try again later",
        )


def _ensure_object_owner_or_raise(file_object_id: str, uid: str) -> str:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT owner_user_id FROM objects WHERE file_object_id=%s",
                (file_object_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="object not found")
            owner = str(row["owner_user_id"])
            if owner != uid:
                raise HTTPException(status_code=403, detail="forbidden")
            return owner


# ==========================================
# リクエスト / レスポンスモデル
# ==========================================
class SignupIn(BaseModel):
    last_name: str
    first_name: str
    email: str
    password: str
    country_code: str
    accepted_terms: bool
    accepted_privacy_policy: bool


class LoginIn(BaseModel):
    email: str
    password: str


class MasterKeyUpsertIn(BaseModel):
    wrapped_master_key_b64: str
    salt_b64: str
    kdf: str
    params_json: str


class MasterKeyOut(BaseModel):
    user_id: str
    wrapped_master_key_b64: str
    salt_b64: str
    kdf: str
    params_json: str
    created_at: int
    updated_at: Optional[int] = None


class WrappedDEKIn(BaseModel):
    wrapped_dek_b64: str


class WrappedDEKOut(BaseModel):
    file_object_id: str
    wrapped_dek_b64: str
    created_at: int


class CreateShareIn(BaseModel):
    item_id: str
    role: str = Field(description="viewer または editor")
    expires_in_sec: Optional[int] = None


class DownloadTokenOut(BaseModel):
    download_token: str
    expires_at: int
    file_object_id: str
    charge_user_id: str
    is_shared: bool


class CheckoutIn(BaseModel):
    plan_id: str
    success_path: str = "/billing/success"
    cancel_path: str = "/billing/cancel"


class ConnectOnboardingIn(BaseModel):
    return_path: str = "/billing/stripe/connect/return"
    refresh_path: str = "/billing/stripe/connect/refresh"


class PayoutIn(BaseModel):
    node_id: str
    earning_id: str


class UserProfileOut(BaseModel):
    user_id: str
    email: str
    last_name: Optional[str] = None
    first_name: Optional[str] = None
    country_code: Optional[str] = None
    terms_version: Optional[str] = None
    privacy_policy_version: Optional[str] = None
    terms_accepted_at: Optional[int] = None
    privacy_policy_accepted_at: Optional[int] = None
    created_at: Optional[int] = None


class UserProfileUpdateIn(BaseModel):
    country_code: str


# ==========================================
# 認証 API
# ==========================================
@app.post("/auth/signup")
def signup(inp: SignupIn) -> Dict[str, Any]:
    uid = str(uuid.uuid4())
    created = int(now_ts())
    email = _normalize_email(inp.email)
    password = _validate_signup_password(inp.password)
    last_name = _normalize_name(inp.last_name, "last_name")
    first_name = _normalize_name(inp.first_name, "first_name")
    country_code = _normalize_country_code(inp.country_code)
    _validate_terms_acceptance(inp.accepted_terms, inp.accepted_privacy_policy)
    password_hash = hash_password(password)

    with db_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO users(
                        user_id,email,password_hash,last_name,first_name,country_code,
                        terms_version,privacy_policy_version,terms_accepted_at,privacy_policy_accepted_at,created_at
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        uid,
                        email,
                        password_hash,
                        last_name,
                        first_name,
                        country_code,
                        TERMS_VERSION,
                        PRIVACY_POLICY_VERSION,
                        created,
                        created,
                        created,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO user_roles(user_id,role,created_at)
                    VALUES (%s,%s,%s)
                    """,
                    (uid, "client", created),
                )
            except Exception as exc:
                conn.rollback()
                raise HTTPException(status_code=400, detail=f"signup failed: {exc}")
        conn.commit()

    access_token = jwt_encode({"sub": uid}, JWT_SECRET, exp_seconds=7 * 24 * 3600)
    return {"user_id": uid, "access_token": access_token}


@app.post("/auth/login")
def login(inp: LoginIn) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT user_id,password_hash FROM users WHERE email=%s",
                (_normalize_email(inp.email),),
            )
            row = cur.fetchone()
            if not row or not verify_password(inp.password, row["password_hash"]):
                raise HTTPException(status_code=401, detail="invalid credentials")
            uid = str(row["user_id"])

    try:
        login_restriction = restriction_for_request(uid, "/auth/login")
    except AdminControlsUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="account controls are temporarily unavailable",
        ) from exc
    if login_restriction:
        raise HTTPException(status_code=403, detail=login_restriction)

    access_token = jwt_encode({"sub": uid}, JWT_SECRET, exp_seconds=7 * 24 * 3600)
    return {"user_id": uid, "access_token": access_token}

def _fetch_user_profile_or_raise(uid: str) -> UserProfileOut:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT user_id, email, last_name, first_name, country_code,
                       terms_version, privacy_policy_version,
                       terms_accepted_at, privacy_policy_accepted_at, created_at
                FROM users
                WHERE user_id=%s
                """,
                (uid,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="user not found")
            return UserProfileOut(**dict(row))


@app.get("/auth/profile", response_model=UserProfileOut)
def auth_profile(uid: str = Depends(current_user_id)) -> UserProfileOut:
    return _fetch_user_profile_or_raise(uid)


@app.patch("/auth/profile", response_model=UserProfileOut)
def update_auth_profile(inp: UserProfileUpdateIn, uid: str = Depends(current_user_id)) -> UserProfileOut:
    country_code = _normalize_country_code(inp.country_code)
    updated_at = int(now_ts())
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET country_code=%s
                WHERE user_id=%s
                """,
                (country_code, uid),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="user not found")
        conn.commit()
    return _fetch_user_profile_or_raise(uid)


# ==========================================
# Key Wrapping API
# ==========================================
@app.get("/keys/master", response_model=MasterKeyOut)
def get_master_key(uid: str = Depends(current_user_id)) -> MasterKeyOut:
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM user_master_keys WHERE user_id=%s", (uid,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="master key not found")
            return MasterKeyOut(
                user_id=uid,
                wrapped_master_key_b64=b64e(bytes(row["wrapped_master_key"])),
                salt_b64=b64e(bytes(row["salt"])),
                kdf=str(row["kdf"]),
                params_json=str(row["params_json"]),
                created_at=int(row["created_at"]),
                updated_at=int(row["updated_at"]) if row["updated_at"] is not None else None,
            )


@app.post("/keys/master", response_model=MasterKeyOut)
def create_master_key(
    inp: MasterKeyUpsertIn,
    uid: str = Depends(current_user_id),
) -> MasterKeyOut:
    created = int(now_ts())
    wrapped = b64d(inp.wrapped_master_key_b64)
    salt = b64d(inp.salt_b64)

    if len(salt) < 8 or len(wrapped) < 16:
        raise HTTPException(status_code=400, detail="invalid key blob")

    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT 1 FROM user_master_keys WHERE user_id=%s", (uid,))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="master key already exists")
            cur.execute(
                """
                INSERT INTO user_master_keys(
                    user_id,kdf,salt,params_json,wrapped_master_key,created_at,updated_at
                )
                VALUES (%s,%s,%s,%s,%s,%s,NULL)
                """,
                (uid, inp.kdf, salt, inp.params_json, wrapped, created),
            )
        conn.commit()

    return MasterKeyOut(
        user_id=uid,
        wrapped_master_key_b64=inp.wrapped_master_key_b64,
        salt_b64=inp.salt_b64,
        kdf=inp.kdf,
        params_json=inp.params_json,
        created_at=created,
        updated_at=None,
    )


@app.get("/keys/file/{file_object_id}", response_model=WrappedDEKOut)
def get_wrapped_dek(
    file_object_id: str,
    uid: str = Depends(current_user_id),
) -> WrappedDEKOut:
    _ensure_object_owner_or_raise(file_object_id, uid)
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT wrapped_dek, created_at
                FROM file_wrapped_keys
                WHERE file_object_id=%s
                """,
                (file_object_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="wrapped DEK not found")
            return WrappedDEKOut(
                file_object_id=file_object_id,
                wrapped_dek_b64=b64e(bytes(row["wrapped_dek"])),
                created_at=int(row["created_at"]),
            )


@app.post("/keys/file/{file_object_id}", response_model=WrappedDEKOut)
def put_wrapped_dek(
    file_object_id: str,
    inp: WrappedDEKIn,
    uid: str = Depends(current_user_id),
) -> WrappedDEKOut:
    owner = _ensure_object_owner_or_raise(file_object_id, uid)
    created = int(now_ts())
    wrapped = b64d(inp.wrapped_dek_b64)

    if len(wrapped) < 16:
        raise HTTPException(status_code=400, detail="invalid wrapped_dek")

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO file_wrapped_keys(file_object_id,owner_user_id,wrapped_dek,created_at)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (file_object_id) DO UPDATE SET
                  wrapped_dek=EXCLUDED.wrapped_dek,
                  created_at=EXCLUDED.created_at
                """,
                (file_object_id, owner, wrapped, created),
            )
        conn.commit()

    return WrappedDEKOut(
        file_object_id=file_object_id,
        wrapped_dek_b64=inp.wrapped_dek_b64,
        created_at=created,
    )


# ==========================================
# ファイル共有 API
# ==========================================
@app.post("/share/create")
def create_share(
    inp: CreateShareIn,
    uid: str = Depends(current_user_id),
) -> Dict[str, Any]:
    share_id = uuid.uuid4().hex
    created = int(now_ts())
    expires = created + int(inp.expires_in_sec) if inp.expires_in_sec else None
    role = _ensure_role_value(inp.role)

    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT owner_user_id
                FROM items
                WHERE item_id=%s AND trashed_at IS NULL
                """,
                (inp.item_id,),
            )
            row = cur.fetchone()
            if not row or str(row["owner_user_id"]) != uid:
                raise HTTPException(status_code=404, detail="item not found")

            cur.execute(
                """
                INSERT INTO shares(share_id,item_id,owner_user_id,role,expires_at,revoked_at,created_at)
                VALUES (%s,%s,%s,%s,%s,NULL,%s)
                """,
                (share_id, inp.item_id, uid, role, expires, created),
            )
        conn.commit()

    return {"share_id": share_id, "expires_at": expires}


# ==========================================
# ダウンロード制御 API
# ==========================================
@app.post("/items/{item_id}/download_token", response_model=DownloadTokenOut)
def download_token(
    item_id: str,
    uid: str = Depends(current_user_id),
) -> DownloadTokenOut:
    created = int(now_ts())
    expires_at = created + 600
    token = uuid.uuid4().hex

    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT file_object_id, owner_user_id
                FROM items
                WHERE item_id=%s AND trashed_at IS NULL
                """,
                (item_id,),
            )
            item = cur.fetchone()
            if not item:
                raise HTTPException(status_code=404, detail="item not found")
            if str(item["owner_user_id"]) != uid:
                raise HTTPException(status_code=403, detail="forbidden")

            _cap_exceeded_or_raise(uid, is_shared=False)

            cur.execute(
                """
                INSERT INTO download_tokens(
                    token,file_object_id,owner_user_id,charge_user_id,is_shared,expires_at,created_at
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    token,
                    item["file_object_id"],
                    item["owner_user_id"],
                    uid,
                    False,
                    expires_at,
                    created,
                ),
            )
        conn.commit()

    return DownloadTokenOut(
        download_token=token,
        expires_at=expires_at,
        file_object_id=str(item["file_object_id"]),
        charge_user_id=uid,
        is_shared=False,
    )


@app.post("/s/{share_id}/download_token", response_model=DownloadTokenOut)
def share_download_token(share_id: str) -> DownloadTokenOut:
    created = int(now_ts())
    expires_at = created + 600
    token = uuid.uuid4().hex

    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT s.item_id,s.owner_user_id,s.expires_at,s.revoked_at,i.file_object_id
                FROM shares s
                JOIN items i ON s.item_id=i.item_id
                WHERE s.share_id=%s AND i.trashed_at IS NULL
                """,
                (share_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="share not found")
            if row["revoked_at"] is not None:
                raise HTTPException(status_code=410, detail="share revoked")
            if row["expires_at"] is not None and int(row["expires_at"]) < created:
                raise HTTPException(status_code=410, detail="share expired")

            owner = str(row["owner_user_id"])
            try:
                public_share_restriction = restriction_for_request(
                    owner,
                    "/s/shared/download_token",
                )
            except AdminControlsUnavailable as exc:
                raise HTTPException(
                    status_code=503,
                    detail="account controls are temporarily unavailable",
                ) from exc
            if public_share_restriction:
                raise HTTPException(status_code=403, detail=public_share_restriction)
            _cap_exceeded_or_raise(owner, is_shared=True)

            cur.execute(
                """
                INSERT INTO download_tokens(
                    token,file_object_id,owner_user_id,charge_user_id,is_shared,expires_at,created_at
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    token,
                    row["file_object_id"],
                    owner,
                    owner,
                    True,
                    expires_at,
                    created,
                ),
            )
        conn.commit()

    return DownloadTokenOut(
        download_token=token,
        expires_at=expires_at,
        file_object_id=str(row["file_object_id"]),
        charge_user_id=owner,
        is_shared=True,
    )


# ==========================================
# ノード報酬 API
# ==========================================
@app.get("/node/earnings")
def my_node_earnings(limit: int = 50, uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    limit = _clamp_limit(limit)
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT ne.*
                FROM node_earnings ne
                JOIN node_profiles np ON np.node_id = ne.node_id
                WHERE np.owner_user_id=%s
                ORDER BY ne.period_start DESC, ne.created_at DESC
                LIMIT %s
                """,
                (uid, limit),
            )
            items = [dict(row) for row in cur.fetchall()]
    return {"items": items}


@app.get("/node/payouts")
def my_node_payouts(limit: int = 50, uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    limit = _clamp_limit(limit)
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT np2.*
                FROM node_payouts np2
                JOIN node_profiles np ON np.node_id = np2.node_id
                WHERE np.owner_user_id=%s
                ORDER BY np2.created_at DESC
                LIMIT %s
                """,
                (uid, limit),
            )
            items = [dict(row) for row in cur.fetchall()]
    return {"items": items}


# ==========================================
# Stripe Billing / Connect API
# ==========================================
@app.post("/billing/stripe/checkout/session")
def create_checkout_session(
    inp: CheckoutIn,
    uid: str = Depends(current_user_id),
) -> Dict[str, Any]:
    _require_stripe()

    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT email FROM users WHERE user_id=%s", (uid,))
            user_row = cur.fetchone()
            if not user_row:
                raise HTTPException(status_code=404, detail="user not found")

            cur.execute(
                """
                SELECT stripe_price_id
                FROM stripe_plan_prices
                WHERE plan_id=%s AND active=TRUE
                """,
                (inp.plan_id,),
            )
            price_row = cur.fetchone()
            if not price_row:
                raise HTTPException(
                    status_code=400,
                    detail="stripe price mapping not found for plan",
                )

    customer_id = _get_or_create_stripe_customer(uid, str(user_row["email"]))
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": str(price_row["stripe_price_id"]), "quantity": 1}],
        success_url=_absolute_app_url(inp.success_path) + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=_absolute_app_url(inp.cancel_path),
        client_reference_id=uid,
        metadata={"user_id": uid, "plan_id": inp.plan_id},
    )
    return {"checkout_session_id": session.id, "url": session.url}


@app.post("/billing/stripe/webhook")
async def stripe_webhook(request: Request) -> Dict[str, Any]:
    _require_stripe()

    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")

    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(
                payload=payload,
                sig_header=signature,
                secret=STRIPE_WEBHOOK_SECRET,
            )
        else:
            # 開発用。署名なし受信を許すのはローカルのみ推奨。
            event = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid webhook: {exc}")

    event_id = event.get("id")
    event_type = str(event.get("type") or "")
    data_obj = (event.get("data") or {}).get("object") or {}

    # すでに処理済みなら成功として返す
    if event_id:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM stripe_webhook_events WHERE event_id=%s",
                    (event_id,),
                )
                if cur.fetchone():
                    return {"received": True, "duplicate": True}

    try:
        with db_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                if event_type == "checkout.session.completed":
                    uid = str(
                        (data_obj.get("metadata") or {}).get("user_id")
                        or data_obj.get("client_reference_id")
                        or ""
                    )
                    plan_id = str(
                        (data_obj.get("metadata") or {}).get("plan_id") or ""
                    )
                    customer_id = str(data_obj.get("customer") or "")
                    sub_id = str(data_obj.get("subscription") or "")

                    if uid and customer_id:
                        cur.execute(
                            """
                            INSERT INTO stripe_customers(user_id,stripe_customer_id,created_at)
                            VALUES (%s,%s,%s)
                            ON CONFLICT (user_id) DO UPDATE SET
                              stripe_customer_id=EXCLUDED.stripe_customer_id
                            """,
                            (uid, customer_id, int(now_ts())),
                        )

                    if uid and sub_id:
                        cur.execute(
                            """
                            INSERT INTO subscriptions(
                                user_id,plan_id,status,current_period_start,created_at,provider,provider_ref
                            )
                            VALUES (%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (user_id) DO UPDATE SET
                                plan_id=EXCLUDED.plan_id,
                                status=EXCLUDED.status,
                                current_period_start=EXCLUDED.current_period_start,
                                provider=EXCLUDED.provider,
                                provider_ref=EXCLUDED.provider_ref
                            """,
                            (
                                uid,
                                plan_id or "dev-standard",
                                "active",
                                int(now_ts()),
                                int(now_ts()),
                                "stripe",
                                sub_id,
                            ),
                        )

                elif event_type in {
                    "customer.subscription.created",
                    "customer.subscription.updated",
                    "customer.subscription.deleted",
                }:
                    sub_id = str(data_obj.get("id") or "")
                    status = str(data_obj.get("status") or "")
                    if sub_id:
                        cur.execute(
                            """
                            UPDATE subscriptions
                            SET status=%s
                            WHERE provider='stripe' AND provider_ref=%s
                            """,
                            (status, sub_id),
                        )

                elif event_type == "invoice.paid":
                    sub_id = str(data_obj.get("subscription") or "")
                    if sub_id:
                        cur.execute(
                            """
                            UPDATE invoices
                            SET status='paid'
                            WHERE user_id IN (
                                SELECT user_id
                                FROM subscriptions
                                WHERE provider='stripe' AND provider_ref=%s
                            )
                              AND status='open'
                            """,
                            (sub_id,),
                        )

                elif event_type == "invoice.payment_failed":
                    sub_id = str(data_obj.get("subscription") or "")
                    if sub_id:
                        cur.execute(
                            """
                            UPDATE subscriptions
                            SET status='past_due'
                            WHERE provider='stripe' AND provider_ref=%s
                            """,
                            (sub_id,),
                        )
            conn.commit()

        _record_webhook_event(
            event_id=event_id,
            event_type=event_type,
            status="ok",
            detail=None,
        )
    except Exception as exc:
        _record_webhook_event(
            event_id=event_id,
            event_type=event_type,
            status="error",
            detail=str(exc)[:1000],
        )
        raise

    return {"received": True}


@app.post("/billing/stripe/connect/onboarding_link")
def create_connect_onboarding_link(
    inp: ConnectOnboardingIn,
    uid: str = Depends(current_user_id),
) -> Dict[str, Any]:
    _require_stripe()

    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT node_id
                FROM node_profiles
                WHERE owner_user_id=%s
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (uid,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="node profile not found")
            node_id = str(row["node_id"])

    account_id = _get_or_create_connected_account(node_id=node_id, owner_user_id=uid)
    link = stripe.AccountLink.create(
        account=account_id,
        refresh_url=_absolute_app_url(inp.refresh_path),
        return_url=_absolute_app_url(inp.return_path),
        type="account_onboarding",
    )
    return {"account_id": account_id, "url": link.url}


@app.post("/billing/stripe/connect/payout")
def payout_node_earning(
    inp: PayoutIn,
    uid: str = Depends(current_user_id),
) -> Dict[str, Any]:
    _require_stripe()

    payout_id = str(uuid.uuid4())
    created = int(now_ts())

    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT owner_user_id, stripe_connected_account_id,
                       payout_enabled,COALESCE(payouts_paused,FALSE) AS payouts_paused
                FROM node_profiles
                WHERE node_id=%s
                """,
                (inp.node_id,),
            )
            profile = cur.fetchone()
            if not profile:
                raise HTTPException(status_code=404, detail="node profile not found")
            if str(profile["owner_user_id"]) != uid:
                raise HTTPException(status_code=403, detail="forbidden")
            if not bool(profile["payout_enabled"]):
                raise HTTPException(status_code=403, detail="payouts are not enabled")
            if bool(profile["payouts_paused"]):
                raise HTTPException(status_code=403, detail="payouts are paused")

            account_id = str(profile["stripe_connected_account_id"] or "")
            if not account_id:
                raise HTTPException(status_code=400, detail="connect account not linked")

            cur.execute(
                """
                SELECT net_amount_yen,status
                FROM node_earnings
                WHERE earning_id=%s AND node_id=%s
                """,
                (inp.earning_id, inp.node_id),
            )
            earning = cur.fetchone()
            if not earning:
                raise HTTPException(status_code=404, detail="earning not found")

            earning_status = str(earning["status"])
            if earning_status != "approved":
                raise HTTPException(status_code=400, detail="earning not payable")

            amount_yen = int(earning["net_amount_yen"])
            if amount_yen <= 0:
                raise HTTPException(status_code=400, detail="non-positive payout")

            transfer = stripe.Transfer.create(
                amount=amount_yen,
                currency="jpy",
                destination=account_id,
                metadata={"earning_id": inp.earning_id, "node_id": inp.node_id},
            )

            cur.execute(
                """
                INSERT INTO node_payouts(
                    payout_id,node_id,amount_yen,currency,provider,provider_ref,status,created_at
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    payout_id,
                    inp.node_id,
                    amount_yen,
                    "jpy",
                    "stripe_connect",
                    str(transfer.id),
                    "pending",
                    created,
                ),
            )
            cur.execute(
                """
                UPDATE node_earnings
                SET status='paid', updated_at=%s
                WHERE earning_id=%s
                """,
                (created, inp.earning_id),
            )
        conn.commit()

    return {
        "status": "ok",
        "provider_ref": str(transfer.id),
        "amount_yen": amount_yen,
        "payout_id": payout_id,
    }


# ==========================================
# 簡易ヘルスチェック
# ==========================================
@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}
