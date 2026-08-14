# -*- coding: utf-8 -*-
"""Runtime enforcement for Phase 2 user controls in the customer API."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

PHASE2_ADMIN_CONTROLS_ENABLED = (
    os.environ.get("PHASE2_ADMIN_CONTROLS_ENABLED", "0").strip() == "1"
)


class AdminControlsUnavailable(RuntimeError):
    """Raised when enforcement is enabled but its authoritative DB state is unavailable."""


@dataclass(frozen=True)
class UserControls:
    suspended: bool = False
    abuse_flag: bool = False
    sharing_disabled: bool = False
    downloads_disabled: bool = False
    reason: Optional[str] = None


def classify_restriction(controls: UserControls, path: str) -> Optional[str]:
    """Return a stable denial code for the requested path, if any."""
    normalized = "/" + str(path or "").strip().lower().lstrip("/")
    if controls.suspended:
        return "account_suspended"

    sharing_path = (
        normalized.startswith("/share/")
        or normalized == "/share"
        or "/shared" in normalized
    )
    if controls.sharing_disabled and sharing_path:
        return "sharing_disabled"

    if controls.downloads_disabled and "download" in normalized:
        return "downloads_disabled"
    return None


def load_user_controls(user_id: str) -> UserControls:
    """Load controls. Missing rows mean no restrictions; DB failures are not ignored."""
    # Keep the pure path classifier independently testable without DB drivers.
    from psycopg.rows import dict_row

    from meta_db_pg import db_conn

    try:
        with db_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT suspended,abuse_flag,sharing_disabled,downloads_disabled,reason
                    FROM admin_user_controls WHERE user_id=%s
                    """,
                    (str(user_id),),
                )
                row = cur.fetchone()
    except Exception as exc:
        raise AdminControlsUnavailable("admin controls could not be verified") from exc

    if not row:
        return UserControls()
    return UserControls(
        suspended=bool(row["suspended"]),
        abuse_flag=bool(row["abuse_flag"]),
        sharing_disabled=bool(row["sharing_disabled"]),
        downloads_disabled=bool(row["downloads_disabled"]),
        reason=None if not row.get("reason") else str(row["reason"]),
    )


def restriction_for_request(user_id: str, path: str) -> Optional[str]:
    if not PHASE2_ADMIN_CONTROLS_ENABLED:
        return None
    return classify_restriction(load_user_controls(user_id), path)
