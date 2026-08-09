# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import os
import sys
import types
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# The production module uses meta_db_pg only for db_conn/now_ts.  A small stub
# keeps these unit tests independent from psycopg and a running PostgreSQL.
fake_meta_db = types.ModuleType("meta_db_pg")
fake_meta_db.db_conn = None
fake_meta_db.now_ts = lambda: 1_000_000
sys.modules["meta_db_pg"] = fake_meta_db

replica_health_service = importlib.import_module("replica_health_service")


class DetectionCursor:
    def __init__(self) -> None:
        self._one = None
        self._all = []

    def execute(self, query, params=None) -> None:
        text = str(query)
        if "to_regclass" in text:
            table_name = str((params or [""])[0])
            self._one = {"to_regclass": table_name if table_name.endswith(".items") else None}
            self._all = []
            return
        if "WITH tracked_objects" in text:
            self._one = None
            self._all = [
                {
                    "file_object_id": "object-short",
                    "owner_user_id": "user-1",
                    "size_bytes": 10,
                    "logical_replica_count": 3,
                    "online_replica_count": 2,
                    "eligible_replica_count": 2,
                    "verified_healthy_count": 0,
                    "known_bad_replica_count": 0,
                },
                {
                    "file_object_id": "object-ok",
                    "owner_user_id": "user-1",
                    "size_bytes": 20,
                    "logical_replica_count": 3,
                    "online_replica_count": 3,
                    "eligible_replica_count": 3,
                    "verified_healthy_count": 0,
                    "known_bad_replica_count": 0,
                },
            ]
            return
        raise AssertionError(f"unexpected SQL: {text[:120]}")

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._all)


class ReplicaHealthServiceTests(unittest.TestCase):
    def test_safe_rollout_detection_uses_eligible_count_not_missing_audit_history(self) -> None:
        shortages = replica_health_service.detect_under_replicated_objects(
            DetectionCursor(),
            target_replicas=3,
            require_recent_audit=False,
            ts=1_000_000,
        )
        self.assertEqual(len(shortages), 1)
        self.assertEqual(shortages[0]["file_object_id"], "object-short")
        self.assertEqual(shortages[0]["deficit"], 1)
        self.assertEqual(shortages[0]["reason"], "long_offline_or_unusable_replica")

    def test_audit_gating_is_explicit_and_not_the_default(self) -> None:
        shortages = replica_health_service.detect_under_replicated_objects(
            DetectionCursor(),
            target_replicas=3,
            require_recent_audit=True,
            ts=1_000_000,
        )
        self.assertEqual({row["file_object_id"] for row in shortages}, {"object-short", "object-ok"})
        self.assertTrue(all(row["audit_required_for_decision"] for row in shortages))

    def test_failure_rejects_healthy_status(self) -> None:
        with self.assertRaises(ValueError):
            replica_health_service.mark_replica_failure(
                object(),
                file_object_id="object-1",
                node_id="node-1",
                status="healthy",
                error="not allowed",
            )


if __name__ == "__main__":
    unittest.main()
