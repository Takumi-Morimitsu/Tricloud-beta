# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from repair_transfer import RepairTransferState, retry_delay_seconds


class RepairTransferStateTests(unittest.TestCase):
    def _state(self) -> RepairTransferState:
        return RepairTransferState(
            repair_job_id="repair-1",
            file_object_id="object-1",
            file_size=10,
            chunk_size=4,
            target_node_id="target",
            source_node_ids=["source-a", "target", "source-b", "source-a"],
            max_source_attempts=2,
            target_transfer_id="target-attempt-a",
        )

    def test_source_failover_uses_unique_ids_and_never_uses_target(self) -> None:
        state = self._state()
        ids = iter(["source-attempt-a", "source-attempt-b"])
        self.assertEqual(
            state.begin_next_source(now=1.0, id_factory=lambda: next(ids)),
            ("source-a", "source-attempt-a"),
        )
        self.assertEqual(
            state.begin_next_source(now=2.0, id_factory=lambda: next(ids)),
            ("source-b", "source-attempt-b"),
        )
        self.assertIsNone(state.begin_next_source(now=3.0, id_factory=lambda: "unexpected"))

    def test_target_attempt_reset_rejects_delayed_target_frames(self) -> None:
        state = self._state()
        old_id, new_id = state.reset_target_transfer(now=5.0, id_factory=lambda: "target-attempt-b")
        self.assertEqual(old_id, "target-attempt-a")
        self.assertEqual(new_id, "target-attempt-b")
        self.assertFalse(state.accepts_target(node_id="target", transfer_id=old_id))
        self.assertTrue(state.accepts_target(node_id="target", transfer_id=new_id))

    def test_ciphertext_chunks_must_cover_the_complete_object_once(self) -> None:
        state = self._state()
        state.begin_next_source(now=1.0, id_factory=lambda: "source-attempt-a")
        self.assertEqual(state.total_chunks, 3)
        self.assertEqual(state.observe_source_chunk(0, 4, now=1.1), "new")
        self.assertEqual(state.observe_source_chunk(0, 4, now=1.2), "duplicate")
        self.assertEqual(state.observe_source_chunk(3, 1, now=1.3), "invalid")
        self.assertFalse(state.source_stream_complete())
        self.assertEqual(state.observe_source_chunk(1, 4, now=1.4), "new")
        self.assertEqual(state.observe_source_chunk(2, 2, now=1.5), "new")
        self.assertTrue(state.source_stream_complete())
        self.assertEqual(state.copied_bytes, 10)

    def test_retry_backoff_is_bounded(self) -> None:
        delays = [30, 300, 1800]
        self.assertEqual(retry_delay_seconds(1, delays), 30)
        self.assertEqual(retry_delay_seconds(2, delays), 300)
        self.assertEqual(retry_delay_seconds(9, delays), 1800)


if __name__ == "__main__":
    unittest.main()
