# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from download_failover import DownloadFailoverState


class DownloadFailoverStateTests(unittest.TestCase):
    def test_attempt_ids_are_unique_and_attempt_budget_is_enforced(self) -> None:
        ids = iter(["attempt-a", "attempt-b", "attempt-c"])
        state = DownloadFailoverState(
            transfer_id="client-transfer",
            candidate_node_ids=["node-1", "node-2", "node-3"],
            total_chunks=2,
            max_attempts=2,
        )

        self.assertEqual(
            state.begin_next_attempt(now=10.0, id_factory=lambda: next(ids)),
            ("node-1", "attempt-a"),
        )
        self.assertEqual(
            state.begin_next_attempt(now=20.0, id_factory=lambda: next(ids)),
            ("node-2", "attempt-b"),
        )
        self.assertIsNone(state.begin_next_attempt(now=30.0, id_factory=lambda: next(ids)))
        self.assertEqual(state.attempted_node_ids, ["node-1", "node-2"])

    def test_delayed_frames_from_previous_attempt_are_rejected(self) -> None:
        ids = iter(["attempt-a", "attempt-b"])
        state = DownloadFailoverState(
            transfer_id="client-transfer",
            candidate_node_ids=["node-1", "node-2"],
            total_chunks=1,
        )
        state.begin_next_attempt(now=1.0, id_factory=lambda: next(ids))
        state.begin_next_attempt(now=2.0, id_factory=lambda: next(ids))

        self.assertFalse(state.accepts_frame(node_id="node-1", node_transfer_id="attempt-a"))
        self.assertTrue(state.accepts_frame(node_id="node-2", node_transfer_id="attempt-b"))

    def test_chunks_can_complete_across_two_replicas_without_duplicate_forwarding(self) -> None:
        ids = iter(["attempt-a", "attempt-b"])
        state = DownloadFailoverState(
            transfer_id="client-transfer",
            candidate_node_ids=["node-1", "node-2"],
            total_chunks=3,
        )
        state.begin_next_attempt(now=1.0, id_factory=lambda: next(ids))
        self.assertEqual(state.observe_chunk(0, 100, now=1.1), "new")
        self.assertEqual(state.observe_chunk(1, 100, now=1.2), "new")
        self.assertEqual(state.global_missing(), [2])

        state.begin_next_attempt(now=2.0, id_factory=lambda: next(ids))
        self.assertEqual(state.observe_chunk(0, 100, now=2.1), "duplicate")
        self.assertEqual(state.observe_chunk(1, 100, now=2.2), "duplicate")
        self.assertEqual(state.observe_chunk(2, 100, now=2.3), "new")

        self.assertEqual(state.global_missing(), [])
        self.assertEqual(state.current_attempt_missing(), [])
        self.assertEqual(state.attempt_bytes, 300)

    def test_invalid_chunk_is_not_recorded_and_timeout_uses_last_activity(self) -> None:
        state = DownloadFailoverState(
            transfer_id="client-transfer",
            candidate_node_ids=["node-1"],
            total_chunks=2,
        )
        state.begin_next_attempt(now=10.0, id_factory=lambda: "attempt-a")
        self.assertEqual(state.observe_chunk(2, 10, now=11.0), "invalid")
        self.assertEqual(state.global_missing(), [0, 1])
        self.assertFalse(state.timed_out(4.0, now=13.9))
        self.assertTrue(state.timed_out(4.0, now=14.0))


if __name__ == "__main__":
    unittest.main()
