# -*- coding: utf-8 -*-
"""
billing_monthly_close_integrated.py
- 月次のユーザー請求書作成
- 月次のノード報酬計算

統合元:
- 料金計算_postgres.py
- ノード報酬計算_postgres.py

狙い:
- JST月次境界や時間重複計算の重複実装を1か所に集約する
- 1つの CLI から請求締め / 報酬締め / 両方実行を選べるようにする
"""

from __future__ import annotations

import argparse
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple

from psycopg.rows import dict_row

try:
    from meta_db_pg import db_conn, ensure_default_plan, now_ts
except Exception:
    from database_pg import db_conn, ensure_default_plan, now_ts  # type: ignore

from usage_metering import get_plan_for_user

GB = 1024 ** 3


# ------------------------
# 共通の期間計算
# ------------------------
def jst_month_start(ts: int) -> int:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=9)
    ms = datetime(dt.year, dt.month, 1, tzinfo=timezone.utc) - timedelta(hours=9)
    return int(ms.timestamp())



def prev_month_period(now: int) -> Tuple[int, int]:
    this_start = jst_month_start(now)
    dt = datetime.fromtimestamp(this_start, tz=timezone.utc) + timedelta(hours=9)
    y, m = dt.year, dt.month
    if m == 1:
        y -= 1
        m = 12
    else:
        m -= 1
    prev = datetime(y, m, 1, tzinfo=timezone.utc) - timedelta(hours=9)
    return int(prev.timestamp()), this_start



def overlap_seconds(a0: int, a1: int, b0: int, b1: int) -> int:
    s = max(a0, b0)
    e = min(a1, b1)
    return max(0, e - s)


# ------------------------
# 請求書作成側
# ------------------------
def compute_storage_gb_month(user_id: str, period_start: int, period_end: int) -> float:
    seconds_in_period = max(1, period_end - period_start)
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT size_bytes,start_ts,COALESCE(end_ts,%s) AS end_ts
                FROM object_lifetimes
                WHERE owner_user_id=%s AND start_ts < %s AND COALESCE(end_ts,%s) > %s
                """,
                (period_end, user_id, period_end, period_end, period_start),
            )
            gb_seconds = 0.0
            for row in cur.fetchall():
                secs = overlap_seconds(int(row["start_ts"]), int(row["end_ts"]), period_start, period_end)
                gb_seconds += (float(row["size_bytes"]) / GB) * float(secs)
            return gb_seconds / float(seconds_in_period)



def compute_transfer_gb(user_id: str, period_start: int, period_end: int) -> Tuple[float, float]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  COALESCE(SUM(CASE WHEN direction='ingress' THEN bytes ELSE 0 END),0) AS in_b,
                  COALESCE(SUM(CASE WHEN direction='egress' THEN bytes ELSE 0 END),0) AS out_b
                FROM transfer_events
                WHERE user_id=%s AND ts >= %s AND ts < %s
                """,
                (user_id, period_start, period_end),
            )
            in_b, out_b = cur.fetchone()
            return float(in_b) / GB, float(out_b) / GB



def ensure_invoice_not_exists(cur, user_id: str, period_start: int) -> bool:
    cur.execute("SELECT 1 FROM invoices WHERE user_id=%s AND period_start=%s", (user_id, period_start))
    return cur.fetchone() is None



def run_invoice_monthly_close(period_start: int | None = None, period_end: int | None = None) -> Dict[str, str]:
    ensure_default_plan()
    if period_start is None or period_end is None:
        period_start, period_end = prev_month_period(now_ts())

    created_ids: Dict[str, str] = {}
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT user_id FROM users")
            users = [str(row["user_id"]) for row in cur.fetchall()]

    for uid in users:
        plan = get_plan_for_user(uid)
        storage = compute_storage_gb_month(uid, period_start, period_end)
        ing_gb, eg_gb = compute_transfer_gb(uid, period_start, period_end)

        base = int(plan.monthly_base)
        storage_y = int(round(storage * float(plan.storage_yen_per_gb_month)))
        ing_y = int(round(ing_gb * float(plan.ingress_yen_per_gb)))
        eg_y = int(round(eg_gb * float(plan.egress_yen_per_gb)))
        subtotal = base + storage_y + ing_y + eg_y
        total = subtotal

        with db_conn() as conn2:
            with conn2.cursor() as cur2:
                if not ensure_invoice_not_exists(cur2, uid, period_start):
                    continue
                invoice_id = str(uuid.uuid4())
                created = now_ts()
                cur2.execute(
                    """
                    INSERT INTO invoices(invoice_id,user_id,period_start,period_end,subtotal,total,status,created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (invoice_id, uid, period_start, period_end, subtotal, total, "open", created),
                )
                cur2.execute(
                    "INSERT INTO invoice_lines(invoice_id,kind,quantity,unit_yen,amount_yen) VALUES (%s,%s,%s,%s,%s)",
                    (invoice_id, "base", 1.0, float(base), int(base)),
                )
                cur2.execute(
                    "INSERT INTO invoice_lines(invoice_id,kind,quantity,unit_yen,amount_yen) VALUES (%s,%s,%s,%s,%s)",
                    (invoice_id, "storage", float(storage), float(plan.storage_yen_per_gb_month), int(storage_y)),
                )
                cur2.execute(
                    "INSERT INTO invoice_lines(invoice_id,kind,quantity,unit_yen,amount_yen) VALUES (%s,%s,%s,%s,%s)",
                    (invoice_id, "ingress", float(ing_gb), float(plan.ingress_yen_per_gb), int(ing_y)),
                )
                cur2.execute(
                    "INSERT INTO invoice_lines(invoice_id,kind,quantity,unit_yen,amount_yen) VALUES (%s,%s,%s,%s,%s)",
                    (invoice_id, "egress", float(eg_gb), float(plan.egress_yen_per_gb), int(eg_y)),
                )
            conn2.commit()
        created_ids[uid] = invoice_id

    return created_ids


# ------------------------
# ノード報酬側
# ------------------------
def _table_exists(conn, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
        return cur.fetchone()[0] is not None



def compute_node_gb_months(period_start: int, period_end: int) -> Dict[str, float]:
    seconds_in_period = max(1, period_end - period_start)
    out: Dict[str, float] = {}

    with db_conn() as conn:
        use_replica_lifetimes = _table_exists(conn, "replica_lifetimes")

        if use_replica_lifetimes:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT node_id, size_bytes, start_ts, COALESCE(end_ts,%s) AS end_ts
                    FROM replica_lifetimes
                    WHERE start_ts < %s AND COALESCE(end_ts,%s) > %s
                    """,
                    (period_end, period_end, period_end, period_start),
                )
                gb_seconds_by_node: Dict[str, float] = {}
                for row in cur.fetchall():
                    secs = overlap_seconds(int(row["start_ts"]), int(row["end_ts"]), period_start, period_end)
                    if secs <= 0:
                        continue
                    node_id = str(row["node_id"])
                    gb_seconds_by_node[node_id] = gb_seconds_by_node.get(node_id, 0.0) + (float(row["size_bytes"]) / GB) * float(secs)
                for node_id, gb_seconds in gb_seconds_by_node.items():
                    out[node_id] = gb_seconds / float(seconds_in_period)
                return out

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT r.node_id, ol.size_bytes, ol.start_ts, COALESCE(ol.end_ts,%s) AS end_ts
                FROM replicas r
                JOIN object_lifetimes ol ON ol.file_object_id = r.file_object_id
                WHERE ol.start_ts < %s AND COALESCE(ol.end_ts,%s) > %s
                """,
                (period_end, period_end, period_end, period_start),
            )
            gb_seconds_by_node: Dict[str, float] = {}
            for row in cur.fetchall():
                secs = overlap_seconds(int(row["start_ts"]), int(row["end_ts"]), period_start, period_end)
                if secs <= 0:
                    continue
                node_id = str(row["node_id"])
                gb_seconds_by_node[node_id] = gb_seconds_by_node.get(node_id, 0.0) + (float(row["size_bytes"]) / GB) * float(secs)
            for node_id, gb_seconds in gb_seconds_by_node.items():
                out[node_id] = gb_seconds / float(seconds_in_period)
    return out



def _compute_pool_from_invoices(period_start: int, period_end: int, reward_ratio: float) -> int:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(total),0) FROM invoices WHERE period_start=%s AND period_end=%s AND status IN ('open','paid')",
                (period_start, period_end),
            )
            gross = int(cur.fetchone()[0] or 0)
    return int(round(gross * reward_ratio))



def run_node_monthly_close(
    period_start: int | None = None,
    period_end: int | None = None,
    *,
    pool_yen: int | None = None,
    reward_ratio: float | None = None,
) -> Dict[str, int]:
    if period_start is None or period_end is None:
        period_start, period_end = prev_month_period(now_ts())

    if reward_ratio is None:
        reward_ratio = float(os.environ.get("NODE_REWARD_POOL_RATIO", "0.30"))
    if pool_yen is None:
        pool_yen = _compute_pool_from_invoices(period_start, period_end, reward_ratio)

    node_gb_months = compute_node_gb_months(period_start, period_end)
    total_gb_month = sum(value for value in node_gb_months.values() if value > 0)
    results: Dict[str, int] = {}

    if total_gb_month <= 0 or pool_yen <= 0:
        return results

    raw_amounts: List[tuple[str, float, float]] = []
    for node_id, gbm in sorted(node_gb_months.items()):
        if gbm <= 0:
            continue
        ratio = gbm / total_gb_month
        raw = pool_yen * ratio
        raw_amounts.append((node_id, gbm, raw))

    rounded = {node_id: int(raw) for node_id, _, raw in raw_amounts}
    remainder = int(pool_yen - sum(rounded.values()))
    if remainder != 0 and raw_amounts:
        fracs = sorted([(node_id, raw - int(raw)) for node_id, _, raw in raw_amounts], key=lambda x: x[1], reverse=True)
        idx = 0
        step = 1 if remainder > 0 else -1
        while remainder != 0 and fracs:
            node_id = fracs[idx % len(fracs)][0]
            rounded[node_id] += step
            remainder -= step
            idx += 1

    created = now_ts()
    with db_conn() as conn:
        with conn.cursor() as cur:
            for node_id, gbm, _ in raw_amounts:
                share_ratio = gbm / total_gb_month if total_gb_month > 0 else 0.0
                amount = int(rounded[node_id])
                results[node_id] = amount
                cur.execute(
                    """
                    INSERT INTO node_earnings(
                        earning_id,node_id,period_start,period_end,gb_month,share_ratio,
                        pool_amount_yen,gross_amount_yen,adjustments_yen,net_amount_yen,status,created_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (node_id, period_start, period_end)
                    DO UPDATE SET
                        gb_month=EXCLUDED.gb_month,
                        share_ratio=EXCLUDED.share_ratio,
                        pool_amount_yen=EXCLUDED.pool_amount_yen,
                        gross_amount_yen=EXCLUDED.gross_amount_yen,
                        net_amount_yen=EXCLUDED.net_amount_yen,
                        updated_at=%s
                    """,
                    (
                        str(uuid.uuid4()), node_id, period_start, period_end, float(gbm), float(share_ratio),
                        int(pool_yen), int(amount), 0, int(amount), "calculated", created, created,
                    ),
                )
        conn.commit()
    return results


# ------------------------
# 両方まとめて実行
# ------------------------
def run_full_monthly_close(*, pool_yen: int | None = None, reward_ratio: float | None = None) -> Dict[str, object]:
    ensure_default_plan()
    period_start, period_end = prev_month_period(now_ts())
    invoices = run_invoice_monthly_close(period_start, period_end)
    rewards = run_node_monthly_close(period_start, period_end, pool_yen=pool_yen, reward_ratio=reward_ratio)
    return {
        "period_start": period_start,
        "period_end": period_end,
        "invoice_count": len(invoices),
        "node_reward_count": len(rewards),
        "node_reward_total_yen": sum(rewards.values()),
    }



def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-monthly-close", action="store_true", help="請求書作成とノード報酬計算を両方実行")
    ap.add_argument("--run-invoice-close", action="store_true", help="請求書作成のみ実行")
    ap.add_argument("--run-node-close", action="store_true", help="ノード報酬計算のみ実行")
    ap.add_argument("--pool-yen", type=int, default=None, help="ノード報酬原資（円）")
    ap.add_argument("--reward-ratio", type=float, default=None, help="請求売上に対する報酬原資比率")
    args = ap.parse_args()

    if args.run_monthly_close:
        print(run_full_monthly_close(pool_yen=args.pool_yen, reward_ratio=args.reward_ratio))
    elif args.run_invoice_close:
        print({"invoice_count": len(run_invoice_monthly_close())})
    elif args.run_node_close:
        result = run_node_monthly_close(pool_yen=args.pool_yen, reward_ratio=args.reward_ratio)
        print({"node_count": len(result), "total_yen": sum(result.values())})
    else:
        print("No action. Use --run-monthly-close / --run-invoice-close / --run-node-close")


if __name__ == "__main__":
    main()
