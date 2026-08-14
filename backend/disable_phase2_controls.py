# -*- coding: utf-8 -*-
"""Emergency rollback helper for Phase 2 runtime controls.

The additive tables and audit history are deliberately retained.  This only
returns runtime restrictions to their permissive defaults and revokes admin
sessions before an application rollback.
"""

from __future__ import annotations

import argparse

from admin_service import write_admin_audit
from meta_db_pg import db_conn, now_ts


def main() -> int:
    parser = argparse.ArgumentParser(description="Disable all active Phase 2 runtime controls")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="required confirmation that all Phase 2 user/node controls should be cleared",
    )
    args = parser.parse_args()
    if not args.yes:
        parser.error("pass --yes only after reviewing the rollback runbook")

    timestamp = int(now_ts())
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM nodes WHERE COALESCE(placement_paused,FALSE)) AS paused_nodes,
                  (SELECT COUNT(*) FROM node_profiles WHERE COALESCE(payouts_paused,FALSE)) AS paused_payouts,
                  (SELECT COUNT(*) FROM admin_user_controls
                   WHERE suspended OR sharing_disabled OR downloads_disabled) AS restricted_users
                """
            )
            row = cur.fetchone()
            before = {
                "paused_nodes": int(row[0] or 0),
                "paused_payouts": int(row[1] or 0),
                "restricted_users": int(row[2] or 0),
            }
            cur.execute("UPDATE nodes SET placement_paused=FALSE WHERE placement_paused")
            cur.execute("UPDATE node_profiles SET payouts_paused=FALSE WHERE payouts_paused")
            cur.execute(
                """
                UPDATE admin_node_controls
                SET placement_paused=FALSE,payouts_paused=FALSE,
                    reason='phase2 rollback',updated_by='system:phase2-rollback',updated_at=%s
                WHERE placement_paused OR payouts_paused
                """,
                (timestamp,),
            )
            cur.execute(
                """
                UPDATE admin_user_controls
                SET suspended=FALSE,sharing_disabled=FALSE,downloads_disabled=FALSE,
                    reason='phase2 rollback',updated_by='system:phase2-rollback',updated_at=%s
                WHERE suspended OR sharing_disabled OR downloads_disabled
                """,
                (timestamp,),
            )
            cur.execute(
                "UPDATE admin_sessions SET revoked_at=%s WHERE revoked_at IS NULL",
                (timestamp,),
            )
            write_admin_audit(
                cur,
                admin_user_id="system:phase2-rollback",
                action="phase2.controls.disable_all",
                target_type="system",
                target_id="phase2-admin-system",
                before=before,
                after={"controls_enabled": False, "admin_sessions_revoked": True},
            )
        conn.commit()

    print(f"Phase 2 runtime controls cleared: {before}")
    print("Keep PHASE2_ADMIN_CONTROLS_ENABLED=0 while rolling back services.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
