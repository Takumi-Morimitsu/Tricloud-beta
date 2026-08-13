# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import importlib
import os
import sys
import types
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


def _install_import_stubs() -> None:
    fake_zmq = types.ModuleType("zmq")
    fake_zmq.ROUTER = 1
    fake_zmq.DEALER = 2
    fake_zmq.LINGER = 3
    fake_zmq.ROUTER_HANDOVER = 4
    fake_zmq.HANDSHAKE_IVL = 5
    fake_zmq.HEARTBEAT_IVL = 6
    fake_zmq.HEARTBEAT_TIMEOUT = 7
    fake_zmq.HEARTBEAT_TTL = 8
    fake_zmq.POLLIN = 9
    fake_zmq.NOBLOCK = 10
    fake_zmq.ETERM = 11
    fake_zmq.__version__ = "test"
    fake_zmq.zmq_version = lambda: "test"
    fake_zmq.Context = object
    fake_zmq.Socket = object
    fake_zmq.Poller = object
    fake_zmq.ZMQError = type("ZMQError", (Exception,), {})
    fake_zmq.Again = type("Again", (Exception,), {})
    sys.modules["zmq"] = fake_zmq

    fake_rows = types.ModuleType("psycopg.rows")
    fake_rows.dict_row = object()
    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.rows = fake_rows
    sys.modules["psycopg"] = fake_psycopg
    sys.modules["psycopg.rows"] = fake_rows

    fake_meta = types.ModuleType("meta_db_pg")
    fake_meta.db_conn = None
    fake_meta.init_schema = lambda: None
    fake_meta.ensure_default_plan = lambda: None
    fake_meta.now_ts = lambda: 1_000_000
    sys.modules["meta_db_pg"] = fake_meta

    fake_usage = types.ModuleType("usage_metering")
    fake_usage.ensure_object_lifetime_started = lambda *args, **kwargs: None
    fake_usage.record_transfer_event = lambda *args, **kwargs: None
    fake_usage.check_cap_allow_send = lambda *args, **kwargs: (True, 10**9, None)
    sys.modules["usage_metering"] = fake_usage

    fake_heartbeat = types.ModuleType("node_heartbeat_stats_patch")
    fake_heartbeat.init_node_heartbeat_stats_schema = lambda *args, **kwargs: None
    fake_heartbeat.record_node_heartbeat_sample = lambda *args, **kwargs: None
    sys.modules["node_heartbeat_stats_patch"] = fake_heartbeat

    fake_gc = types.ModuleType("object_gc")
    for name in (
        "fetch_pending_object_gc_tasks",
        "init_object_gc_schema",
        "mark_object_gc_task_done_by_reply",
        "mark_object_gc_task_failed",
        "mark_object_gc_task_sent",
    ):
        setattr(fake_gc, name, lambda *args, **kwargs: None)
    sys.modules["object_gc"] = fake_gc

    fake_auth = types.ModuleType("auth_util")
    fake_auth.jwt_decode = lambda *args, **kwargs: None
    fake_auth.JWT_SECRET = "test"
    sys.modules["auth_util"] = fake_auth

    fake_health = types.ModuleType("replica_health_service")
    for name in (
        "fetch_download_candidates",
        "init_storage_maintenance_schema",
        "mark_replica_failure",
        "mark_replica_healthy",
        "mark_replicas_healthy",
        "record_node_transfer_metric",
    ):
        setattr(fake_health, name, lambda *args, **kwargs: None)
    fake_health._tracked_object_union = lambda *args, **kwargs: "SELECT file_object_id FROM objects"
    sys.modules["replica_health_service"] = fake_health


_install_import_stubs()
sys.modules.pop("server", None)
server = importlib.import_module("server")


class FakeClientSocket:
    def __init__(self) -> None:
        self.sent = []

    def send_multipart(self, frames) -> None:
        self.sent.append(list(frames))


class DataServerFailoverTests(unittest.TestCase):
    def _new_server(self):
        ds = object.__new__(server.DataServer)
        ds.transfers = {}
        ds.node_transfer_index = {}
        ds.repairs = {}
        ds.repair_source_index = {}
        ds.repair_target_index = {}
        ds.client_messages = []
        ds.node_messages = []
        ds.attempt_records = []
        ds.client_sock = FakeClientSocket()
        ds._send_client_json = lambda client_id, payload: ds.client_messages.append((client_id, payload))
        ds._send_node_json = lambda node_id, payload: ds.node_messages.append((node_id, payload))
        ds._send_node_data = lambda node_id, frames: ds.node_messages.append((node_id, list(frames)))
        ds._flush_transfer_meter = lambda ctx: None
        ds._record_download_attempt = lambda ctx, **kwargs: ds.attempt_records.append((ctx.failover.current_node_id, kwargs))
        return ds

    def _new_context(self):
        state = server.DownloadFailoverState(
            transfer_id="client-transfer",
            candidate_node_ids=["node-1", "node-2"],
            total_chunks=1,
            max_attempts=2,
        )
        return server.TransferCtx(
            transfer_id="client-transfer",
            client_id=b"client",
            file_object_id="object-1",
            total_chunks=1,
            charge_user_id="user-1",
            is_shared=False,
            failover=state,
            client_ready_sent=True,
        )

    def test_switch_removes_old_node_attempt_mapping(self) -> None:
        ds = self._new_server()
        ctx = self._new_context()
        ds.transfers[ctx.transfer_id] = ctx

        self.assertTrue(ds._start_next_download_attempt(ctx))
        old_attempt_id = str(ctx.failover.current_node_transfer_id)
        self.assertEqual(ds.node_transfer_index[old_attempt_id], ctx.transfer_id)

        self.assertTrue(ds._start_next_download_attempt(ctx, previous_error="timeout"))
        new_attempt_id = str(ctx.failover.current_node_transfer_id)
        self.assertNotEqual(old_attempt_id, new_attempt_id)
        self.assertNotIn(old_attempt_id, ds.node_transfer_index)
        self.assertEqual(ds.node_transfer_index[new_attempt_id], ctx.transfer_id)
        self.assertEqual(ds.attempt_records[0][0], "node-1")
        self.assertEqual(ds.client_messages[-1][1]["status"], "retrying")

    def test_node_attempt_id_is_rewritten_to_stable_client_id(self) -> None:
        ds = self._new_server()
        ctx = self._new_context()
        ds.transfers[ctx.transfer_id] = ctx
        ds._start_next_download_attempt(ctx)
        node_attempt_id = str(ctx.failover.current_node_transfer_id)
        data = b"ciphertext"
        digest = hashlib.sha256(data).hexdigest().encode("utf-8")

        ds._handle_node_stream([
            b"node-1",
            b"stream",
            node_attempt_id.encode("utf-8"),
            b"0",
            digest,
            data,
        ])

        self.assertEqual(len(ds.client_sock.sent), 1)
        self.assertEqual(ds.client_sock.sent[0][2], b"client-transfer")
        self.assertEqual(ctx.failover.global_missing(), [])

    def test_repair_relays_validated_ciphertext_to_target_without_client_delivery(self) -> None:
        ds = self._new_server()
        repair = server.RepairTransferState(
            repair_job_id="repair-1",
            file_object_id="object-1",
            file_size=10,
            chunk_size=10,
            target_node_id="target-1",
            source_node_ids=["source-1"],
            target_transfer_id="target-transfer",
        )
        repair.begin_next_source(id_factory=lambda: "source-transfer")
        ds.repairs[repair.repair_job_id] = repair
        ds.repair_source_index[repair.source_transfer_id] = repair.repair_job_id

        data = b"ciphertext"
        digest = hashlib.sha256(data).hexdigest().encode("utf-8")
        handled = ds._handle_repair_source_stream([
            b"source-1",
            b"stream",
            b"source-transfer",
            b"0",
            digest,
            data,
        ])

        self.assertTrue(handled)
        self.assertEqual(ds.client_sock.sent, [])
        self.assertEqual(ds.node_messages[0][0], "target-1")
        self.assertEqual(ds.node_messages[0][1][0], b"store")
        self.assertEqual(ds.node_messages[0][1][1], b"target-transfer")
        self.assertEqual(ds.node_messages[0][1][-1], data)

    def test_repair_hash_mismatch_switches_source_instead_of_relaying(self) -> None:
        ds = self._new_server()
        repair = server.RepairTransferState(
            repair_job_id="repair-1",
            file_object_id="object-1",
            file_size=10,
            chunk_size=10,
            target_node_id="target-1",
            source_node_ids=["source-1", "source-2"],
            target_transfer_id="target-transfer",
        )
        repair.begin_next_source(id_factory=lambda: "source-transfer")
        ds.repairs[repair.repair_job_id] = repair
        ds.repair_source_index[repair.source_transfer_id] = repair.repair_job_id
        failures = []
        ds._restart_repair_from_next_source = lambda ctx, code, **kwargs: failures.append((ctx, code, kwargs))

        handled = ds._handle_repair_source_stream([
            b"source-1",
            b"stream",
            b"source-transfer",
            b"0",
            b"not-the-real-hash",
            b"ciphertext",
        ])

        self.assertTrue(handled)
        self.assertEqual(ds.node_messages, [])
        self.assertEqual(failures[0][1], "repair_ciphertext_hash_mismatch")
        self.assertEqual(failures[0][2]["replica_status"], "corrupt")


if __name__ == "__main__":
    unittest.main()
