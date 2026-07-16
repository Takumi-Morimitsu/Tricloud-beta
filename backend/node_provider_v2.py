# -*- coding: utf-8 -*-
"""
phase1 node provider patch

目的:
- Web UI から「このPCのストレージをノードとして提供する」導線を作る
- 既存の node_profiles / node_earnings / node_payouts / nodes テーブルを使い、
  提供量、報酬サマリー、Stripe Connect 連携状況、ノード起動コマンドを返す
- 既存のアップロード / 課金 / 報酬計算コードを壊さず、薄い追加で統合する
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
import shutil
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from psycopg.rows import dict_row

from meta_db_pg import db_conn, now_ts
from auth_util import JWT_SECRET, jwt_decode

router = APIRouter(tags=["phase1-node-provider"])

ROOT_NODE_NAME = "マイノード"
DEFAULT_CAPACITY_GB = 0
ONLINE_TTL_SEC = 20
NODE_SERVER_ENDPOINT = os.environ.get("NODE_SERVER_ENDPOINT", "tcp://127.0.0.1:9999")
NODE_STORAGE_DIR_DEFAULT = os.environ.get("NODE_STORAGE_DIR_DEFAULT", "Tri_Cloud")
NODE_RUNNER_FILE = os.environ.get("NODE_RUNNER_FILE", "node_phase1_runner.py")



def _node_storage_dir_for_capacity_hint() -> str:
    """ノード保存先の容量表示に使うローカルパス。Electron側も Tri_Cloud を同名規約として扱う。"""
    raw = str(NODE_STORAGE_DIR_DEFAULT or "").strip()
    normalized = raw.replace("\\", "/").rstrip("/")
    if not raw or normalized in {"./node_store", "node_store", "Tri_Cloud"}:
        return str(Path.home() / "Tri_Cloud")
    return os.path.abspath(raw)


def bearer_token(authorization: Optional[str] = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return authorization.split(" ", 1)[1].strip()


def current_user_id(token: str = Depends(bearer_token)) -> str:
    td = jwt_decode(token, JWT_SECRET)
    return td.sub


class NodeProviderProfileIn(BaseModel):
    node_name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    desired_capacity_gb: int = Field(default=DEFAULT_CAPACITY_GB, ge=0, le=1024 * 1024)


class RotateKeyOut(BaseModel):
    node_id: str
    node_api_key: str


class NodeProviderSummaryOut(BaseModel):
    profile: Optional[Dict[str, Any]]
    runtime: Dict[str, Any]
    earnings_summary: Dict[str, Any]
    recent_earnings: List[Dict[str, Any]]
    recent_payouts: List[Dict[str, Any]]
    reward_projection: Dict[str, Any]
    stripe: Dict[str, Any]
    launch: Optional[Dict[str, Any]]
    defaults: Dict[str, Any]
    local_capacity: Optional[Dict[str, Any]] = None
    uptime_summary: Optional[Dict[str, Any]] = None


DDL = [
    """
    CREATE TABLE IF NOT EXISTS node_profiles (
        node_id TEXT PRIMARY KEY,
        owner_user_id TEXT,
        created_at INTEGER
    )
    """,
    "ALTER TABLE node_profiles ADD COLUMN IF NOT EXISTS node_name TEXT",
    "ALTER TABLE node_profiles ADD COLUMN IF NOT EXISTS desired_capacity_bytes BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE node_profiles ADD COLUMN IF NOT EXISTS updated_at INTEGER",
    "ALTER TABLE node_profiles ADD COLUMN IF NOT EXISTS node_api_key TEXT",
    "ALTER TABLE node_profiles ADD COLUMN IF NOT EXISTS stripe_connected_account_id TEXT",
    "ALTER TABLE node_profiles ADD COLUMN IF NOT EXISTS payout_enabled BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE node_profiles ADD COLUMN IF NOT EXISTS payouts_paused BOOLEAN NOT NULL DEFAULT FALSE",
    "CREATE INDEX IF NOT EXISTS idx_node_profiles_owner_created ON node_profiles(owner_user_id, created_at)",
]


def init_phase1_node_provider_schema() -> None:
    with db_conn() as conn:
        with conn.cursor() as cur:
            for stmt in DDL:
                cur.execute(stmt)
        conn.commit()


def _bytes_to_gb_floor(value: int) -> int:
    return max(0, int(value // (1024 ** 3)))


def _gb_to_bytes(value: int) -> int:
    return int(value) * 1024 * 1024 * 1024


def _make_node_api_key() -> str:
    return secrets.token_urlsafe(32)


def _ensure_node_role(cur, uid: str) -> None:
    created = int(now_ts())
    cur.execute(
        """
        INSERT INTO user_roles(user_id, role, created_at)
        VALUES (%s,%s,%s)
        ON CONFLICT DO NOTHING
        """,
        (uid, "node", created),
    )


def _get_or_create_profile(cur, uid: str) -> Dict[str, Any]:
    cur.execute(
        """
        SELECT node_id, owner_user_id, created_at, updated_at,
               COALESCE(node_name, %s) AS node_name,
               COALESCE(desired_capacity_bytes, 0) AS desired_capacity_bytes,
               COALESCE(node_api_key, '') AS node_api_key,
               stripe_connected_account_id,
               COALESCE(payout_enabled, FALSE) AS payout_enabled,
               COALESCE(payouts_paused, FALSE) AS payouts_paused
        FROM node_profiles
        WHERE owner_user_id=%s
        ORDER BY created_at ASC NULLS LAST, node_id ASC
        LIMIT 1
        """,
        (ROOT_NODE_NAME, uid),
    )
    row = cur.fetchone()
    if row:
        return dict(row)

    created = int(now_ts())
    node_id = str(uuid.uuid4())
    node_api_key = _make_node_api_key()
    cur.execute(
        """
        INSERT INTO node_profiles(
            node_id, owner_user_id, created_at, updated_at, node_name, desired_capacity_bytes, node_api_key
        ) VALUES (%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            node_id,
            uid,
            created,
            created,
            ROOT_NODE_NAME,
            _gb_to_bytes(DEFAULT_CAPACITY_GB),
            node_api_key,
        ),
    )
    _ensure_node_role(cur, uid)
    return {
        "node_id": node_id,
        "owner_user_id": uid,
        "created_at": created,
        "updated_at": created,
        "node_name": ROOT_NODE_NAME,
        "desired_capacity_bytes": _gb_to_bytes(DEFAULT_CAPACITY_GB),
        "node_api_key": node_api_key,
        "stripe_connected_account_id": None,
        "payout_enabled": False,
        "payouts_paused": False,
    }


def _fetch_profile(cur, uid: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT node_id, owner_user_id, created_at, updated_at,
               COALESCE(node_name, %s) AS node_name,
               COALESCE(desired_capacity_bytes, 0) AS desired_capacity_bytes,
               COALESCE(node_api_key, '') AS node_api_key,
               stripe_connected_account_id,
               COALESCE(payout_enabled, FALSE) AS payout_enabled,
               COALESCE(payouts_paused, FALSE) AS payouts_paused
        FROM node_profiles
        WHERE owner_user_id=%s
        ORDER BY created_at ASC NULLS LAST, node_id ASC
        LIMIT 1
        """,
        (ROOT_NODE_NAME, uid),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _runtime_for_node(cur, node_id: str, desired_capacity_bytes: int) -> Dict[str, Any]:
    cur.execute(
        """
        SELECT node_id, capacity_bytes, reserved_bytes, last_seen, meta_json
        FROM nodes
        WHERE node_id=%s
        """,
        (node_id,),
    )
    row = cur.fetchone()
    if not row:
        return {
            "online": False,
            "last_seen": None,
            "capacity_bytes": desired_capacity_bytes,
            "reserved_bytes": 0,
            "free_bytes": desired_capacity_bytes,
            "source": "desired_profile",
        }

    cap = int(row["capacity_bytes"] or 0)
    reserved = int(row["reserved_bytes"] or 0)
    last_seen = int(row["last_seen"] or 0)
    return {
        "online": last_seen >= int(now_ts()) - ONLINE_TTL_SEC,
        "last_seen": last_seen,
        "capacity_bytes": cap,
        "reserved_bytes": reserved,
        "free_bytes": max(0, cap - reserved),
        "source": "node_heartbeat",
        "meta_json": row["meta_json"],
    }


def _current_month_window(profile_created_at: Optional[int] = None) -> tuple[int, int]:
    now = int(now_ts())
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    month_start = datetime(dt.year, dt.month, 1, tzinfo=timezone.utc)
    start_ts = int(month_start.timestamp())
    if profile_created_at:
        start_ts = max(start_ts, int(profile_created_at))
    return start_ts, now


def _uptime_summary(cur, node_id: str, profile_created_at: Optional[int], heartbeat_interval_sec: int = 3) -> Dict[str, Any]:
    start_ts, end_ts = _current_month_window(profile_created_at)
    cur.execute(
        """
        SELECT COALESCE(SUM(sample_count), 0) AS sample_count,
               COALESCE(SUM(reserved_bytes_sum), 0) AS reserved_bytes_sum
        FROM node_heartbeat_hourly
        WHERE node_id=%s AND hour_start BETWEEN %s AND %s
        """,
        (node_id, (start_ts // 3600) * 3600, (end_ts // 3600) * 3600),
    )
    row = cur.fetchone()
    sample_count = int((row or {}).get("sample_count") or 0)
    reserved_bytes_sum = int((row or {}).get("reserved_bytes_sum") or 0)
    elapsed = max(1, end_ts - start_ts)
    expected_samples = max(1, int(round(elapsed / max(1, heartbeat_interval_sec))))
    avg_used_bytes = (reserved_bytes_sum / sample_count) if sample_count > 0 else None
    online_ratio = min(1.0, sample_count / expected_samples) if expected_samples > 0 else None
    return {
        "period_start": start_ts,
        "period_end": end_ts,
        "avg_used_bytes": int(avg_used_bytes) if avg_used_bytes is not None else None,
        "avg_used_gb": round((avg_used_bytes or 0) / float(1024 ** 3), 2) if avg_used_bytes is not None else None,
        "online_ratio": online_ratio,
        "sample_count": sample_count,
        "expected_samples": expected_samples,
    }


def _local_capacity_hint() -> Dict[str, Any]:
    try:
        target = _node_storage_dir_for_capacity_hint()
        os.makedirs(target, exist_ok=True)
        usage = shutil.disk_usage(target)
        offerable_bytes = int(usage.free * 0.90)
        return {
            "path": target,
            "total_bytes": int(usage.total),
            "free_bytes": int(usage.free),
            "offerable_bytes": offerable_bytes,
            "offerable_gb": _bytes_to_gb_floor(offerable_bytes),
            "source": "server_disk_usage_90pct",
        }
    except Exception:
        return {
            "path": _node_storage_dir_for_capacity_hint(),
            "total_bytes": 0,
            "free_bytes": 0,
            "offerable_bytes": 0,
            "offerable_gb": 0,
            "source": "unavailable",
        }


def _earnings(cur, node_id: str, desired_capacity_bytes: int = 0) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    cur.execute(
        """
        SELECT earning_id, node_id, period_start, period_end, gb_month, share_ratio,
               pool_amount_yen, gross_amount_yen, adjustments_yen, net_amount_yen, status, note, created_at, updated_at
        FROM node_earnings
        WHERE node_id=%s
        ORDER BY period_start DESC, created_at DESC
        LIMIT 6
        """,
        (node_id,),
    )
    items = [dict(row) for row in cur.fetchall()]
    total_net = int(sum(int(item.get("net_amount_yen") or 0) for item in items))
    total_gb_month = float(sum(float(item.get("gb_month") or 0.0) for item in items))
    avg_rate = (total_net / total_gb_month) if total_gb_month > 0 else 0.0
    desired_capacity_gb = float(desired_capacity_bytes) / float(1024 ** 3) if desired_capacity_bytes else 0.0
    util_ratios: List[float] = []
    if desired_capacity_gb > 0:
        for item in items:
            gb_month = float(item.get("gb_month") or 0.0)
            period_start = int(item.get("period_start") or 0)
            period_end = int(item.get("period_end") or 0)
            if period_end > period_start:
                period_days = max(1.0, (period_end - period_start) / 86400.0)
                period_months = period_days / 30.0
            else:
                period_months = 1.0
            denom = desired_capacity_gb * period_months
            if denom > 0:
                util_ratios.append(max(0.0, min(1.0, gb_month / denom)))
    latest = items[0] if items else None
    summary = {
        "history_count": len(items),
        "total_net_amount_yen": total_net,
        "total_gb_month": total_gb_month,
        "avg_yen_per_gb_month": avg_rate,
        "avg_utilization_ratio": (sum(util_ratios) / len(util_ratios)) if util_ratios else None,
        "latest_period_net_yen": int(latest.get("net_amount_yen") or 0) if latest else 0,
        "latest_period_end": int(latest.get("period_end") or 0) if latest else None,
    }
    return summary, items


def _payouts(cur, node_id: str) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT payout_id, node_id, amount_yen, currency, provider, provider_ref, status, created_at, paid_at, failure_reason
        FROM node_payouts
        WHERE node_id=%s
        ORDER BY created_at DESC
        LIMIT 5
        """,
        (node_id,),
    )
    return [dict(row) for row in cur.fetchall()]


def _projection(desired_capacity_bytes: int, avg_yen_per_gb_month: float) -> Dict[str, Any]:
    desired_gb = float(desired_capacity_bytes) / float(1024 ** 3)
    util_levels = [0.30, 0.60, 0.90]
    scenarios = []
    for level in util_levels:
        estimated = desired_gb * level * avg_yen_per_gb_month
        scenarios.append(
            {
                "utilization_ratio": level,
                "estimated_gb_month": round(desired_gb * level, 2),
                "estimated_reward_yen": int(round(estimated)),
            }
        )
    return {
        "desired_capacity_gb": round(desired_gb, 2),
        "scenarios": scenarios,
    }


def _launch_payload(profile: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    desired_gb = _bytes_to_gb_floor(int(profile.get("desired_capacity_bytes") or 0))
    if desired_gb <= 0:
        return None
    node_id = str(profile["node_id"])
    node_api_key = str(profile.get("node_api_key") or "")
    runner_path = str((Path(__file__).resolve().parent / NODE_RUNNER_FILE).resolve())
    storage_dir = _node_storage_dir_for_capacity_hint()
    command = (
        f'python "{runner_path}" '
        f'--node-id "{node_id}" '
        f'--node-api-key "{node_api_key}" '
        f'--server "{NODE_SERVER_ENDPOINT}" '
        f'--storage-dir "{storage_dir}" '
        f'--capacity-gb {desired_gb}'
    )
    return {
        "runner_file": NODE_RUNNER_FILE,
        "runner_path": runner_path,
        "command": command,
        "node_id": node_id,
        "node_api_key": node_api_key,
        "server": NODE_SERVER_ENDPOINT,
        "storage_dir": storage_dir,
        "capacity_gb": desired_gb,
    }


@router.get("/node/provider/summary", response_model=NodeProviderSummaryOut)
def node_provider_summary(uid: str = Depends(current_user_id)) -> NodeProviderSummaryOut:
    init_phase1_node_provider_schema()
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            profile = _fetch_profile(cur, uid)
            if not profile:
                local_capacity = _local_capacity_hint()
                defaults = {
                    "node_name": ROOT_NODE_NAME,
                    "desired_capacity_gb": DEFAULT_CAPACITY_GB,
                    "suggested_slider_max_gb": max(500, int(local_capacity.get("offerable_gb") or 0)),
                }
                return NodeProviderSummaryOut(
                    profile=None,
                    runtime={
                        "online": False,
                        "last_seen": None,
                        "capacity_bytes": 0,
                        "reserved_bytes": 0,
                        "free_bytes": 0,
                        "source": "none",
                    },
                    earnings_summary={
                        "history_count": 0,
                        "total_net_amount_yen": 0,
                        "total_gb_month": 0.0,
                        "avg_yen_per_gb_month": 0.0,
                        "latest_period_net_yen": 0,
                        "latest_period_end": None,
                    },
                    recent_earnings=[],
                    recent_payouts=[],
                    reward_projection={
                        "desired_capacity_gb": float(DEFAULT_CAPACITY_GB),
                        "scenarios": [
                            {"utilization_ratio": 0.30, "estimated_gb_month": round(DEFAULT_CAPACITY_GB * 0.30, 2), "estimated_reward_yen": 0},
                            {"utilization_ratio": 0.60, "estimated_gb_month": round(DEFAULT_CAPACITY_GB * 0.60, 2), "estimated_reward_yen": 0},
                            {"utilization_ratio": 0.90, "estimated_gb_month": round(DEFAULT_CAPACITY_GB * 0.90, 2), "estimated_reward_yen": 0},
                        ],
                    },
                    stripe={
                        "configured": bool(os.environ.get("STRIPE_SECRET_KEY")),
                        "connected": False,
                        "payout_enabled": False,
                        "payouts_paused": False,
                    },
                    launch=None,
                    defaults=defaults,
                    local_capacity=local_capacity,
                    uptime_summary={
                        "period_start": None,
                        "period_end": None,
                        "avg_used_bytes": None,
                        "avg_used_gb": None,
                        "online_ratio": None,
                        "sample_count": 0,
                        "expected_samples": 0,
                    },
                )

            local_capacity = _local_capacity_hint()
            runtime = _runtime_for_node(cur, str(profile["node_id"]), int(profile.get("desired_capacity_bytes") or 0))
            earnings_summary, recent_earnings = _earnings(cur, str(profile["node_id"]), int(profile.get("desired_capacity_bytes") or 0))
            recent_payouts = _payouts(cur, str(profile["node_id"]))
            projection = _projection(int(profile.get("desired_capacity_bytes") or 0), float(earnings_summary["avg_yen_per_gb_month"]))
            uptime_summary = _uptime_summary(cur, str(profile["node_id"]), int(profile.get("created_at") or 0))
            profile_out = {
                **profile,
                "desired_capacity_gb": _bytes_to_gb_floor(int(profile.get("desired_capacity_bytes") or 0)),
                "node_api_key_preview": (str(profile.get("node_api_key") or "")[:6] + "…") if profile.get("node_api_key") else "",
            }
            return NodeProviderSummaryOut(
                profile=profile_out,
                runtime=runtime,
                earnings_summary=earnings_summary,
                recent_earnings=recent_earnings,
                recent_payouts=recent_payouts,
                reward_projection=projection,
                stripe={
                    "configured": bool(os.environ.get("STRIPE_SECRET_KEY")),
                    "connected": bool(profile.get("stripe_connected_account_id")),
                    "payout_enabled": bool(profile.get("payout_enabled")),
                    "payouts_paused": bool(profile.get("payouts_paused")),
                },
                launch=_launch_payload(profile),
                defaults={
                    "node_name": ROOT_NODE_NAME,
                    "desired_capacity_gb": DEFAULT_CAPACITY_GB,
                    "suggested_slider_max_gb": max(500, int(local_capacity.get("offerable_gb") or 0)),
                },
                local_capacity=local_capacity,
                uptime_summary=uptime_summary,
            )


@router.post("/node/provider/profile", response_model=NodeProviderSummaryOut)
def upsert_node_provider_profile(inp: NodeProviderProfileIn, uid: str = Depends(current_user_id)) -> NodeProviderSummaryOut:
    init_phase1_node_provider_schema()
    desired_capacity_bytes = _gb_to_bytes(inp.desired_capacity_gb)
    node_name = (inp.node_name or "").strip() or ROOT_NODE_NAME
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            profile = _get_or_create_profile(cur, uid)
            updated = int(now_ts())
            if not profile.get("node_api_key"):
                profile["node_api_key"] = _make_node_api_key()
            cur.execute(
                """
                UPDATE node_profiles
                SET node_name=%s,
                    desired_capacity_bytes=%s,
                    node_api_key=%s,
                    updated_at=%s
                WHERE node_id=%s
                """,
                (
                    node_name,
                    desired_capacity_bytes,
                    profile["node_api_key"],
                    updated,
                    profile["node_id"],
                ),
            )
            _ensure_node_role(cur, uid)
        conn.commit()
    return node_provider_summary(uid)


@router.post("/node/provider/rotate_api_key", response_model=RotateKeyOut)
def rotate_node_provider_api_key(uid: str = Depends(current_user_id)) -> RotateKeyOut:
    init_phase1_node_provider_schema()
    new_key = _make_node_api_key()
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            profile = _get_or_create_profile(cur, uid)
            cur.execute(
                "UPDATE node_profiles SET node_api_key=%s, updated_at=%s WHERE node_id=%s",
                (new_key, int(now_ts()), profile["node_id"]),
            )
        conn.commit()
    return RotateKeyOut(node_id=str(profile["node_id"]), node_api_key=new_key)


@router.delete("/node/provider/client_data")
def delete_node_provider_client_data(uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    """
    現在保存されているクライアント関連データを削除する。

    ここで削除するもの:
    - sync_clients: 同期クライアント heartbeat 表示用データ
    - nodes:       ノード runtime 表示用の最新状態

    残すもの:
    - node_profiles: ノード自体のプロフィール
    - node_earnings / node_payouts: 報酬・支払い履歴
    """
    init_phase1_node_provider_schema()
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            profile = _fetch_profile(cur, uid)
            if not profile:
                raise HTTPException(status_code=404, detail="node profile not found")

            node_id = str(profile["node_id"])

            cur.execute("DELETE FROM sync_clients WHERE user_id=%s", (uid,))
            deleted_sync_clients = int(cur.rowcount or 0)

            cur.execute("DELETE FROM nodes WHERE node_id=%s", (node_id,))
            deleted_runtime_rows = int(cur.rowcount or 0)

            # 停止後の UI 表示と実態を合わせるため、提供容量を 0 に戻す。
            cur.execute(
                """
                UPDATE node_profiles
                SET desired_capacity_bytes=0,
                    updated_at=%s
                WHERE node_id=%s AND owner_user_id=%s
                """,
                (int(now_ts()), node_id, uid),
            )
        conn.commit()

    return {
        "ok": True,
        "node_id": node_id,
        "deleted_sync_clients": deleted_sync_clients,
        "deleted_runtime_rows": deleted_runtime_rows,
    }


@router.post("/node/provider/stop")
def stop_node_provider(uid: str = Depends(current_user_id)) -> Dict[str, Any]:
    """
    ストレージ提供停止を1本で実行するAPI。

    処理内容:
    1. desired_capacity_bytes を 0 に戻す
    2. node_api_key を再生成する
    3. sync_clients の該当ユーザー行を削除する
    4. nodes の該当ノード行を削除し、runtime 表示を初期化する
    5. 最新の summary を返す
    """
    init_phase1_node_provider_schema()
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            profile = _fetch_profile(cur, uid)
            if not profile:
                raise HTTPException(status_code=404, detail="node profile not found")

            node_id = str(profile["node_id"])
            new_key = _make_node_api_key()
            ts = int(now_ts())

            cur.execute(
                """
                UPDATE node_profiles
                SET desired_capacity_bytes=0,
                    node_api_key=%s,
                    updated_at=%s
                WHERE node_id=%s AND owner_user_id=%s
                """,
                (new_key, ts, node_id, uid),
            )

            cur.execute("DELETE FROM sync_clients WHERE user_id=%s", (uid,))
            deleted_sync_clients = int(cur.rowcount or 0)

            cur.execute("DELETE FROM nodes WHERE node_id=%s", (node_id,))
            deleted_runtime_rows = int(cur.rowcount or 0)
        conn.commit()

    summary = node_provider_summary(uid)
    return {
        "ok": True,
        "rotated": {"node_id": node_id, "node_api_key": new_key},
        "deleted_sync_clients": deleted_sync_clients,
        "deleted_runtime_rows": deleted_runtime_rows,
        "summary": summary.model_dump() if hasattr(summary, "model_dump") else summary.dict(),
    }

