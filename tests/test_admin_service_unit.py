# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import admin_service


class RepairAdmissionCursor:
    def __init__(self, healthy_count: int) -> None:
        self.healthy_count = int(healthy_count)
        self._one = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params=None) -> None:
        text = " ".join(str(query).split())
        if text.startswith("SELECT file_object_id FROM objects"):
            self._one = {"file_object_id": "object-1"}
        elif text.startswith("SELECT COUNT(*) FILTER"):
            self._one = {"healthy_count": self.healthy_count}
        else:
            self._one = None

    def fetchone(self):
        value = self._one
        self._one = None
        return value


class RepairAdmissionConnection:
    def __init__(self, healthy_count: int) -> None:
        self.cur = RepairAdmissionCursor(healthy_count)
        self.commit = MagicMock()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self, **kwargs):
        return self.cur


class AdminServiceRepairGuardTests(unittest.TestCase):
    def test_manual_repair_respects_target_healthy_boundary(self) -> None:
        for healthy_count in (2, 3, 4):
            with self.subTest(healthy_count=healthy_count):
                conn = RepairAdmissionConnection(healthy_count)
                enqueue = MagicMock(return_value="repair-1")
                audit = MagicMock()
                with (
                    patch.object(admin_service, "TARGET_REPLICA_COUNT", 3),
                    patch.object(admin_service, "db_conn", return_value=conn),
                    patch.object(admin_service, "enqueue_repair_job", enqueue),
                    patch.object(admin_service, "write_admin_audit", audit),
                ):
                    if healthy_count < 3:
                        result = admin_service.create_manual_repair(
                            admin_user_id="admin-1",
                            file_object_id="object-1",
                            reason="regression test",
                            audit_context={},
                        )
                        self.assertEqual(result, "repair-1")
                        enqueue.assert_called_once()
                        audit.assert_called_once()
                        conn.commit.assert_called_once_with()
                    else:
                        with self.assertRaisesRegex(
                            ValueError,
                            "object already has the target number of healthy replicas",
                        ):
                            admin_service.create_manual_repair(
                                admin_user_id="admin-1",
                                file_object_id="object-1",
                                reason="regression test",
                                audit_context={},
                            )
                        enqueue.assert_not_called()
                        audit.assert_not_called()
                        conn.commit.assert_not_called()
