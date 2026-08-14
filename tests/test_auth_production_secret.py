# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import subprocess
import sys
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")


class ProductionSecretTests(unittest.TestCase):
    def _import_auth(self, jwt_secret: str | None) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["TRICLOUD_ENV"] = "production"
        environment["PYTHONPATH"] = BACKEND_DIR
        if jwt_secret is None:
            environment.pop("JWT_SECRET", None)
        else:
            environment["JWT_SECRET"] = jwt_secret
        return subprocess.run(
            [sys.executable, "-c", "import auth_util"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_missing_secret_fails_closed_in_production(self) -> None:
        result = self._import_auth(None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-default", result.stderr)

    def test_explicit_non_default_secret_is_accepted(self) -> None:
        result = self._import_auth("unit-test-secret-that-is-not-used-outside-this-process")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_short_secret_is_rejected_in_production(self) -> None:
        result = self._import_auth("too-short")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("at least 32", result.stderr)


if __name__ == "__main__":
    unittest.main()
