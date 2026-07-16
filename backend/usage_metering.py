
# 使用量計算_postgres.py
# -*- coding: utf-8 -*-
"""
PostgreSQL版：使用量計測と日次転送枠
- transfer_events を shared/normal で分けて集計
- 実測ベース：DataServerが送れた分だけ transfer_events に追加
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple

from psycopg.rows import dict_row

from meta_db_pg import db_conn, now_ts, ensure_default_plan


GB = 1024 ** 3


@dataclass
class Plan:
    plan_id: str
    monthly_base: int
    storage_yen_per_gb_month: float
    ingress_yen_per_gb: float
    egress_yen_per_gb: float
    daily_egress_gb_limit: Optional[float]
    daily_shared_egress_gb_limit: Optional[float]


def _jst_day_start(ts: int) -> int:
    """JST日次境界（00:00 JST）"""
    return ((ts + 9 * 3600) // 86400) * 86400 - 9 * 3600


def _rolling_window_start(ts: int, window_seconds: int) -> int:
    """直近window_secondsのローリング窓の開始時刻（UTC秒）"""
    return ts - window_seconds



    # JST = UTC+9
    # day boundary: floor((ts+9h)/86400)*86400 - 9h
    return ((ts + 9*3600) // 86400) * 86400 - 9*3600


def get_plan_for_user(user_id: str) -> Plan:
    ensure_default_plan()
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT plan_id,status FROM subscriptions WHERE user_id=%s", (user_id,))
            sub = cur.fetchone()
            if not sub or sub["status"] != "active":
                plan_id = "dev-standard"
            else:
                plan_id = sub["plan_id"]

            cur.execute("SELECT * FROM billing_plans WHERE plan_id=%s", (plan_id,))
            p = cur.fetchone()
            if not p:
                # fallback
                cur.execute("SELECT * FROM billing_plans WHERE plan_id=%s", ("dev-standard",))
                p = cur.fetchone()
            return Plan(
                plan_id=p["plan_id"],
                monthly_base=int(p["monthly_base"]),
                storage_yen_per_gb_month=float(p["storage_yen_per_gb_month"]),
                ingress_yen_per_gb=float(p["ingress_yen_per_gb"]),
                egress_yen_per_gb=float(p["egress_yen_per_gb"]),
                daily_egress_gb_limit=float(p["daily_egress_gb_limit"]) if p["daily_egress_gb_limit"] is not None else None,
                daily_shared_egress_gb_limit=float(p["daily_shared_egress_gb_limit"]) if p["daily_shared_egress_gb_limit"] is not None else None,
            )


def get_daily_egress_limit_bytes(user_id: str, *, is_shared: bool) -> Optional[int]:
    plan = get_plan_for_user(user_id)
    lim_gb = plan.daily_shared_egress_gb_limit if is_shared else plan.daily_egress_gb_limit
    if lim_gb is None:
        return None
    return int(lim_gb * GB)


def get_daily_egress_used_bytes(user_id: str, day_ts: Optional[int] = None, *, is_shared: bool) -> int:
    if day_ts is None:
        day_ts = now_ts()
    start = _jst_day_start(day_ts)
    end = start + 86400
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(bytes),0)
                FROM transfer_events
                WHERE user_id=%s AND direction='egress' AND ts >= %s AND ts < %s AND is_shared=%s
                """,
                (user_id, start, end, is_shared)
            )
            return int(cur.fetchone()[0])


def get_rolling_egress_used_bytes(user_id: str, *, ts: Optional[int] = None, window_seconds: int = 86400, is_shared: bool = False) -> int:
    """直近ローリング窓での egress 使用量（bytes）。
    Google Driveの『しばらくしてから再試行』に寄せ、明確なリセット時刻を返さない実装向け。
    """
    if ts is None:
        ts = now_ts()
    start = _rolling_window_start(ts, window_seconds)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(bytes),0)
                FROM transfer_events
                WHERE user_id=%s AND direction='egress' AND ts >= %s AND ts <= %s AND is_shared=%s
                """,
                (user_id, start, ts, is_shared)
            )
            return int(cur.fetchone()[0])


def record_transfer_event(user_id: str, direction: str, bytes_: int, *, ts: Optional[int] = None,
                          file_object_id: Optional[str] = None, is_shared: bool = False) -> None:
    if ts is None:
        ts = now_ts()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO transfer_events(user_id,direction,bytes,ts,file_object_id,is_shared) VALUES (%s,%s,%s,%s,%s,%s)",
                (user_id, direction, int(bytes_), int(ts), file_object_id, is_shared)
            )
        conn.commit()


def ensure_object_lifetime_started(file_object_id: str, owner_user_id: str, size_bytes: int, *, start_ts: Optional[int] = None) -> None:
    if start_ts is None:
        start_ts = now_ts()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM object_lifetimes WHERE file_object_id=%s AND end_ts IS NULL", (file_object_id,))
            if cur.fetchone():
                return
            cur.execute(
                "INSERT INTO object_lifetimes(file_object_id,owner_user_id,size_bytes,start_ts,end_ts) VALUES (%s,%s,%s,%s,NULL)",
                (file_object_id, owner_user_id, int(size_bytes), int(start_ts))
            )
        conn.commit()


def end_object_lifetime(file_object_id: str, *, end_ts: Optional[int] = None) -> None:
    if end_ts is None:
        end_ts = now_ts()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE object_lifetimes SET end_ts=%s WHERE file_object_id=%s AND end_ts IS NULL",
                (int(end_ts), file_object_id)
            )
        conn.commit()


def check_cap_allow_send(charge_user_id: str, *, is_shared: bool, bytes_to_send: int, ts: Optional[int] = None) -> Tuple[bool, int, Optional[int]]:
    """転送枠チェック。
    - 通常DL: JST日次境界での集計
    - 共有リンクDL: 直近24hローリング窓での集計（Google Drive風：明確なリセット時刻を返さない）
    """
    if ts is None:
        ts = now_ts()
    limit_b = get_daily_egress_limit_bytes(charge_user_id, is_shared=is_shared)
    if limit_b is None:
        return True, 2**63 - 1, None
    if is_shared:
        used = get_rolling_egress_used_bytes(charge_user_id, ts=ts, window_seconds=86400, is_shared=True)
    else:
        used = get_daily_egress_used_bytes(charge_user_id, day_ts=ts, is_shared=False)
    remaining = max(0, limit_b - used)
    return remaining >= bytes_to_send, remaining, limit_b

