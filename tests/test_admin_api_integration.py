# -*- coding: utf-8 -*-
"""HTTP-level integration tests for the separate admin API.

They use dependency/service overrides and therefore never require a live DB.
Install backend/requirements-dev.txt to run them; the source-only bundle's
minimal verification environment skips this module when those packages are
not available.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

HTTP_STACK_AVAILABLE = all(
    importlib.util.find_spec(name) is not None
    for name in ("fastapi", "httpx", "psycopg", "argon2")
)
if HTTP_STACK_AVAILABLE:
    from fastapi import Request as FastAPIRequest
else:  # pragma: no cover - used only so discovery can report a clean skip
    FastAPIRequest = object  # type: ignore[misc,assignment]


@unittest.skipUnless(HTTP_STACK_AVAILABLE, "install backend/requirements-dev.txt")
class AdminApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("TRICLOUD_ENV", "development")
        from fastapi.testclient import TestClient

        # Discovery imports isolated unit-test modules first.  Some of those
        # modules install collaborator stubs at module scope; clear them here
        # so this HTTP integration test always exercises the real admin stack.
        for module_name in (
            "admin_api",
            "admin_auth",
            "admin_service",
            "auth_util",
            "meta_db_pg",
            "replica_health_service",
            "replica_repair_service",
            "object_gc",
            "usage_metering",
            "node_heartbeat_stats_patch",
            "crypto_common_keywrap",
            "psycopg",
            "psycopg.rows",
            "zmq",
        ):
            sys.modules.pop(module_name, None)
        import admin_api
        from admin_auth import AdminPrincipal

        cls.module = admin_api
        cls.principal = AdminPrincipal(
            user_id="admin-1",
            session_id="session-1",
            expires_at=4_000_000_000,
        )
        cls.TestClient = TestClient

    def tearDown(self) -> None:
        self.module.app.dependency_overrides.clear()

    def test_dashboard_rejects_missing_admin_session(self) -> None:
        with patch.object(self.module, "record_admin_audit"):
            with self.TestClient(self.module.app) as client:
                response = client.get("/admin/v1/dashboard")
        self.assertEqual(response.status_code, 401)

    def test_dashboard_uses_separate_admin_dependency(self) -> None:
        def admin_dependency(request: FastAPIRequest):
            request.state.admin_user_id = self.principal.user_id
            return self.principal

        self.module.app.dependency_overrides[self.module.require_admin] = admin_dependency
        with (
            patch.object(self.module, "dashboard_summary", return_value={"integrity": {"bad_replicas": 0}}),
            patch.object(self.module, "record_admin_audit") as audit,
            self.TestClient(self.module.app) as client,
        ):
            response = client.get("/admin/v1/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["integrity"]["bad_replicas"], 0)
        audit.assert_called_once()
        self.assertEqual(audit.call_args.kwargs["action"], "admin_api.request")

    def test_dangerous_action_requires_exact_confirmation_and_reauth(self) -> None:
        self.module.app.dependency_overrides[self.module.require_admin] = lambda: self.principal
        endpoint = "/admin/v1/integrity/objects/object-1/audits"
        with (
            patch.object(self.module, "verify_admin_password", return_value=True),
            patch.object(self.module, "force_audits", return_value=["audit-1"]) as force,
            patch.object(self.module, "record_admin_audit"),
            self.TestClient(self.module.app) as client,
        ):
            denied = client.post(
                endpoint,
                json={"admin_password": "password", "confirmation": "wrong", "limit": 1},
            )
            accepted = client.post(
                endpoint,
                json={"admin_password": "password", "confirmation": "QUEUE AUDIT", "limit": 1},
            )
        self.assertEqual(denied.status_code, 400)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["created_audit_job_ids"], ["audit-1"])
        force.assert_called_once()


if __name__ == "__main__":
    unittest.main()
