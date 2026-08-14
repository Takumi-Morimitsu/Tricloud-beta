# -*- coding: utf-8 -*-
"""Grant the existing Tricloud user identified by email the admin DB role."""

from __future__ import annotations

import argparse

from psycopg.rows import dict_row

from admin_service import write_admin_audit
from meta_db_pg import db_conn, now_ts


def main() -> int:
    parser = argparse.ArgumentParser(description="Grant a Tricloud user the admin role")
    parser.add_argument("--email", required=True, help="existing user email")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="required confirmation that the selected account should become an administrator",
    )
    args = parser.parse_args()

    email = str(args.email or "").strip().lower()
    if not email:
        parser.error("--email must not be empty")
    if not args.yes:
        parser.error("review the email and pass --yes to perform the role grant")

    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT user_id,email FROM users WHERE lower(email)=lower(%s)",
                (email,),
            )
            user = cur.fetchone()
            if not user:
                print("No existing user matched the supplied email; nothing changed.")
                return 2
            cur.execute(
                """
                INSERT INTO user_roles(user_id,role,created_at)
                VALUES (%s,'admin',%s)
                ON CONFLICT (user_id,role) DO NOTHING
                """,
                (str(user["user_id"]), int(now_ts())),
            )
            created = cur.rowcount > 0
            write_admin_audit(
                cur,
                admin_user_id="system:cli",
                action="admin.role.grant",
                target_type="user",
                target_id=str(user["user_id"]),
                before={"admin_role": not created},
                after={"admin_role": True, "created": created},
            )
        conn.commit()

    state = "granted" if created else "already present"
    print(f"Admin role {state} for {user['email']} ({user['user_id']}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
