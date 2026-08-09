# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import tempfile
import types
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

fake_zmq = types.ModuleType("zmq")
fake_zmq.DEALER = 1
fake_zmq.Again = type("Again", (Exception,), {})
fake_zmq.ZMQError = type("ZMQError", (Exception,), {})
fake_zmq.ETERM = 2
fake_zmq.Context = object
sys.modules["zmq"] = fake_zmq

fake_crypto = types.ModuleType("crypto_common_keywrap")
fake_crypto.jdump = lambda value: json.dumps(value, ensure_ascii=False).encode("utf-8")
fake_crypto.jload = lambda value: json.loads(value.decode("utf-8"))
fake_crypto.now_ts = lambda: 1_000_000
fake_crypto.sha256_hex = lambda value: hashlib.sha256(value).hexdigest()
fake_crypto.b = lambda value: str(value).encode("utf-8")
fake_crypto.s = lambda value: value.decode("utf-8") if isinstance(value, bytes) else str(value)
fake_crypto.ceil_div = lambda left, right: (int(left) + int(right) - 1) // int(right)
sys.modules["crypto_common_keywrap"] = fake_crypto
sys.modules.pop("node", None)
node = importlib.import_module("node")


class NodeStorageIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.instance = object.__new__(node.Node)
        self.instance.base = self.temp.name
        self.instance.transfers = {}
        os.makedirs(os.path.join(self.temp.name, "objects"), exist_ok=True)
        os.makedirs(os.path.join(self.temp.name, "tmp", "transfers"), exist_ok=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_object(self, object_id: str, data: bytes, *, transfer_id: str = "") -> None:
        chunks = os.path.join(node.obj_dir(self.temp.name, object_id), "chunks")
        os.makedirs(chunks, exist_ok=True)
        with open(os.path.join(chunks, "0.bin"), "wb") as handle:
            handle.write(data)
        with open(node.meta_path(self.temp.name, object_id), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "file_size": len(data),
                    "chunk_size": max(1, len(data)),
                    "total_chunks": 1,
                    "repair_transfer_id": transfer_id,
                },
                handle,
            )

    def test_audit_hashes_only_the_requested_ciphertext_slice(self) -> None:
        self._write_object("object-1", b"0123456789")
        status, digest, message = self.instance._audit_ciphertext_slice("object-1", 0, 2, 4)
        self.assertEqual(status, "ok")
        self.assertEqual(digest, hashlib.sha256(b"2345").hexdigest())
        self.assertEqual(message, "")

    def test_audit_distinguishes_missing_or_truncated_data(self) -> None:
        self._write_object("object-1", b"abc")
        self.assertEqual(self.instance._audit_ciphertext_slice("object-1", 4, 0, 1)[0], "missing")
        self.assertEqual(self.instance._audit_ciphertext_slice("object-1", 0, 2, 4)[0], "missing")

    def test_empty_object_metadata_has_a_deterministic_audit_tag(self) -> None:
        object_dir = node.obj_dir(self.temp.name, "empty")
        os.makedirs(object_dir, exist_ok=True)
        with open(node.meta_path(self.temp.name, "empty"), "w", encoding="utf-8") as handle:
            json.dump({"file_size": 0, "chunk_size": 262144, "total_chunks": 0}, handle)
        status, digest, _ = self.instance._audit_ciphertext_slice("empty", -1, 0, 0)
        self.assertEqual(status, "ok")
        self.assertEqual(digest, hashlib.sha256(b"meta:0:262144:0").hexdigest())

    def test_stale_cleanup_cannot_delete_a_newer_repair_copy(self) -> None:
        self._write_object("object-1", b"ciphertext", transfer_id="new-attempt")
        self.assertFalse(self.instance._abort_repair_transfer("old-attempt", "object-1"))
        self.assertTrue(os.path.exists(node.meta_path(self.temp.name, "object-1")))
        self.assertTrue(self.instance._abort_repair_transfer("new-attempt", "object-1"))
        self.assertFalse(os.path.exists(node.obj_dir(self.temp.name, "object-1")))

    def test_repair_finalize_records_attempt_identity(self) -> None:
        transfer = node.TransferSession(
            transfer_id="attempt-1",
            repair_job_id="repair-1",
            file_object_id="object-1",
            file_size=5,
            chunk_size=5,
            total_chunks=1,
        )
        self.instance._transfer_write_chunk("attempt-1", 0, b"abcde")
        self.instance._transfer_finalize(transfer)
        meta = self.instance._load_meta("object-1")
        self.assertEqual(meta["repair_transfer_id"], "attempt-1")
        self.assertEqual(meta["repair_job_id"], "repair-1")


if __name__ == "__main__":
    unittest.main()
