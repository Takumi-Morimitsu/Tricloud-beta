# -*- coding: utf-8 -*-
"""State tracking for replica-aware downloads.

The client sees one stable ``transfer_id``.  Each node attempt receives a
different ``node_transfer_id`` so that delayed frames from a failed attempt
cannot be mixed into the next attempt.

This module is intentionally independent from ZeroMQ and PostgreSQL.  Keeping
the state machine pure makes the failover rules testable without a running
DataServer or database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Set, Tuple
import time
import uuid


@dataclass
class DownloadFailoverState:
    transfer_id: str
    candidate_node_ids: List[str]
    total_chunks: int
    max_attempts: int = 3
    candidate_index: int = -1
    current_node_id: Optional[str] = None
    current_node_transfer_id: Optional[str] = None
    attempt_started_ts: float = 0.0
    last_activity_ts: float = 0.0
    attempt_bytes: int = 0
    got: Set[int] = field(default_factory=set)
    attempt_got: Set[int] = field(default_factory=set)
    attempted_node_ids: List[str] = field(default_factory=list)

    def begin_next_attempt(
        self,
        *,
        now: Optional[float] = None,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> Optional[Tuple[str, str]]:
        """Advance to the next candidate and return ``(node_id, attempt_id)``.

        ``None`` means that either the candidate list or the configured attempt
        budget has been exhausted.
        """
        if len(self.attempted_node_ids) >= max(1, int(self.max_attempts)):
            return None

        next_index = self.candidate_index + 1
        if next_index >= len(self.candidate_node_ids):
            return None

        node_id = str(self.candidate_node_ids[next_index])
        node_transfer_id = str(id_factory())
        timestamp = float(time.monotonic() if now is None else now)

        self.candidate_index = next_index
        self.current_node_id = node_id
        self.current_node_transfer_id = node_transfer_id
        self.attempt_started_ts = timestamp
        self.last_activity_ts = timestamp
        self.attempt_bytes = 0
        self.attempt_got.clear()
        self.attempted_node_ids.append(node_id)
        return node_id, node_transfer_id

    def accepts_frame(self, *, node_id: str, node_transfer_id: str) -> bool:
        """Return whether a node frame belongs to the active attempt."""
        return (
            bool(self.current_node_id)
            and bool(self.current_node_transfer_id)
            and str(node_id) == self.current_node_id
            and str(node_transfer_id) == self.current_node_transfer_id
        )

    def observe_chunk(self, chunk_id: int, size_bytes: int, *, now: Optional[float] = None) -> str:
        """Record one validated ciphertext chunk.

        Returns ``"new"`` for a chunk that should be forwarded to the client,
        ``"duplicate"`` for a chunk already delivered by an earlier attempt,
        or ``"invalid"`` when the id is outside the object range.
        """
        cid = int(chunk_id)
        if cid < 0 or cid >= int(self.total_chunks):
            return "invalid"

        self.last_activity_ts = float(time.monotonic() if now is None else now)
        self.attempt_bytes += max(0, int(size_bytes))
        self.attempt_got.add(cid)

        if cid in self.got:
            return "duplicate"
        self.got.add(cid)
        return "new"

    def touch(self, *, now: Optional[float] = None) -> None:
        self.last_activity_ts = float(time.monotonic() if now is None else now)

    def global_missing(self) -> List[int]:
        return [cid for cid in range(int(self.total_chunks)) if cid not in self.got]

    def current_attempt_missing(self) -> List[int]:
        return [cid for cid in range(int(self.total_chunks)) if cid not in self.attempt_got]

    def timed_out(self, timeout_sec: float, *, now: Optional[float] = None) -> bool:
        timestamp = float(time.monotonic() if now is None else now)
        return timestamp - float(self.last_activity_ts) >= max(0.1, float(timeout_sec))
