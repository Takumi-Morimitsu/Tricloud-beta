# -*- coding: utf-8 -*-
"""Object-scoped PostgreSQL locking for replica repair admission.

Every path that can admit or publish a repair uses the ``objects`` row as the
shared transaction lock.  This closes the window where one transaction sees a
replica shortage while another transaction is about to publish the replica
that satisfies it.
"""

from __future__ import annotations


def lock_repair_object(cur, *, file_object_id: str) -> bool:
    """Lock one object's repair state until the surrounding transaction ends."""
    cur.execute(
        "SELECT file_object_id FROM objects WHERE file_object_id=%s FOR UPDATE",
        (str(file_object_id),),
    )
    return cur.fetchone() is not None


def healthy_replica_count(cur, *, file_object_id: str) -> int:
    """Return the number of published replicas currently verified healthy."""
    cur.execute(
        """
        SELECT COUNT(*) FILTER (WHERE h.status='healthy') AS healthy_count
        FROM replicas r
        LEFT JOIN replica_health h
          ON h.file_object_id=r.file_object_id AND h.node_id=r.node_id
        WHERE r.file_object_id=%s
        """,
        (str(file_object_id),),
    )
    row = cur.fetchone()
    if row is None:
        return 0
    if isinstance(row, dict):
        return int(row.get("healthy_count") or 0)
    return int(row[0] or 0)


def repair_replica_counts(cur, *, file_object_id: str, target_node_id: str = "") -> dict[str, int]:
    """Return counts needed to decide whether a new target can be published."""
    cur.execute(
        """
        SELECT COUNT(*) AS logical_count,
               COUNT(*) FILTER (WHERE h.status='healthy') AS healthy_count,
               COUNT(*) FILTER (
                   WHERE h.status IN ('missing','corrupt','deleted')
               ) AS retirable_bad_count,
               COUNT(*) FILTER (WHERE r.node_id=%s) AS target_published_count
        FROM replicas r
        LEFT JOIN replica_health h
          ON h.file_object_id=r.file_object_id AND h.node_id=r.node_id
        WHERE r.file_object_id=%s
        """,
        (str(target_node_id), str(file_object_id)),
    )
    row = cur.fetchone()
    if row is None:
        return {
            "logical_count": 0,
            "healthy_count": 0,
            "retirable_bad_count": 0,
            "target_published_count": 0,
        }
    if isinstance(row, dict):
        return {
            "logical_count": int(row.get("logical_count") or 0),
            "healthy_count": int(row.get("healthy_count") or 0),
            "retirable_bad_count": int(row.get("retirable_bad_count") or 0),
            "target_published_count": int(row.get("target_published_count") or 0),
        }
    return {
        "logical_count": int(row[0] or 0),
        "healthy_count": int(row[1] or 0),
        "retirable_bad_count": int(row[2] or 0),
        "target_published_count": int(row[3] or 0),
    }


def repair_block_reason(
    counts: dict[str, int],
    *,
    target_replicas: int,
    publishing_new_target: bool = True,
) -> str | None:
    """Explain why repair admission/publication must stop, if it must."""
    target = max(1, int(target_replicas))
    if int(counts.get("healthy_count") or 0) >= target:
        return "target_already_satisfied"
    added = 1 if publishing_new_target else 0
    retirement_needed = max(0, int(counts.get("logical_count") or 0) + added - target)
    if int(counts.get("retirable_bad_count") or 0) < retirement_needed:
        return "no_safe_retirement_candidate"
    return None
