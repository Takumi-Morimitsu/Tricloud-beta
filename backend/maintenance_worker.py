# -*- coding: utf-8 -*-
"""Phase 1 storage-maintenance scanner and operator CLI.

The default mode is observation-only: it detects under-replicated objects and
prints structured summaries.  Queue creation is opt-in.  Encrypted transfer is
performed separately by DataServer only when
``REPLICA_REPAIR_EXECUTION_ENABLED=1``; no HTTP request runs maintenance work.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List

from psycopg.rows import dict_row

from meta_db_pg import db_conn, init_schema
from replica_health_service import (
    detect_under_replicated_objects,
    init_storage_maintenance_schema,
)
from storage_audit_service import init_storage_audit_schema
from replica_repair_service import (
    cancel_repair_job,
    enqueue_repair_job,
    init_replica_repair_schema,
    list_repair_jobs,
    requeue_repair_job,
)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _log(level: str, event: str, **fields: Any) -> None:
    timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    suffix = ""
    if fields:
        suffix = " " + json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str)
    print(f"[tricloud-maintenance {timestamp}] {level.upper()} {event}{suffix}", flush=True)


def scan_once(
    *,
    queue_repair_jobs: bool = False,
    require_recent_audit: bool = False,
    limit: int = 1000,
) -> Dict[str, Any]:
    """Run one scan and optionally add queued-only repair jobs."""
    created_job_ids: List[str] = []
    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            shortages = detect_under_replicated_objects(
                cur,
                require_recent_audit=bool(require_recent_audit),
                limit=max(1, int(limit)),
            )
            if queue_repair_jobs:
                for shortage in shortages:
                    if int(shortage.get("deficit") or 0) <= 0:
                        continue
                    job_id = enqueue_repair_job(
                        cur,
                        file_object_id=str(shortage["file_object_id"]),
                        reason=str(shortage.get("reason") or "under_replicated"),
                    )
                    if job_id:
                        created_job_ids.append(job_id)
        if queue_repair_jobs:
            conn.commit()
        else:
            # Detection is read-only.  End the implicit psycopg transaction
            # explicitly so a long-running worker never remains idle in it.
            conn.rollback()

    return {
        "mode": "queue-only" if queue_repair_jobs else "detect-only",
        "require_recent_audit": bool(require_recent_audit),
        "under_replicated_count": len(shortages),
        "queued_repair_job_count": len(created_job_ids),
        "queued_repair_job_ids": created_job_ids,
        "objects": shortages,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Tricloud Phase 1 data-integrity maintenance")
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument(
        "--queue-repair-jobs",
        action="store_true",
        help="create idempotent queued repair jobs; DataServer execution has a separate feature flag",
    )
    parser.add_argument(
        "--require-recent-audit",
        action="store_true",
        help="require a recent successful audit when counting effective replicas",
    )
    parser.add_argument("--interval-sec", type=float, default=float(os.environ.get("REPLICA_HEALTH_SCAN_INTERVAL_SEC", "300")))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("REPLICA_HEALTH_SCAN_LIMIT", "1000")))
    parser.add_argument("--list-repair-jobs", action="store_true", help="print recent repair jobs and exit")
    parser.add_argument("--cancel-repair-job", metavar="JOB_ID", help="cancel one active repair and release its reservation")
    parser.add_argument("--retry-repair-job", metavar="JOB_ID", help="requeue one failed/canceled repair")
    parser.add_argument("--reset-attempts", action="store_true", help="with --retry-repair-job, reset the attempt counter")
    args = parser.parse_args()

    queue_enabled = bool(args.queue_repair_jobs) or _env_flag("REPLICA_REPAIR_QUEUE_ENABLED", False)
    require_audit = bool(args.require_recent_audit) or _env_flag("REPLICA_REQUIRE_RECENT_AUDIT", False)

    init_schema()
    init_storage_maintenance_schema()
    init_storage_audit_schema()
    init_replica_repair_schema()

    if args.list_repair_jobs or args.cancel_repair_job or args.retry_repair_job:
        with db_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                if args.cancel_repair_job:
                    output = cancel_repair_job(cur, repair_job_id=str(args.cancel_repair_job))
                elif args.retry_repair_job:
                    output = requeue_repair_job(
                        cur,
                        repair_job_id=str(args.retry_repair_job),
                        reset_attempts=bool(args.reset_attempts),
                    )
                else:
                    output = {"repair_jobs": list_repair_jobs(cur, limit=max(1, int(args.limit)))}
            if args.list_repair_jobs:
                conn.rollback()
            else:
                conn.commit()
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True, default=str), flush=True)
        return
    _log(
        "INFO",
        "maintenance worker started",
        mode="queue-only" if queue_enabled else "detect-only",
        require_recent_audit=require_audit,
        interval_sec=max(1.0, float(args.interval_sec)),
    )

    while True:
        try:
            result = scan_once(
                queue_repair_jobs=queue_enabled,
                require_recent_audit=require_audit,
                limit=max(1, int(args.limit)),
            )
            _log(
                "INFO",
                "replica health scan completed",
                mode=result["mode"],
                under_replicated_count=result["under_replicated_count"],
                queued_repair_job_count=result["queued_repair_job_count"],
                objects=result["objects"],
            )
        except Exception as exc:
            _log("ERROR", "replica health scan failed", error=f"{type(exc).__name__}: {exc}")

        if args.once:
            return
        time.sleep(max(1.0, float(args.interval_sec)))


if __name__ == "__main__":
    main()
