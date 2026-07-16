# -*- coding: utf-8 -*-
"""
node heartbeat monthly stats patch

目的:
- ノード heartbeat を1時間単位で集計し、
  その月の「平均何GB貸し出されていたか」と「どれだけオンラインだったか」を
  provider summary から表示できるようにする。
- 既存の nodes テーブルは壊さず、補助テーブルを追加する薄い patch とする。
"""

from __future__ import annotations

from typing import Any

DDL = [
    """
    CREATE TABLE IF NOT EXISTS node_heartbeat_hourly (
        node_id TEXT NOT NULL,
        hour_start INTEGER NOT NULL,
        sample_count BIGINT NOT NULL DEFAULT 0,
        reserved_bytes_sum NUMERIC NOT NULL DEFAULT 0,
        capacity_bytes_max BIGINT NOT NULL DEFAULT 0,
        first_seen INTEGER,
        last_seen INTEGER,
        PRIMARY KEY (node_id, hour_start)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_node_heartbeat_hourly_node_hour ON node_heartbeat_hourly(node_id, hour_start DESC)",
]


def init_node_heartbeat_stats_schema(cur) -> None:
    for stmt in DDL:
        cur.execute(stmt)


def record_node_heartbeat_sample(cur, *, node_id: str, reserved_bytes: int, capacity_bytes: int, ts: int) -> None:
    hour_start = int(ts // 3600) * 3600
    cur.execute(
        """
        INSERT INTO node_heartbeat_hourly(
            node_id, hour_start, sample_count, reserved_bytes_sum, capacity_bytes_max, first_seen, last_seen
        ) VALUES (%s,%s,1,%s,%s,%s,%s)
        ON CONFLICT (node_id, hour_start) DO UPDATE SET
          sample_count = node_heartbeat_hourly.sample_count + 1,
          reserved_bytes_sum = node_heartbeat_hourly.reserved_bytes_sum + EXCLUDED.reserved_bytes_sum,
          capacity_bytes_max = GREATEST(node_heartbeat_hourly.capacity_bytes_max, EXCLUDED.capacity_bytes_max),
          first_seen = LEAST(node_heartbeat_hourly.first_seen, EXCLUDED.first_seen),
          last_seen = GREATEST(node_heartbeat_hourly.last_seen, EXCLUDED.last_seen)
        """,
        (node_id, hour_start, int(reserved_bytes), int(capacity_bytes), int(ts), int(ts)),
    )
