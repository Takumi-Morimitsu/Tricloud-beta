# -*- coding: utf-8 -*-
"""Separate HTTP API for the Tricloud Phase 2 administration system.

Run this service on an internal interface/port.  It intentionally does not
share sessions, CORS policy, or routes with the customer-facing Control API.
"""

from __future__ import annotations

import hmac
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from psycopg.rows import dict_row

from admin_auth import (
    ADMIN_SESSION_TTL_SEC,
    AdminPrincipal,
    authenticate_admin,
    create_admin_session,
    request_ip,
    request_user_agent,
    require_admin,
    revoke_admin_session,
    verify_admin_password,
)
from admin_schema import init_phase2_admin_schema, inspect_phase2_schema
from admin_service import (
    billing_overview,
    cancel_repair,
    create_manual_repair,
    dashboard_summary,
    force_audits,
    integrity_object_detail,
    list_admin_audit_logs,
    list_integrity_objects,
    list_nodes,
    list_releases,
    list_users,
    node_detail,
    record_admin_audit,
    repair_events,
    repairs,
    request_billing_retry,
    retry_repair,
    rewards_overview,
    under_replicated_objects,
    update_earning_status,
    update_node_controls,
    update_user_controls,
    upsert_release,
)
from meta_db_pg import db_conn


IS_PRODUCTION = os.environ.get("TRICLOUD_ENV", "development").strip().lower() == "production"
ADMIN_AUTO_MIGRATE = os.environ.get("ADMIN_AUTO_MIGRATE", "0").strip() == "1"
ADMIN_CORS_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.environ.get(
        "ADMIN_CORS_ORIGINS",
        "http://localhost:5174,http://127.0.0.1:5174,"
        "http://localhost:4174,http://127.0.0.1:4174",
    ).split(",")
    if origin.strip()
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Production rollout should run migrate_phase2_admin.py explicitly first.
    if ADMIN_AUTO_MIGRATE:
        init_phase2_admin_schema()
    yield


app = FastAPI(
    title="Tricloud Administration API",
    version="2.0.0-phase2",
    lifespan=lifespan,
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ADMIN_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    supplied = str(request.headers.get("x-request-id") or "").strip()
    request.state.request_id = supplied[:200] if supplied else str(uuid.uuid4())
    try:
        response = await call_next(request)
    except Exception:
        admin_user_id = str(getattr(request.state, "admin_user_id", ""))
        if admin_user_id:
            record_admin_audit(
                admin_user_id=admin_user_id,
                action="admin_api.request",
                target_type="route",
                target_id=request.url.path[:300],
                after={"method": request.method, "status_code": 500},
                result_status="error",
                error_code="unhandled_exception",
                **_audit_context(request),
            )
        raise
    admin_user_id = str(getattr(request.state, "admin_user_id", ""))
    if admin_user_id:
        record_admin_audit(
            admin_user_id=admin_user_id,
            action="admin_api.request",
            target_type="route",
            target_id=request.url.path[:300],
            after={"method": request.method, "status_code": response.status_code},
            result_status="success" if response.status_code < 400 else "denied",
            error_code=None if response.status_code < 400 else f"http_{response.status_code}",
            **_audit_context(request),
        )
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["Cache-Control"] = "no-store"
    return response


class LoginIn(BaseModel):
    # Admin accounts are looked up by the value stored in users.email, but the
    # administration login intentionally does not impose email-address syntax.
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1000)


class ReauthenticatedIn(BaseModel):
    admin_password: str = Field(min_length=1, max_length=1000)
    confirmation: str = Field(min_length=1, max_length=200)


class ForceAuditIn(ReauthenticatedIn):
    limit: int = Field(default=100, ge=1, le=500)


class CreateRepairIn(ReauthenticatedIn):
    file_object_id: str = Field(min_length=1, max_length=300)
    reason: str = Field(default="operator_requested", max_length=1000)


class RepairActionIn(ReauthenticatedIn):
    reason: str = Field(default="operator_requested", max_length=1000)
    reset_attempts: bool = False


class NodeControlsIn(ReauthenticatedIn):
    placement_paused: bool
    payouts_paused: bool
    reason: str = Field(default="", max_length=1000)


class UserControlsIn(ReauthenticatedIn):
    suspended: bool
    abuse_flag: bool
    sharing_disabled: bool
    downloads_disabled: bool
    reason: str = Field(default="", max_length=1000)


class BillingRetryIn(ReauthenticatedIn):
    reason: str = Field(default="operator_requested", max_length=1000)


class EarningStatusIn(ReauthenticatedIn):
    status: str = Field(min_length=1, max_length=30)
    note: str = Field(default="", max_length=2000)


class ReleaseUpsertIn(ReauthenticatedIn):
    channel: str = Field(default="stable", min_length=1, max_length=50)
    status: str = Field(default="draft", min_length=1, max_length=30)
    minimum_supported: bool = False
    force_update: bool = False
    rollout_percent: int = Field(default=0, ge=0, le=100)
    release_notes: str = Field(default="", max_length=10000)


def _audit_context(request: Request) -> Dict[str, Optional[str]]:
    return {
        "ip_address": request_ip(request),
        "user_agent": request_user_agent(request),
        "request_id": str(getattr(request.state, "request_id", ""))[:200] or None,
    }


def _reauthenticate(
    principal: AdminPrincipal,
    payload: ReauthenticatedIn,
    *,
    expected_confirmation: str,
) -> None:
    if not hmac.compare_digest(str(payload.confirmation), str(expected_confirmation)):
        raise HTTPException(
            status_code=400,
            detail=f"confirmation must exactly match: {expected_confirmation}",
        )
    if not verify_admin_password(principal.user_id, payload.admin_password):
        raise HTTPException(status_code=403, detail="admin password confirmation failed")


def _service_error(exc: Exception) -> None:
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@app.get("/health/live")
def health_live() -> Dict[str, Any]:
    return {"status": "ok", "service": "tricloud-admin-api"}


@app.get("/health/ready")
def health_ready(response: Response) -> Dict[str, Any]:
    try:
        with db_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT 1 AS ok")
                cur.fetchone()
                schema = inspect_phase2_schema(cur)
    except Exception as exc:
        response.status_code = 503
        return {"status": "not_ready", "database": False, "error": type(exc).__name__}
    if not bool(schema.get("ok")):
        response.status_code = 503
        return {"status": "not_ready", "database": True, "schema": schema}
    return {"status": "ok", "database": True, "schema": schema}


@app.post("/admin/v1/session")
def login(payload: LoginIn, request: Request) -> Dict[str, Any]:
    audit_context = _audit_context(request)
    try:
        user_id = authenticate_admin(payload.email, payload.password)
    except Exception:
        # A schema/DB failure must not be disguised as an invalid password.
        raise
    if not user_id:
        record_admin_audit(
            admin_user_id="unauthenticated",
            action="admin.login.failed",
            target_type="email",
            target_id=payload.email.strip().lower()[:320],
            result_status="denied",
            error_code="invalid_credentials_or_role",
            **audit_context,
        )
        raise HTTPException(status_code=401, detail="invalid credentials or admin role")
    token, principal = create_admin_session(
        user_id=user_id,
        ip_address=audit_context["ip_address"],
        user_agent=audit_context["user_agent"],
    )
    record_admin_audit(
        admin_user_id=user_id,
        action="admin.login.success",
        target_type="admin_session",
        target_id=principal.session_id,
        after={"expires_at": principal.expires_at},
        **audit_context,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": principal.expires_at,
        "expires_in": ADMIN_SESSION_TTL_SEC,
        "admin_user_id": principal.user_id,
    }


@app.get("/admin/v1/session")
def session(principal: AdminPrincipal = Depends(require_admin)) -> Dict[str, Any]:
    return {
        "admin_user_id": principal.user_id,
        "session_id": principal.session_id,
        "expires_at": principal.expires_at,
    }


@app.delete("/admin/v1/session")
def logout(
    request: Request,
    principal: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    revoked = revoke_admin_session(principal.session_id)
    record_admin_audit(
        admin_user_id=principal.user_id,
        action="admin.logout",
        target_type="admin_session",
        target_id=principal.session_id,
        after={"revoked": revoked},
        **_audit_context(request),
    )
    return {"status": "ok", "revoked": revoked}


@app.get("/admin/v1/dashboard")
def dashboard(_: AdminPrincipal = Depends(require_admin)) -> Dict[str, Any]:
    return dashboard_summary()


@app.get("/admin/v1/integrity/objects")
def integrity_objects(
    q: str = Query(default="", max_length=300),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    return {"items": list_integrity_objects(query=q, limit=limit, offset=offset)}


@app.get("/admin/v1/integrity/objects/under-replicated")
def integrity_shortages(
    limit: int = Query(default=200, ge=1, le=200),
    _: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    return {"items": under_replicated_objects(limit=limit)}


@app.get("/admin/v1/integrity/objects/{file_object_id}")
def integrity_detail(
    file_object_id: str,
    _: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    result = integrity_object_detail(file_object_id)
    if result is None:
        raise HTTPException(status_code=404, detail="object not found")
    return result


@app.post("/admin/v1/integrity/objects/{file_object_id}/audits")
def audit_object(
    file_object_id: str,
    payload: ForceAuditIn,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    _reauthenticate(principal, payload, expected_confirmation="QUEUE AUDIT")
    try:
        created = force_audits(
            admin_user_id=principal.user_id,
            file_object_id=file_object_id,
            limit=payload.limit,
            audit_context=_audit_context(request),
        )
    except Exception as exc:
        _service_error(exc)
        raise AssertionError("unreachable")
    return {"status": "ok", "created_audit_job_ids": created}


@app.post("/admin/v1/nodes/{node_id}/audits")
def audit_node(
    node_id: str,
    payload: ForceAuditIn,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    _reauthenticate(principal, payload, expected_confirmation="QUEUE AUDIT")
    try:
        created = force_audits(
            admin_user_id=principal.user_id,
            node_id=node_id,
            limit=payload.limit,
            audit_context=_audit_context(request),
        )
    except Exception as exc:
        _service_error(exc)
        raise AssertionError("unreachable")
    return {"status": "ok", "created_audit_job_ids": created}


@app.get("/admin/v1/repairs")
def list_repairs(
    status: Optional[str] = Query(default=None, max_length=50),
    limit: int = Query(default=100, ge=1, le=1000),
    _: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    return {"items": repairs(status=status, limit=limit)}


@app.get("/admin/v1/repairs/{repair_job_id}/events")
def list_repair_events(
    repair_job_id: str,
    _: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    return {"items": repair_events(repair_job_id)}


@app.post("/admin/v1/repairs")
def create_repair(
    payload: CreateRepairIn,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    _reauthenticate(principal, payload, expected_confirmation="CREATE REPAIR")
    try:
        repair_job_id = create_manual_repair(
            admin_user_id=principal.user_id,
            file_object_id=payload.file_object_id,
            reason=payload.reason,
            audit_context=_audit_context(request),
        )
    except Exception as exc:
        _service_error(exc)
    return {"status": "ok", "repair_job_id": repair_job_id}


@app.post("/admin/v1/repairs/{repair_job_id}/cancel")
def cancel_repair_endpoint(
    repair_job_id: str,
    payload: RepairActionIn,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    _reauthenticate(principal, payload, expected_confirmation="CANCEL REPAIR")
    try:
        return cancel_repair(
            admin_user_id=principal.user_id,
            repair_job_id=repair_job_id,
            reason=payload.reason,
            audit_context=_audit_context(request),
        )
    except Exception as exc:
        _service_error(exc)
    raise AssertionError("unreachable")


@app.post("/admin/v1/repairs/{repair_job_id}/retry")
def retry_repair_endpoint(
    repair_job_id: str,
    payload: RepairActionIn,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    _reauthenticate(principal, payload, expected_confirmation="RETRY REPAIR")
    try:
        return retry_repair(
            admin_user_id=principal.user_id,
            repair_job_id=repair_job_id,
            reason=payload.reason,
            reset_attempts=payload.reset_attempts,
            audit_context=_audit_context(request),
        )
    except Exception as exc:
        _service_error(exc)
    raise AssertionError("unreachable")


@app.get("/admin/v1/nodes")
def nodes(
    q: str = Query(default="", max_length=300),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    return {"items": list_nodes(query=q, limit=limit, offset=offset)}


@app.get("/admin/v1/nodes/{node_id}")
def get_node(node_id: str, _: AdminPrincipal = Depends(require_admin)) -> Dict[str, Any]:
    result = node_detail(node_id)
    if result is None:
        raise HTTPException(status_code=404, detail="node not found")
    return result


@app.patch("/admin/v1/nodes/{node_id}/controls")
def patch_node_controls(
    node_id: str,
    payload: NodeControlsIn,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    _reauthenticate(principal, payload, expected_confirmation="APPLY NODE CONTROLS")
    try:
        return update_node_controls(
            admin_user_id=principal.user_id,
            node_id=node_id,
            placement_paused=payload.placement_paused,
            payouts_paused=payload.payouts_paused,
            reason=payload.reason,
            audit_context=_audit_context(request),
        )
    except Exception as exc:
        _service_error(exc)
    raise AssertionError("unreachable")


@app.get("/admin/v1/users")
def users(
    q: str = Query(default="", max_length=300),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    return {"items": list_users(query=q, limit=limit, offset=offset)}


@app.get("/admin/v1/users/{user_id}")
def get_user(user_id: str, _: AdminPrincipal = Depends(require_admin)) -> Dict[str, Any]:
    result = user_detail(user_id)
    if result is None:
        raise HTTPException(status_code=404, detail="user not found")
    return result


@app.patch("/admin/v1/users/{user_id}/controls")
def patch_user_controls(
    user_id: str,
    payload: UserControlsIn,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    _reauthenticate(principal, payload, expected_confirmation="APPLY USER CONTROLS")
    try:
        return update_user_controls(
            admin_user_id=principal.user_id,
            user_id=user_id,
            suspended=payload.suspended,
            abuse_flag=payload.abuse_flag,
            sharing_disabled=payload.sharing_disabled,
            downloads_disabled=payload.downloads_disabled,
            reason=payload.reason,
            audit_context=_audit_context(request),
        )
    except Exception as exc:
        _service_error(exc)
    raise AssertionError("unreachable")


@app.get("/admin/v1/billing")
def billing(
    limit: int = Query(default=100, ge=1, le=200),
    _: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    return billing_overview(limit=limit)


@app.post("/admin/v1/billing/webhooks/{event_id}/retry-requests")
def billing_retry(
    event_id: str,
    payload: BillingRetryIn,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    _reauthenticate(principal, payload, expected_confirmation="REQUEST BILLING RETRY")
    try:
        return request_billing_retry(
            admin_user_id=principal.user_id,
            event_id=event_id,
            reason=payload.reason,
            audit_context=_audit_context(request),
        )
    except Exception as exc:
        _service_error(exc)
    raise AssertionError("unreachable")


@app.get("/admin/v1/rewards")
def rewards(
    limit: int = Query(default=100, ge=1, le=200),
    _: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    return rewards_overview(limit=limit)


@app.patch("/admin/v1/rewards/earnings/{earning_id}")
def patch_earning(
    earning_id: str,
    payload: EarningStatusIn,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    _reauthenticate(principal, payload, expected_confirmation="UPDATE EARNING")
    try:
        return update_earning_status(
            admin_user_id=principal.user_id,
            earning_id=earning_id,
            status=payload.status,
            note=payload.note,
            audit_context=_audit_context(request),
        )
    except Exception as exc:
        _service_error(exc)
    raise AssertionError("unreachable")


@app.get("/admin/v1/releases")
def releases_endpoint(_: AdminPrincipal = Depends(require_admin)) -> Dict[str, Any]:
    return {"items": list_releases()}


@app.put("/admin/v1/releases/{version}")
def put_release(
    version: str,
    payload: ReleaseUpsertIn,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    _reauthenticate(principal, payload, expected_confirmation="SAVE RELEASE")
    try:
        return upsert_release(
            admin_user_id=principal.user_id,
            version=version,
            channel=payload.channel,
            status=payload.status,
            minimum_supported=payload.minimum_supported,
            force_update=payload.force_update,
            rollout_percent=payload.rollout_percent,
            release_notes=payload.release_notes,
            audit_context=_audit_context(request),
        )
    except Exception as exc:
        _service_error(exc)
    raise AssertionError("unreachable")


@app.get("/admin/v1/audit-logs")
def audit_logs(
    q: str = Query(default="", max_length=300),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    _: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    return {"items": list_admin_audit_logs(query=q, limit=limit, offset=offset)}
