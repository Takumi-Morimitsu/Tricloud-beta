# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from admin_controls import UserControls, classify_restriction  # noqa: E402


class AdminControlsTests(unittest.TestCase):
    def test_suspension_blocks_every_authenticated_path(self) -> None:
        controls = UserControls(suspended=True)
        self.assertEqual(classify_restriction(controls, "/items"), "account_suspended")
        self.assertEqual(classify_restriction(controls, "/auth/login"), "account_suspended")

    def test_scoped_restrictions_only_block_matching_routes(self) -> None:
        controls = UserControls(sharing_disabled=True, downloads_disabled=True)
        self.assertEqual(classify_restriction(controls, "/share/send_by_email"), "sharing_disabled")
        self.assertEqual(classify_restriction(controls, "/library/items/a/download_token"), "downloads_disabled")
        self.assertEqual(classify_restriction(controls, "/s/shared/download_token"), "sharing_disabled")
        self.assertIsNone(classify_restriction(controls, "/items"))

    def test_abuse_flag_is_review_metadata_not_an_automatic_suspension(self) -> None:
        self.assertIsNone(classify_restriction(UserControls(abuse_flag=True), "/items"))


if __name__ == "__main__":
    unittest.main()
