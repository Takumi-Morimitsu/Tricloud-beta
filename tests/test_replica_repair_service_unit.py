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

fake_object_gc = types.ModuleType("object_gc")
fake_object_gc.init_object_gc_schema = lambda *args, **kwargs: None
sys.modules["object_gc"] = fake_object_gc
sys.modules.pop("replica_repair_service", None)
replica_repair_service = importlib.import_module("replica_repair_service")


class RepairCursor:
    def __init__(self) -> None:
        self._one = None
        self._all = []
        self.executed = []

    def execute(self, query, params=None) -> None:
        text = " ".join(str(query).split())
        self.executed.append((text, params))
        self._one = None
        self._all = []
        if text.startswith("SELECT * FROM repair_jobs WHERE status IN"):
            self._all = [
                {
                    "repair_job_id": "repair-1",
                    "file_object_id": "object-1",
                    "status": "queued",
                    "attempt_count": 0,
                    "max_attempts": 4,
                    "created_at": 1,
                }
            ]
        elif text.startswith("UPDATE repair_jobs SET status='selecting_source'"):
            self._one = {
                "repair_job_id": "repair-1",
                "file_object_id": "object-1",
                "status": "selecting_source",
                "attempt_count": 1,
            }
        elif text.startswith("SELECT n.node_id FROM nodes n"):
            self._all = [{"node_id": "target-1"}, {"node_id": "target-2"}]
        elif text.startswith("UPDATE nodes SET reserved_bytes=reserved_bytes+"):
            self._one = {"node_id": params[1]}
        elif text.startswith("UPDATE repair_jobs SET status='selecting_target'"):
            self._one = {"repair_job_id": "repair-1"}

    def fetchone(self):
        value = self._one
        self._one = None
        return value

    def fetchall(self):
        value = list(self._all)
        self._all = []
        return value


class ReplicaRepairServiceTests(unittest.TestCase):
    def test_claim_uses_skip_locked_and_increments_attempt(self) -> None:
        cur = RepairCursor()
        jobs = replica_repair_service.claim_due_repair_jobs(
            cur,
            worker_id="worker-1",
            limit=1,
            ts=1_000,
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "selecting_source")
        select_sql = cur.executed[0][0]
        self.assertIn("FOR UPDATE SKIP LOCKED", select_sql)
        update_sql = cur.executed[1][0]
        self.assertIn("attempt_count=attempt_count+1", update_sql)

    def test_target_reservation_is_atomic_and_does_not_use_reliability_score(self) -> None:
        cur = RepairCursor()
        target = replica_repair_service.select_and_reserve_target(
            cur,
            repair_job_id="repair-1",
            file_object_id="object-1",
            file_size=1234,
            source_node_ids=["source-1"],
            online_after=900,
            ts=1_000,
        )
        self.assertEqual(target, "target-1")
        selection_sql = cur.executed[0][0]
        self.assertIn("capacity_bytes-n.reserved_bytes", selection_sql)
        self.assertIn("failure_domain", selection_sql)
        self.assertIn("FOR UPDATE OF n SKIP LOCKED", selection_sql)
        self.assertNotIn("reliability", selection_sql.lower())
        reserve_sql = cur.executed[1][0]
        self.assertIn("SET reserved_bytes=reserved_bytes+", reserve_sql)


if __name__ == "__main__":
    unittest.main()
