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


fake_meta = types.ModuleType("meta_db_pg")
fake_meta.db_conn = None
fake_meta.now_ts = lambda: 1_000_000
sys.modules["meta_db_pg"] = fake_meta

fake_health = types.ModuleType("replica_health_service")
fake_health._tracked_object_union = lambda cur: "SELECT file_object_id FROM objects"


def _healthy(cur, **kwargs):
    cur.health_calls.append(("healthy", kwargs))


def _failure(cur, **kwargs):
    cur.health_calls.append((kwargs["status"], kwargs))


fake_health.mark_replica_healthy = _healthy
fake_health.mark_replica_failure = _failure
sys.modules["replica_health_service"] = fake_health
sys.modules.pop("storage_audit_service", None)
storage_audit_service = importlib.import_module("storage_audit_service")


class AuditCursor:
    def __init__(self, *, purpose="scheduled", prior_failures=0, attempt_count=1, event_id="event-1") -> None:
        self.job = {
            "audit_job_id": "audit-1",
            "current_event_id": event_id,
            "file_object_id": "object-1",
            "node_id": "node-1",
            "status": "sent",
            "purpose": purpose,
            "repair_job_id": "repair-1" if purpose == "repair_verify" else None,
            "attempt_count": attempt_count,
        }
        self.prior_failures = prior_failures
        self._one = None
        self.health_calls = []
        self.executed = []

    def execute(self, query, params=None) -> None:
        text = " ".join(str(query).split())
        self.executed.append((text, params))
        if text.startswith("SELECT * FROM audit_jobs"):
            self._one = dict(self.job)
        elif text.startswith("SELECT file_object_id FROM objects"):
            self._one = {"file_object_id": self.job["file_object_id"]}
        elif text.startswith("SELECT consecutive_failures"):
            self._one = {"consecutive_failures": self.prior_failures}
        else:
            self._one = None

    def fetchone(self):
        value = self._one
        self._one = None
        return value


class StorageAuditOutcomeTests(unittest.TestCase):
    def test_success_marks_replica_healthy_and_completes(self) -> None:
        cur = AuditCursor()
        result = storage_audit_service.complete_audit_job(
            cur,
            audit_job_id="audit-1",
            event_id="event-1",
            outcome="ok",
            got_hash="abc",
            ts=1_000_000,
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(cur.health_calls[0][0], "healthy")

    def test_first_hash_mismatch_retries_as_suspect(self) -> None:
        cur = AuditCursor(prior_failures=0, attempt_count=1)
        result = storage_audit_service.complete_audit_job(
            cur,
            audit_job_id="audit-1",
            event_id="event-1",
            outcome="hash_mismatch",
            got_hash="wrong",
            mismatch_corrupt_threshold=2,
            ts=1_000_000,
        )
        self.assertEqual(result["status"], "retry_wait")
        self.assertEqual(result["health_status"], "suspect")
        self.assertFalse(result["repair_needed"])

    def test_repeated_hash_mismatch_becomes_corrupt_and_requests_repair(self) -> None:
        cur = AuditCursor(prior_failures=1, attempt_count=2)
        result = storage_audit_service.complete_audit_job(
            cur,
            audit_job_id="audit-1",
            event_id="event-1",
            outcome="hash_mismatch",
            got_hash="wrong",
            mismatch_corrupt_threshold=2,
            ts=1_000_000,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["health_status"], "corrupt")
        self.assertTrue(result["repair_needed"])

    def test_retry_attempt_id_rejects_delayed_result(self) -> None:
        cur = AuditCursor(event_id="event-new")
        result = storage_audit_service.complete_audit_job(
            cur,
            audit_job_id="audit-1",
            event_id="event-old",
            outcome="ok",
            ts=1_000_000,
        )
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "stale_attempt")
        self.assertEqual(cur.health_calls, [])

    def test_failed_repair_verification_does_not_create_a_second_repair(self) -> None:
        cur = AuditCursor(purpose="repair_verify", prior_failures=0)
        result = storage_audit_service.complete_audit_job(
            cur,
            audit_job_id="audit-1",
            event_id="event-1",
            outcome="missing",
            ts=1_000_000,
        )
        self.assertTrue(result["terminal"])
        self.assertFalse(result["repair_needed"])
        self.assertEqual(result["repair_job_id"], "repair-1")


if __name__ == "__main__":
    unittest.main()
