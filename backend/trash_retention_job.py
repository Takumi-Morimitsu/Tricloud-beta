# -*- coding: utf-8 -*-
"""
trash_retention_job.py

ごみ箱に入ってから30日を超えたアイテムを DB から整理するための簡易ジョブ。
cron / Windows タスク スケジューラ等で1日1回まわす前提。
"""

from __future__ import annotations

import os
from typing import Set

from psycopg.rows import dict_row

from meta_db_pg import db_conn, now_ts
from items_phase2_patch import _collect_descendants
from object_gc import collect_file_object_ids_for_items, gc_unreferenced_objects

TRASH_RETENTION_DAYS = int(os.environ.get("TRASH_RETENTION_DAYS", "30"))
TRASH_RETENTION_SECONDS = TRASH_RETENTION_DAYS * 24 * 60 * 60


def main() -> None:
    cutoff_ts = int(now_ts()) - TRASH_RETENTION_SECONDS
    total = 0
    total_gc_objects = 0
    total_gc_queued = 0
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                WITH candidates AS (
                    SELECT item_id,parent_id,owner_user_id
                    FROM items
                    WHERE trashed_at IS NOT NULL
                      AND trashed_at <= %s
                )
                SELECT c.item_id, c.owner_user_id
                FROM candidates c
                LEFT JOIN candidates p ON c.parent_id = p.item_id
                WHERE p.item_id IS NULL
                ORDER BY c.owner_user_id, c.item_id
                """,
                (cutoff_ts,),
            )
            roots = [(str(r["item_id"]), str(r["owner_user_id"])) for r in cur.fetchall()]
            for root_id, uid in roots:
                all_ids: Set[str] = set()
                for node in _collect_descendants(cur, uid, root_id):
                    all_ids.add(str(node["item_id"]))
                if not all_ids:
                    continue
                ids = sorted(all_ids)
                candidate_file_object_ids = collect_file_object_ids_for_items(cur, ids)
                cur.execute("SELECT version_id FROM item_versions WHERE item_id = ANY(%s)", (ids,))
                version_ids = [str(row["version_id"]) for row in cur.fetchall()]
                if version_ids:
                    cur.execute("DELETE FROM item_version_parts WHERE version_id = ANY(%s)", (version_ids,))
                cur.execute("DELETE FROM item_versions WHERE item_id = ANY(%s)", (ids,))
                cur.execute("DELETE FROM item_parts WHERE item_id = ANY(%s)", (ids,))
                cur.execute("DELETE FROM items WHERE owner_user_id=%s AND item_id = ANY(%s)", (uid, ids))
                gc_result = gc_unreferenced_objects(cur, candidate_file_object_ids, reason="trash_retention_job")
                total += len(ids)
                total_gc_objects += int(gc_result.get("gc_object_count") or 0)
                total_gc_queued += int(gc_result.get("queued_delete_count") or 0)
        conn.commit()
    print({
        "purged_count": total,
        "gc_object_count": total_gc_objects,
        "queued_delete_count": total_gc_queued,
        "cutoff_ts": cutoff_ts,
        "retention_days": TRASH_RETENTION_DAYS,
    })


if __name__ == "__main__":
    main()
