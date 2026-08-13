# -*- coding: utf-8 -*-
"""Pure in-memory state for one encrypted replica repair transfer.

The DataServer owns ZeroMQ sockets and persists job transitions separately.
This module intentionally contains no database or network access so the
failover rules can be unit-tested without PostgreSQL or running nodes.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Set, Tuple


IdFactory = Callable[[], str]


@dataclass
class RepairTransferState:
    repair_job_id: str
    file_object_id: str
    file_size: int
    chunk_size: int
    target_node_id: str
    source_node_ids: List[str]
    max_source_attempts: int = 3
    target_transfer_id: str = ""
    source_transfer_id: str = ""
    source_index: int = 0
    current_source_node_id: Optional[str] = None
    attempted_source_node_ids: List[str] = field(default_factory=list)
    received_chunk_ids: Set[int] = field(default_factory=set)
    target_ack_chunk_ids: Set[int] = field(default_factory=set)
    copied_bytes: int = 0
    persisted_copied_bytes: int = 0
    phase: str = "awaiting_target_ready"
    started_monotonic: float = field(default_factory=time.monotonic)
    last_activity_monotonic: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.file_size = max(0, int(self.file_size))
        self.chunk_size = max(1, int(self.chunk_size))
        self.max_source_attempts = max(1, int(self.max_source_attempts))
        self.source_node_ids = list(dict.fromkeys(str(x) for x in self.source_node_ids if x))
        if not self.target_transfer_id:
            self.target_transfer_id = uuid.uuid4().hex

    @property
    def total_chunks(self) -> int:
        return (self.file_size + self.chunk_size - 1) // self.chunk_size

    def touch(self, now: Optional[float] = None) -> None:
        self.last_activity_monotonic = time.monotonic() if now is None else float(now)

    def timed_out(self, timeout_sec: float, *, now: Optional[float] = None) -> bool:
        timestamp = time.monotonic() if now is None else float(now)
        return timestamp - self.last_activity_monotonic >= max(0.1, float(timeout_sec))

    def accepts_target(self, *, node_id: str, transfer_id: str) -> bool:
        return str(node_id) == self.target_node_id and str(transfer_id) == self.target_transfer_id

    def accepts_source(self, *, node_id: str, transfer_id: str) -> bool:
        return (
            self.current_source_node_id is not None
            and str(node_id) == self.current_source_node_id
            and str(transfer_id) == self.source_transfer_id
        )

    def begin_next_source(
        self,
        *,
        now: Optional[float] = None,
        id_factory: IdFactory = lambda: uuid.uuid4().hex,
    ) -> Optional[Tuple[str, str]]:
        if len(self.attempted_source_node_ids) >= self.max_source_attempts:
            return None
        while self.source_index < len(self.source_node_ids):
            node_id = self.source_node_ids[self.source_index]
            self.source_index += 1
            if node_id == self.target_node_id or node_id in self.attempted_source_node_ids:
                continue
            self.current_source_node_id = node_id
            self.source_transfer_id = str(id_factory())
            self.attempted_source_node_ids.append(node_id)
            self.received_chunk_ids.clear()
            self.target_ack_chunk_ids.clear()
            self.copied_bytes = 0
            self.persisted_copied_bytes = 0
            self.phase = "awaiting_source_ready"
            self.touch(now)
            return node_id, self.source_transfer_id
        return None

    def reset_target_transfer(
        self,
        *,
        now: Optional[float] = None,
        id_factory: IdFactory = lambda: uuid.uuid4().hex,
    ) -> Tuple[str, str]:
        """Use a fresh target id before retrying from another source."""
        old_id = self.target_transfer_id
        self.target_transfer_id = str(id_factory())
        self.source_transfer_id = ""
        self.current_source_node_id = None
        self.received_chunk_ids.clear()
        self.target_ack_chunk_ids.clear()
        self.copied_bytes = 0
        self.persisted_copied_bytes = 0
        self.phase = "awaiting_target_ready"
        self.touch(now)
        return old_id, self.target_transfer_id

    def observe_source_chunk(self, chunk_id: int, byte_count: int, *, now: Optional[float] = None) -> str:
        cid = int(chunk_id)
        if cid < 0 or cid >= self.total_chunks:
            return "invalid"
        self.touch(now)
        if cid in self.received_chunk_ids:
            return "duplicate"
        self.received_chunk_ids.add(cid)
        self.copied_bytes += max(0, int(byte_count))
        self.phase = "copying"
        return "new"

    def observe_target_ack(self, chunk_id: int, *, now: Optional[float] = None) -> str:
        cid = int(chunk_id)
        if cid < 0 or cid >= self.total_chunks:
            return "invalid"
        self.touch(now)
        if cid in self.target_ack_chunk_ids:
            return "duplicate"
        self.target_ack_chunk_ids.add(cid)
        return "new"

    def missing_source_chunks(self) -> List[int]:
        return [cid for cid in range(self.total_chunks) if cid not in self.received_chunk_ids]

    def source_stream_complete(self) -> bool:
        return not self.missing_source_chunks()


def retry_delay_seconds(attempt_count: int, delays: List[int]) -> int:
    """Return a bounded retry delay for a one-based attempt number."""
    normalized = [max(1, int(value)) for value in delays if int(value) > 0]
    if not normalized:
        normalized = [30, 300, 1800]
    index = max(0, min(len(normalized) - 1, int(attempt_count) - 1))
    return normalized[index]
