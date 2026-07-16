# -*- coding: utf-8 -*-
"""
ローカル3ノードテスト用の node_profiles を作成・補正するスクリプト。

この版では node_profiles.country_code は使わない。
Tricloud の現在方針どおり、ノードの国は「ノード所有者 users.country_code」で判定する。

使い方 PowerShell:
  $env:DATABASE_URL="postgresql://<USER>:<PASSWORD>@127.0.0.1:5432/<DB_NAME>"
  $env:TEST_USER_ID="JPノード提供者ユーザーの user_id"
  $env:TEST_COUNTRY_CODE="JP"
  python create_or_fix_local_test_nodes.py

既存の local-test-node-1/2/3 がある場合、node_api_key は維持する。
未作成の場合だけ新規発行して表示する。
"""
from __future__ import annotations

import os
import secrets
import time
from typing import Optional

from psycopg.rows import dict_row
from meta_db_pg import db_conn

NODE_IDS = ["local-test-node-1", "local-test-node-2", "local-test-node-3"]
USER_ID = os.environ.get("TEST_USER_ID", "").strip()
CAPACITY_GB = int(os.environ.get("TEST_NODE_CAPACITY_GB", "50"))
TEST_COUNTRY_CODE = os.environ.get("TEST_COUNTRY_CODE", "JP").strip().upper() or "JP"


def _require_user_and_set_country(cur, user_id: str) -> dict:
    cur.execute("SELECT user_id, email, country_code FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(
            f"users に TEST_USER_ID={user_id!r} が見つかりません。"
            "JPノード提供者ユーザーの user_id を指定してください。"
        )

    current_country = str(row.get("country_code") or "").strip().upper()
    if current_country != TEST_COUNTRY_CODE:
        cur.execute(
            "UPDATE users SET country_code=%s WHERE user_id=%s RETURNING user_id, email, country_code",
            (TEST_COUNTRY_CODE, user_id),
        )
        row = cur.fetchone()
    return row


def _existing_key(cur, node_id: str) -> Optional[str]:
    cur.execute("SELECT node_api_key FROM node_profiles WHERE node_id=%s", (node_id,))
    row = cur.fetchone()
    if not row:
        return None
    key = row.get("node_api_key")
    return str(key) if key else None


def main() -> None:
    if not USER_ID:
        raise SystemExit(
            "TEST_USER_ID を指定してください。例: "
            '$env:TEST_USER_ID="JPノード提供者ユーザーのuser_id"'
        )

    now = int(time.time())
    capacity_bytes = CAPACITY_GB * 1024 * 1024 * 1024

    with db_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            owner = _require_user_and_set_country(cur, USER_ID)
            print(f"owner_user_id: {owner['user_id']}")
            print(f"owner_email: {owner.get('email')}")
            print(f"owner_country_code: {owner.get('country_code')}")
            print(f"capacity_gb: {CAPACITY_GB}")
            print("")

            for i, node_id in enumerate(NODE_IDS, start=1):
                node_api_key = _existing_key(cur, node_id) or secrets.token_urlsafe(32)
                cur.execute(
                    """
                    INSERT INTO node_profiles (
                        node_id,
                        owner_user_id,
                        node_name,
                        desired_capacity_bytes,
                        node_api_key,
                        payout_enabled,
                        payouts_paused,
                        created_at,
                        updated_at
                    )
                    VALUES (%s,%s,%s,%s,%s,FALSE,FALSE,%s,%s)
                    ON CONFLICT (node_id) DO UPDATE SET
                        owner_user_id = EXCLUDED.owner_user_id,
                        node_name = EXCLUDED.node_name,
                        desired_capacity_bytes = EXCLUDED.desired_capacity_bytes,
                        node_api_key = COALESCE(node_profiles.node_api_key, EXCLUDED.node_api_key),
                        updated_at = EXCLUDED.updated_at
                    RETURNING node_id, node_api_key
                    """,
                    (
                        node_id,
                        USER_ID,
                        f"Local Test Node {i}",
                        capacity_bytes,
                        node_api_key,
                        now,
                        now,
                    ),
                )
                out = cur.fetchone()
                print(f"{out['node_id']} {out['node_api_key']}")
        conn.commit()

    print("\n上の3行の node_api_key を使って、local-test-node-1/2/3 を起動してください。")
    print("この版では node_profiles.country_code は使いません。ノード国は owner_user_id の users.country_code で判定されます。")


if __name__ == "__main__":
    main()
