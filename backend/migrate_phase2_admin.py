# -*- coding: utf-8 -*-
"""Idempotently apply all prerequisites and the Phase 2 admin schema.

This script changes schema metadata only.  It does not start background
audits/repairs, call Stripe, pay rewards, or publish releases.
"""

from __future__ import annotations

import argparse
import json

from admin_schema import MIGRATION_ID, init_phase2_admin_schema, inspect_phase2_schema
from backup_targets_patch import init_backup_targets_schema
from database_extensions import (
    init_auth_profile_schema,
    init_multipart_and_quota_schema,
    init_rewards_and_stripe_schema,
)
from items_phase2_patch import init_phase2_items_schema
from meta_db_pg import db_conn, ensure_default_plan, init_schema
from node_heartbeat_stats_patch import init_node_heartbeat_stats_schema
from node_provider_v2 import init_phase1_node_provider_schema
from object_gc import init_object_gc_schema
from phase5_library_patch import init_phase5_library_schema
from replica_health_service import init_storage_maintenance_schema
from replica_repair_service import init_replica_repair_schema
from storage_audit_service import init_storage_audit_schema


def apply_migration(*, backfill_replica_health: bool = True) -> dict:
    init_schema()
    ensure_default_plan()
    init_rewards_and_stripe_schema()
    init_auth_profile_schema()
    init_multipart_and_quota_schema()
    init_phase1_node_provider_schema()

    with db_conn() as conn:
        with conn.cursor() as cur:
            init_node_heartbeat_stats_schema(cur)
        conn.commit()

    init_phase2_items_schema()
    init_object_gc_schema()
    init_storage_maintenance_schema(backfill=backfill_replica_health)
    init_storage_audit_schema()
    init_replica_repair_schema()
    init_phase5_library_schema()
    init_backup_targets_schema()
    init_phase2_admin_schema()

    with db_conn() as conn:
        with conn.cursor() as cur:
            return inspect_phase2_schema(cur)


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply the Tricloud Phase 2 admin schema")
    parser.add_argument(
        "--no-replica-health-backfill",
        action="store_true",
        help="skip the idempotent pending-health backfill for existing replicas",
    )
    args = parser.parse_args()
    result = apply_migration(backfill_replica_health=not args.no_replica_health_backfill)
    print(json.dumps({"migration_id": MIGRATION_ID, **result}, ensure_ascii=False, indent=2))
    return 0 if bool(result.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
