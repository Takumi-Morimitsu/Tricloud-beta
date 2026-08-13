# -*- coding: utf-8 -*-
"""Apply the additive Phase 1 storage data-integrity schema.

This migration creates new tables and indexes only.  It does not delete or
rewrite objects, replicas, node files, or existing application rows.
"""

from __future__ import annotations

from meta_db_pg import init_schema
from replica_health_service import init_storage_maintenance_schema
from storage_audit_service import init_storage_audit_schema
from replica_repair_service import init_replica_repair_schema


def main() -> None:
    init_schema()
    init_storage_maintenance_schema(backfill=True)
    init_storage_audit_schema()
    init_replica_repair_schema()
    print("Phase 1 storage data-integrity schema applied successfully.", flush=True)


if __name__ == "__main__":
    main()
