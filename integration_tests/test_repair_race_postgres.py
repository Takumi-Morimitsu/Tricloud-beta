# -*- coding: utf-8 -*-
"""PostgreSQL transaction regressions for object-scoped repair admission.

This suite is intentionally separate from the fast unit-test discovery.  It
writes only UUID-prefixed fixtures and refuses any non-loopback or non-test DB.
Set ``TRICLOUD_PG_TEST_DATABASE_URL`` explicitly before running it.
"""

from __future__ import annotations

import os
import ipaddress
import queue
import sys
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import admin_service
from object_gc import gc_unreferenced_objects
from replica_repair_service import ACTIVE_REPAIR_STATUSES, complete_repair_job


class RepairRacePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database_url = os.environ.get("TRICLOUD_PG_TEST_DATABASE_URL", "").strip()
        if not cls.database_url:
            raise RuntimeError("TRICLOUD_PG_TEST_DATABASE_URL is required")
        info = conninfo_to_dict(cls.database_url)
        host = str(info.get("host") or "").lower()
        database = str(info.get("dbname") or "")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("repair race tests require a loopback PostgreSQL host")
        if "test" not in database.lower():
            raise RuntimeError("repair race tests require a dedicated database whose name contains 'test'")
        with cls._connect(row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT current_database() AS database, inet_server_addr()::text AS address"
            ).fetchone()
            if not row or "test" not in str(row["database"]).lower():
                raise RuntimeError("connected PostgreSQL database is not a dedicated test database")
            address_text = str(row.get("address") or "")
            address = ipaddress.ip_interface(address_text).ip
            address_is_loopback = address.is_loopback or bool(
                getattr(address, "ipv4_mapped", None)
                and address.ipv4_mapped.is_loopback
            )
            if not address_is_loopback:
                raise RuntimeError("connected PostgreSQL server is not loopback")
            required = conn.execute(
                """
                SELECT to_regclass('public.repair_jobs') IS NOT NULL AS repair_jobs,
                       to_regclass('public.repair_job_events') IS NOT NULL AS repair_events,
                       to_regclass('public.replica_health') IS NOT NULL AS replica_health
                """
            ).fetchone()
            if not required or not all(required.values()):
                raise RuntimeError("Phase 1 repair schema must already be migrated")

    @classmethod
    def _connect(cls, *, row_factory=None):
        return psycopg.connect(
            cls.database_url,
            row_factory=row_factory,
            application_name="tricloud-repair-race-test",
        )

    def setUp(self) -> None:
        suffix = uuid.uuid4().hex
        self.user_id = f"race-user-{suffix}"
        self.object_id = f"race-object-{suffix}"
        self.job_id = f"race-job-{suffix}"
        self.node_ids = [f"race-node-{index}-{suffix}" for index in range(4)]

    def tearDown(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM chunk_audit_results WHERE event_id IN "
                    "(SELECT current_event_id FROM audit_jobs WHERE file_object_id=%s)",
                    (self.object_id,),
                )
                cur.execute("DELETE FROM audit_jobs WHERE file_object_id=%s", (self.object_id,))
                cur.execute(
                    "DELETE FROM repair_job_events WHERE repair_job_id IN "
                    "(SELECT repair_job_id FROM repair_jobs WHERE file_object_id=%s)",
                    (self.object_id,),
                )
                cur.execute("DELETE FROM repair_cleanup_queue WHERE file_object_id=%s", (self.object_id,))
                cur.execute("DELETE FROM object_gc_queue WHERE file_object_id=%s", (self.object_id,))
                cur.execute("SELECT to_regclass('public.replica_lifetimes')")
                if cur.fetchone()[0] is not None:
                    cur.execute("DELETE FROM replica_lifetimes WHERE file_object_id=%s", (self.object_id,))
                cur.execute("DELETE FROM replica_health WHERE file_object_id=%s", (self.object_id,))
                cur.execute("DELETE FROM replicas WHERE file_object_id=%s", (self.object_id,))
                cur.execute("DELETE FROM repair_jobs WHERE file_object_id=%s", (self.object_id,))
                cur.execute("DELETE FROM admin_audit_logs WHERE target_id=%s", (self.object_id,))
                cur.execute("DELETE FROM objects WHERE file_object_id=%s", (self.object_id,))
                cur.execute("DELETE FROM node_profiles WHERE node_id=ANY(%s::text[])", (self.node_ids,))
                cur.execute("DELETE FROM nodes WHERE node_id=ANY(%s::text[])", (self.node_ids,))
                cur.execute("DELETE FROM user_roles WHERE user_id=%s", (self.user_id,))
                cur.execute("DELETE FROM users WHERE user_id=%s", (self.user_id,))
            conn.commit()

    def _seed_verifying_repair(self, *, third_status: str) -> None:
        timestamp = int(time.time())
        healthy_1, healthy_2, third, target = self.node_ids
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users(user_id,email,password_hash,created_at,country_code) "
                    "VALUES (%s,%s,'local-test-hash',%s,'JP')",
                    (self.user_id, f"{self.user_id}@example.invalid", timestamp),
                )
                cur.execute(
                    "INSERT INTO objects(file_object_id,owner_user_id,size_bytes,chunk_size,created_at) "
                    "VALUES (%s,%s,100,64,%s)",
                    (self.object_id, self.user_id, timestamp),
                )
                for node_id in self.node_ids:
                    reserved = 100
                    cur.execute(
                        """
                        INSERT INTO nodes(node_id,last_seen,capacity_bytes,reserved_bytes,meta_json)
                        VALUES (%s,%s,1000000,%s,'{}')
                        """,
                        (node_id, timestamp, reserved),
                    )
                for node_id, status in (
                    (healthy_1, "healthy"),
                    (healthy_2, "healthy"),
                    (third, third_status),
                ):
                    cur.execute(
                        "INSERT INTO replicas(file_object_id,node_id,created_at) VALUES (%s,%s,%s)",
                        (self.object_id, node_id, timestamp),
                    )
                    cur.execute(
                        """
                        INSERT INTO replica_health(
                            file_object_id,node_id,status,last_verified_at,last_success_at,
                            last_failure_at,consecutive_failures,last_error,created_at,updated_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s)
                        """,
                        (
                            self.object_id,
                            node_id,
                            status,
                            timestamp if status == "healthy" else None,
                            timestamp if status == "healthy" else None,
                            timestamp if status != "healthy" else None,
                            1 if status != "healthy" else 0,
                            timestamp,
                            timestamp,
                        ),
                    )
                cur.execute(
                    """
                    INSERT INTO replica_health(
                        file_object_id,node_id,status,last_verified_at,last_success_at,
                        last_failure_at,consecutive_failures,last_error,created_at,updated_at
                    ) VALUES (%s,%s,'repairing',NULL,NULL,NULL,0,NULL,%s,%s)
                    """,
                    (self.object_id, target, timestamp, timestamp),
                )
                cur.execute(
                    """
                    INSERT INTO repair_jobs(
                        repair_job_id,file_object_id,source_node_id,target_node_id,reason,status,
                        attempt_count,next_retry_at,last_error,worker_id,idempotency_key,
                        created_at,started_at,finished_at,updated_at,max_attempts,lease_expires_at,
                        transfer_id,reserved_bytes,copied_bytes,total_chunks,verified_at,
                        canceled_at,failure_code
                    ) VALUES (%s,%s,%s,%s,'race_fixture','verifying',1,NULL,NULL,'race-worker',%s,
                              %s,%s,NULL,%s,4,%s,%s,100,100,2,NULL,NULL,NULL)
                    """,
                    (
                        self.job_id,
                        self.object_id,
                        healthy_1,
                        target,
                        f"idem-{self.job_id}",
                        timestamp,
                        timestamp,
                        timestamp,
                        timestamp + 60,
                        f"transfer-{self.job_id}",
                    ),
                )
            conn.commit()

    @contextmanager
    def _admin_connection(self, backend_pids: queue.Queue[int]):
        conn = self._connect()
        backend_pids.put(conn.info.backend_pid)
        conn.execute("SET lock_timeout='5s'")
        conn.execute("SET statement_timeout='10s'")
        try:
            yield conn
        finally:
            conn.close()

    def _wait_for_lock(self, backend_pid: int) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with self._connect(row_factory=dict_row) as observer:
                row = observer.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                    (int(backend_pid),),
                ).fetchone()
            if row and row.get("wait_event_type") == "Lock":
                return
            time.sleep(0.05)
        self.fail("concurrent repair admission did not reach the PostgreSQL lock boundary")

    def test_manual_admission_rechecks_after_concurrent_completion(self) -> None:
        self._seed_verifying_repair(third_status="corrupt")
        backend_pids: queue.Queue[int] = queue.Queue()
        conn_a = self._connect()
        future = None
        try:
            with conn_a.cursor(row_factory=dict_row) as cur:
                completed = complete_repair_job(
                    cur,
                    repair_job_id=self.job_id,
                    target_node_id=self.node_ids[3],
                )
            self.assertTrue(completed["applied"])
            self.assertEqual(completed["status"], "completed")

            def admit():
                try:
                    admin_service.create_manual_repair(
                        admin_user_id=self.user_id,
                        file_object_id=self.object_id,
                        reason="concurrent regression",
                        audit_context={},
                    )
                except ValueError as exc:
                    return "rejected", str(exc)
                return "created", None

            with ThreadPoolExecutor(max_workers=1) as pool:
                with patch.object(
                    admin_service,
                    "db_conn",
                    lambda: self._admin_connection(backend_pids),
                ):
                    future = pool.submit(admit)
                    backend_pid = backend_pids.get(timeout=5)
                    self._wait_for_lock(backend_pid)
                    conn_a.commit()
                    outcome, detail = future.result(timeout=10)
            self.assertEqual(outcome, "rejected")
            self.assertIn("target number of healthy replicas", detail or "")
        finally:
            if conn_a.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                conn_a.rollback()
            conn_a.close()

        with self._connect(row_factory=dict_row) as conn:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT r.node_id) AS logical_count,
                       COUNT(DISTINCT r.node_id) FILTER (WHERE h.status='healthy') AS healthy_count,
                       COUNT(DISTINCT j.repair_job_id) FILTER (WHERE j.status=ANY(%s::text[])) AS active_jobs
                FROM replicas r
                LEFT JOIN replica_health h
                  ON h.file_object_id=r.file_object_id AND h.node_id=r.node_id
                LEFT JOIN repair_jobs j ON j.file_object_id=r.file_object_id
                WHERE r.file_object_id=%s
                """,
                (list(ACTIVE_REPAIR_STATUSES), self.object_id),
            ).fetchone()
            self.assertEqual(row["logical_count"], 3)
            self.assertEqual(row["healthy_count"], 3)
            self.assertEqual(row["active_jobs"], 0)
            repair_count = conn.execute(
                "SELECT COUNT(*) AS count FROM repair_jobs WHERE file_object_id=%s",
                (self.object_id,),
            ).fetchone()["count"]
            self.assertEqual(repair_count, 1)

    def test_publish_discards_stale_target_instead_of_creating_fourth_copy(self) -> None:
        self._seed_verifying_repair(third_status="healthy")
        with self._connect(row_factory=dict_row) as conn:
            result = complete_repair_job(
                conn.cursor(),
                repair_job_id=self.job_id,
                target_node_id=self.node_ids[3],
            )
            conn.commit()
        self.assertTrue(result["applied"])
        self.assertEqual(result["status"], "canceled")
        self.assertEqual(result["reason"], "target_already_satisfied")
        self.assertFalse(result["published"])

        with self._connect(row_factory=dict_row) as conn:
            counts = conn.execute(
                """
                SELECT COUNT(*) AS logical_count,
                       COUNT(*) FILTER (WHERE h.status='healthy') AS healthy_count
                FROM replicas r
                LEFT JOIN replica_health h
                  ON h.file_object_id=r.file_object_id AND h.node_id=r.node_id
                WHERE r.file_object_id=%s
                """,
                (self.object_id,),
            ).fetchone()
            job = conn.execute(
                "SELECT status,failure_code FROM repair_jobs WHERE repair_job_id=%s",
                (self.job_id,),
            ).fetchone()
            cleanup = conn.execute(
                "SELECT COUNT(*) AS count FROM repair_cleanup_queue WHERE repair_job_id=%s",
                (self.job_id,),
            ).fetchone()["count"]
            target_health = conn.execute(
                "SELECT status FROM replica_health WHERE file_object_id=%s AND node_id=%s",
                (self.object_id, self.node_ids[3]),
            ).fetchone()
        self.assertEqual(counts["logical_count"], 3)
        self.assertEqual(counts["healthy_count"], 3)
        self.assertEqual(job["status"], "canceled")
        self.assertEqual(job["failure_code"], "target_already_satisfied")
        self.assertEqual(cleanup, 1)
        self.assertEqual(target_health["status"], "deleted")

    def test_publish_requires_a_known_bad_replica_before_replacement(self) -> None:
        self._seed_verifying_repair(third_status="suspect")
        with self._connect(row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                result = complete_repair_job(
                    cur,
                    repair_job_id=self.job_id,
                    target_node_id=self.node_ids[3],
                )
            conn.commit()
        self.assertTrue(result["applied"])
        self.assertEqual(result["status"], "canceled")
        self.assertEqual(result["reason"], "no_safe_retirement_candidate")
        self.assertFalse(result["published"])

        with self._connect(row_factory=dict_row) as conn:
            counts = conn.execute(
                """
                SELECT COUNT(*) AS logical_count,
                       COUNT(*) FILTER (WHERE h.status='healthy') AS healthy_count
                FROM replicas r
                LEFT JOIN replica_health h
                  ON h.file_object_id=r.file_object_id AND h.node_id=r.node_id
                WHERE r.file_object_id=%s
                """,
                (self.object_id,),
            ).fetchone()
            target_replica = conn.execute(
                "SELECT COUNT(*) AS count FROM replicas WHERE file_object_id=%s AND node_id=%s",
                (self.object_id, self.node_ids[3]),
            ).fetchone()["count"]
        self.assertEqual(counts["logical_count"], 3)
        self.assertEqual(counts["healthy_count"], 2)
        self.assertEqual(target_replica, 0)

    def test_gc_waits_for_repair_publish_and_leaves_no_orphan_replica(self) -> None:
        self._seed_verifying_repair(third_status="corrupt")
        backend_pids: queue.Queue[int] = queue.Queue()
        conn_a = self._connect()
        try:
            with conn_a.cursor(row_factory=dict_row) as cur:
                completed = complete_repair_job(
                    cur,
                    repair_job_id=self.job_id,
                    target_node_id=self.node_ids[3],
                )
            self.assertEqual(completed["status"], "completed")

            def collect_object():
                with self._connect(row_factory=dict_row) as conn:
                    backend_pids.put(conn.info.backend_pid)
                    conn.execute("SET lock_timeout='5s'")
                    conn.execute("SET statement_timeout='10s'")
                    with conn.cursor() as cur:
                        result = gc_unreferenced_objects(
                            cur,
                            [self.object_id],
                            reason="repair-race-regression",
                        )
                    conn.commit()
                    return result

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(collect_object)
                backend_pid = backend_pids.get(timeout=5)
                self._wait_for_lock(backend_pid)
                conn_a.commit()
                gc_result = future.result(timeout=10)
            self.assertEqual(gc_result["gc_object_count"], 1)
            self.assertEqual(gc_result["released_replica_count"], 3)
        finally:
            if conn_a.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                conn_a.rollback()
            conn_a.close()

        with self._connect(row_factory=dict_row) as conn:
            object_count = conn.execute(
                "SELECT COUNT(*) AS count FROM objects WHERE file_object_id=%s",
                (self.object_id,),
            ).fetchone()["count"]
            replica_count = conn.execute(
                "SELECT COUNT(*) AS count FROM replicas WHERE file_object_id=%s",
                (self.object_id,),
            ).fetchone()["count"]
        self.assertEqual(object_count, 0)
        self.assertEqual(replica_count, 0)


if __name__ == "__main__":
    unittest.main()
