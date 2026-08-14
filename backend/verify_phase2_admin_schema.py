# -*- coding: utf-8 -*-
"""Read-only Phase 2 schema verification."""

from __future__ import annotations

import json

from admin_schema import inspect_phase2_schema
from meta_db_pg import db_conn


def main() -> int:
    with db_conn() as conn:
        with conn.cursor() as cur:
            result = inspect_phase2_schema(cur)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if bool(result.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
