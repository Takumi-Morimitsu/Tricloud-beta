# -*- coding: utf-8 -*-
from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"


def _literal_assignment(name: str):
    tree = ast.parse((BACKEND_DIR / "admin_schema.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"assignment not found: {name}")


class Phase2AdminStaticTests(unittest.TestCase):
    def test_schema_contains_security_and_control_tables(self) -> None:
        phase2_admin_ddl = _literal_assignment("PHASE2_ADMIN_DDL")
        required_columns = _literal_assignment("REQUIRED_COLUMNS")
        ddl = "\n".join(phase2_admin_ddl).lower()
        for table in (
            "admin_sessions",
            "admin_audit_logs",
            "admin_user_controls",
            "admin_node_controls",
            "admin_release_registry",
        ):
            self.assertIn(table, ddl)
            self.assertIn(table, required_columns)
        self.assertNotIn("reliability_score", ddl)

    def test_placement_pause_is_enforced_by_both_placement_paths(self) -> None:
        server = (BACKEND_DIR / "server.py").read_text(encoding="utf-8")
        repairs = (BACKEND_DIR / "replica_repair_service.py").read_text(encoding="utf-8")
        self.assertIn("COALESCE(n.placement_paused,FALSE)=FALSE", server)
        self.assertGreaterEqual(repairs.count("COALESCE(n.placement_paused,FALSE)=FALSE"), 2)

    def test_payout_requires_admin_approval_and_honors_node_pause(self) -> None:
        source = (BACKEND_DIR / "control_api_integrated_improved.py").read_text(encoding="utf-8")
        self.assertIn('earning_status != "approved"', source)
        self.assertIn('profile["payouts_paused"]', source)
        self.assertNotIn('earning_status not in {"approved", "calculated"}', source)

    def test_admin_api_is_separate_from_customer_api(self) -> None:
        source = (BACKEND_DIR / "admin_api.py").read_text(encoding="utf-8")
        self.assertIn('title="Tricloud Administration API"', source)
        self.assertIn('/admin/v1/audit-logs', source)
        self.assertNotIn("control_api_integrated_improved", source)


if __name__ == "__main__":
    unittest.main()
