# -*- coding: utf-8 -*-
"""Run the destructive, localhost-only post-rollback repair E2E.

The target is the disposable 143-byte file created by the previous rollback
test.  The script never selects another object, never prints credentials, and
starts/stops only child processes whose handles it owns.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
PYTHON = ROOT / ".venv-test" / "Scripts" / "python.exe"
NODE_STORE = ROOT / "node_store"
SOURCE_FILE = ROOT / "tmp" / "phase2-postrollback-upload.txt"
TARGET_NAME = "phase2-postrollback-upload.txt"
TARGET_SIZE = 143
LOCAL_NODES = [f"local-node-{index}" for index in range(1, 5)]
ACTIVE_REPAIR = (
    "queued",
    "selecting_source",
    "selecting_target",
    "copying",
    "verifying",
    "retry_wait",
)


def _load_local_env() -> None:
    env_path = ROOT / ".env"
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if name:
            os.environ[name] = value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _wait_until(description: str, predicate, *, timeout: float = 60.0, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    last_value = None
    while time.monotonic() < deadline:
        last_value = predicate()
        if last_value:
            return last_value
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {description}; last={last_value!r}")


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.2):
            return True
    except OSError:
        return False


def _start_process(command: list[str], *, env: dict[str, str], log_path: Path):
    log_handle = log_path.open("ab", buffering=0)
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creation_flags,
    )
    return process, log_handle


def _stop_owned_processes(processes: list[subprocess.Popen]) -> None:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 10.0
    for process in reversed(processes):
        if process.poll() is not None:
            continue
        timeout = max(0.1, deadline - time.monotonic())
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main() -> None:
    _load_local_env()
    os.environ["TRICLOUD_ENV"] = "development"
    os.environ["CLIENT_ENDPOINT"] = "tcp://127.0.0.1:8888"
    os.environ["NODE_ENDPOINT"] = "tcp://127.0.0.1:9999"
    os.environ["PHASE2_ADMIN_CONTROLS_ENABLED"] = "0"
    os.environ["STORAGE_AUDIT_ENABLED"] = "1"
    os.environ["REPLICA_REPAIR_QUEUE_ENABLED"] = "0"
    os.environ["REPLICA_REPAIR_EXECUTION_ENABLED"] = "1"
    os.environ["AUDIT_TARGET_AGE_SEC"] = str(10 * 365 * 24 * 3600)
    os.environ["AUDIT_SCHEDULE_INTERVAL_SEC"] = "0.5"
    os.environ["AUDIT_SCHEDULE_BATCH"] = "8"
    os.environ["AUDIT_RETRY_DELAYS_SEC"] = "1,1,1"
    os.environ["AUDIT_TIMEOUT_SEC"] = "5"
    os.environ["REPAIR_POLL_INTERVAL_SEC"] = "0.5"
    os.environ["REPAIR_STEP_TIMEOUT_SEC"] = "10"
    os.environ["STRIPE_SECRET_KEY"] = ""

    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    from psycopg.rows import dict_row

    from admin_service import create_manual_repair, force_audits

    database_url = os.environ.get("DATABASE_URL", "").strip()
    info = conninfo_to_dict(database_url)
    if str(info.get("host") or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("E2E requires a loopback PostgreSQL host")
    if "test" not in str(info.get("dbname") or "").lower():
        raise RuntimeError("E2E requires a dedicated database whose name contains 'test'")
    if not PYTHON.is_file() or not SOURCE_FILE.is_file():
        raise RuntimeError("test Python and the dedicated 143-byte source file are required")
    if SOURCE_FILE.stat().st_size != TARGET_SIZE:
        raise RuntimeError("the dedicated rollback source file has an unexpected size")
    for port in (8888, 9999):
        if _port_open(port):
            raise RuntimeError(f"localhost port {port} is already in use")

    run_id = f"postrollback-rerepair-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    logs_dir = ROOT / "logs" / run_id
    evidence_dir = ROOT / "tmp" / run_id
    logs_dir.mkdir(parents=True, exist_ok=False)
    evidence_dir.mkdir(parents=True, exist_ok=False)
    processes: list[subprocess.Popen] = []
    log_handles = []
    repair_completed = False
    victim_chunk: Path | None = None
    backup_chunk: Path | None = None

    try:
        with psycopg.connect(database_url, row_factory=dict_row) as conn:
            location = conn.execute(
                "SELECT current_database() AS database, inet_server_addr()::text AS address"
            ).fetchone()
            if not location or "test" not in str(location["database"]).lower():
                raise RuntimeError("connected database is not the dedicated test database")

            candidates = conn.execute(
                """
                SELECT i.item_id,i.file_object_id,i.owner_user_id,i.name,i.size_bytes,o.chunk_size
                FROM items i
                JOIN objects o ON o.file_object_id=i.file_object_id
                WHERE i.name=%s AND i.size_bytes=%s AND i.trashed_at IS NULL
                ORDER BY i.created_at DESC
                """,
                (TARGET_NAME, TARGET_SIZE),
            ).fetchall()
            if len(candidates) != 1:
                raise RuntimeError(f"expected exactly one dedicated rollback object, found {len(candidates)}")
            target = candidates[0]
            object_id = str(target["file_object_id"])
            owner_user_id = str(target["owner_user_id"])
            admin_row = conn.execute(
                "SELECT user_id FROM user_roles WHERE role='admin' ORDER BY created_at LIMIT 1"
            ).fetchone()
            admin_user_id = str(admin_row["user_id"] if admin_row else owner_user_id)

            replicas = conn.execute(
                """
                SELECT r.node_id,h.status
                FROM replicas r
                JOIN replica_health h
                  ON h.file_object_id=r.file_object_id AND h.node_id=r.node_id
                WHERE r.file_object_id=%s
                ORDER BY r.node_id
                """,
                (object_id,),
            ).fetchall()
            if len(replicas) != 3 or {str(row["status"]) for row in replicas} != {"healthy"}:
                raise RuntimeError("dedicated rollback object must start at 3 replicas / 3 healthy")
            replica_nodes = [str(row["node_id"]) for row in replicas]
            if not set(replica_nodes).issubset(LOCAL_NODES):
                raise RuntimeError("dedicated object is not confined to the four local test nodes")
            spare_nodes = sorted(set(LOCAL_NODES) - set(replica_nodes))
            if len(spare_nodes) != 1:
                raise RuntimeError("exactly one local spare node is required")
            active_audits = conn.execute(
                "SELECT COUNT(*) AS count FROM audit_jobs WHERE file_object_id=%s "
                "AND status IN ('queued','sent','retry_wait')",
                (object_id,),
            ).fetchone()["count"]
            active_repairs = conn.execute(
                "SELECT COUNT(*) AS count FROM repair_jobs WHERE file_object_id=%s "
                "AND status=ANY(%s::text[])",
                (object_id, list(ACTIVE_REPAIR)),
            ).fetchone()["count"]
            if active_audits or active_repairs:
                raise RuntimeError("dedicated object has pre-existing active audit/repair work")
            controls = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM admin_user_controls
                   WHERE suspended OR sharing_disabled OR downloads_disabled) AS user_blocks,
                  (SELECT COUNT(*) FROM nodes WHERE placement_paused) AS placement_blocks,
                  (SELECT COUNT(*) FROM node_profiles WHERE payouts_paused) AS payout_blocks
                """
            ).fetchone()
            if any(int(controls[key] or 0) for key in controls):
                raise RuntimeError("rollback controls are not fully disabled")
            node_rows = conn.execute(
                """
                SELECT n.node_id,n.capacity_bytes,n.failure_domain,n.placement_paused,
                       np.node_api_key
                FROM nodes n
                JOIN node_profiles np ON np.node_id=n.node_id
                WHERE n.node_id=ANY(%s::text[])
                ORDER BY n.node_id
                """,
                (LOCAL_NODES,),
            ).fetchall()
            if len(node_rows) != 4 or any(not row["node_api_key"] for row in node_rows):
                raise RuntimeError("all four local node profiles and API keys are required")
            audit_slice = conn.execute(
                """
                SELECT chunk_id,byte_offset,length
                FROM chunk_audit_slices
                WHERE file_object_id=%s
                ORDER BY chunk_id,slice_index LIMIT 1
                """,
                (object_id,),
            ).fetchone()
            if not audit_slice or int(audit_slice["length"]) <= 0:
                raise RuntimeError("dedicated object has no usable audit slice")
            baseline_repair_count = conn.execute(
                "SELECT COUNT(*) AS count FROM repair_jobs WHERE file_object_id=%s",
                (object_id,),
            ).fetchone()["count"]

        chunk_id = int(audit_slice["chunk_id"])
        chunk_offset = int(audit_slice["byte_offset"])
        chunk_paths = {
            node_id: NODE_STORE / node_id / "objects" / object_id / "chunks" / f"{chunk_id}.bin"
            for node_id in replica_nodes
        }
        if any(not path.is_file() for path in chunk_paths.values()):
            raise RuntimeError("one or more dedicated replica chunk files are missing")
        original_hashes = {_sha256(path) for path in chunk_paths.values()}
        if len(original_hashes) != 1:
            raise RuntimeError("dedicated object replicas are not identical before corruption")

        victim_node = replica_nodes[0]
        victim_chunk = chunk_paths[victim_node]
        backup_chunk = evidence_dir / f"{victim_node}-chunk-{chunk_id}.bin"
        shutil.copy2(victim_chunk, backup_chunk)
        payload = bytearray(victim_chunk.read_bytes())
        if chunk_offset < 0 or chunk_offset >= len(payload):
            raise RuntimeError("audit slice offset falls outside the stored chunk")
        payload[chunk_offset] ^= 0x01
        victim_chunk.write_bytes(payload)
        if _sha256(victim_chunk) == _sha256(backup_chunk):
            raise RuntimeError("ciphertext corruption was not applied")

        service_env = dict(os.environ)
        service_env["PYTHONUNBUFFERED"] = "1"
        data_process, data_log = _start_process(
            [str(PYTHON), "-u", str(BACKEND / "server.py")],
            env=service_env,
            log_path=logs_dir / "dataserver.log",
        )
        processes.append(data_process)
        log_handles.append(data_log)
        _wait_until(
            "DataServer ports",
            lambda: _port_open(8888) and _port_open(9999),
            timeout=20,
        )

        start_epoch = int(time.time())
        for node_row in node_rows:
            node_id = str(node_row["node_id"])
            node_env = dict(service_env)
            node_env["TRICLOUD_NODE_API_KEY"] = str(node_row["node_api_key"])
            capacity_gb = max(1, int(node_row["capacity_bytes"] or 0) // (1024**3))
            command = [
                str(PYTHON),
                "-u",
                str(BACKEND / "node.py"),
                "--node-id",
                node_id,
                "--server",
                "tcp://127.0.0.1:9999",
                "--storage-dir",
                str(NODE_STORE),
                "--capacity-gb",
                str(capacity_gb),
            ]
            if node_row.get("failure_domain"):
                command.extend(["--failure-domain", str(node_row["failure_domain"])])
            process, log_handle = _start_process(
                command,
                env=node_env,
                log_path=logs_dir / f"{node_id}.log",
            )
            processes.append(process)
            log_handles.append(log_handle)

        def all_nodes_online():
            if any(process.poll() is not None for process in processes):
                raise RuntimeError("a local E2E process exited unexpectedly")
            with psycopg.connect(database_url, row_factory=dict_row) as conn:
                rows = conn.execute(
                    "SELECT node_id,last_seen FROM nodes WHERE node_id=ANY(%s::text[])",
                    (LOCAL_NODES,),
                ).fetchall()
            return len(rows) == 4 and all(int(row["last_seen"] or 0) >= start_epoch for row in rows)

        _wait_until("four local node heartbeats", all_nodes_online, timeout=30)

        spare_node = spare_nodes[0]
        spare_object_dir = NODE_STORE / spare_node / "objects" / object_id

        def spare_target_is_clean():
            with psycopg.connect(database_url, row_factory=dict_row) as conn:
                pending = conn.execute(
                    "SELECT COUNT(*) AS count FROM object_gc_queue "
                    "WHERE file_object_id=%s AND node_id=%s AND status<>'done'",
                    (object_id, spare_node),
                ).fetchone()["count"]
            return int(pending or 0) == 0 and not spare_object_dir.exists()

        _wait_until("clean spare repair target", spare_target_is_clean, timeout=30)

        audit_context = {
            "ip_address": "127.0.0.1",
            "user_agent": "postrollback-repair-e2e",
            "request_id": run_id,
        }
        created_audits = force_audits(
            admin_user_id=admin_user_id,
            file_object_id=object_id,
            node_id=None,
            limit=3,
            audit_context=audit_context,
        )
        if len(created_audits) != 3:
            raise RuntimeError(f"expected 3 forced audits, created {len(created_audits)}")

        def corruption_detected():
            with psycopg.connect(database_url, row_factory=dict_row) as conn:
                rows = conn.execute(
                    "SELECT audit_job_id,node_id,status,attempt_count FROM audit_jobs "
                    "WHERE audit_job_id=ANY(%s::text[])",
                    (created_audits,),
                ).fetchall()
                health = conn.execute(
                    "SELECT status FROM replica_health WHERE file_object_id=%s AND node_id=%s",
                    (object_id, victim_node),
                ).fetchone()
            if len(rows) != 3:
                return False
            terminal = all(str(row["status"]) in {"completed", "failed"} for row in rows)
            victim_row = next((row for row in rows if str(row["node_id"]) == victim_node), None)
            return bool(
                terminal
                and victim_row
                and str(victim_row["status"]) == "failed"
                and int(victim_row["attempt_count"] or 0) >= 2
                and health
                and str(health["status"]) == "corrupt"
            )

        _wait_until("audit corruption detection", corruption_detected, timeout=60)
        with psycopg.connect(database_url, row_factory=dict_row) as conn:
            current_repair_count = conn.execute(
                "SELECT COUNT(*) AS count FROM repair_jobs WHERE file_object_id=%s",
                (object_id,),
            ).fetchone()["count"]
        if current_repair_count != baseline_repair_count:
            raise RuntimeError("queue-disabled audit unexpectedly created a repair job")

        repair_job_id = create_manual_repair(
            admin_user_id=admin_user_id,
            file_object_id=object_id,
            reason="postrollback corruption recovery",
            audit_context=audit_context,
        )
        if not repair_job_id:
            raise RuntimeError("manual repair job was not created")

        def repair_finished():
            with psycopg.connect(database_url, row_factory=dict_row) as conn:
                row = conn.execute(
                    "SELECT status,last_error FROM repair_jobs WHERE repair_job_id=%s",
                    (repair_job_id,),
                ).fetchone()
            if row and str(row["status"]) in {"failed", "canceled"}:
                raise RuntimeError(f"repair ended unexpectedly: {row['status']} / {row['last_error']}")
            return row if row and str(row["status"]) == "completed" else False

        _wait_until("repair verification and completion", repair_finished, timeout=90)
        repair_completed = True

        with psycopg.connect(database_url, row_factory=dict_row) as conn:
            final_replicas = conn.execute(
                """
                SELECT r.node_id,h.status
                FROM replicas r
                JOIN replica_health h
                  ON h.file_object_id=r.file_object_id AND h.node_id=r.node_id
                WHERE r.file_object_id=%s ORDER BY r.node_id
                """,
                (object_id,),
            ).fetchall()
            victim_health = conn.execute(
                "SELECT status FROM replica_health WHERE file_object_id=%s AND node_id=%s",
                (object_id, victim_node),
            ).fetchone()
            events = conn.execute(
                "SELECT status,event FROM repair_job_events WHERE repair_job_id=%s ORDER BY id",
                (repair_job_id,),
            ).fetchall()
            active_after = conn.execute(
                "SELECT COUNT(*) AS count FROM repair_jobs WHERE file_object_id=%s "
                "AND status=ANY(%s::text[])",
                (object_id, list(ACTIVE_REPAIR)),
            ).fetchone()["count"]
        if len(final_replicas) != 3 or {str(row["status"]) for row in final_replicas} != {"healthy"}:
            raise RuntimeError("repair did not finish at 3 replicas / 3 healthy")
        if active_after:
            raise RuntimeError("repair left an active job")
        if not victim_health or str(victim_health["status"]) not in {"corrupt", "deleted"}:
            raise RuntimeError("old corrupt replica has an unexpected final health state")
        event_statuses = {str(row["status"]) for row in events}
        required_statuses = {"queued", "selecting_source", "selecting_target", "copying", "verifying", "completed"}
        if not required_statuses.issubset(event_statuses):
            raise RuntimeError(f"repair event sequence is incomplete: {sorted(event_statuses)}")

        final_nodes = [str(row["node_id"]) for row in final_replicas]
        final_chunk_paths = [
            NODE_STORE / node_id / "objects" / object_id / "chunks" / f"{chunk_id}.bin"
            for node_id in final_nodes
        ]
        _wait_until(
            "three final ciphertext chunks",
            lambda: all(path.is_file() for path in final_chunk_paths),
            timeout=15,
        )
        final_hashes = {_sha256(path) for path in final_chunk_paths}
        if len(final_hashes) != 1 or final_hashes != original_hashes:
            raise RuntimeError("repaired ciphertext does not match the original healthy replicas")

        victim_object_dir = NODE_STORE / victim_node / "objects" / object_id

        def retired_replica_gc_done():
            with psycopg.connect(database_url, row_factory=dict_row) as conn:
                row = conn.execute(
                    "SELECT status FROM object_gc_queue WHERE file_object_id=%s AND node_id=%s",
                    (object_id, victim_node),
                ).fetchone()
            return bool(row and str(row["status"]) == "done" and not victim_object_dir.exists())

        _wait_until("retired replica physical GC", retired_replica_gc_done, timeout=30)

        summary = {
            "result": "PASS",
            "run_id": run_id,
            "file_object_id": object_id,
            "victim_node_id": victim_node,
            "replacement_node_id": next(node for node in final_nodes if node not in replica_nodes),
            "forced_audits": 3,
            "repair_job_id": str(repair_job_id),
            "repair_statuses": [str(row["status"]) for row in events],
            "final_replica_count": len(final_replicas),
            "final_healthy_count": sum(1 for row in final_replicas if str(row["status"]) == "healthy"),
            "old_replica_status": str(victim_health["status"]),
            "ciphertext_hash_match": True,
            "retired_physical_copy_deleted": True,
            "backup_path": str(backup_chunk),
            "logs_path": str(logs_dir),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except Exception:
        _stop_owned_processes(processes)
        processes.clear()
        if not repair_completed and victim_chunk and backup_chunk and backup_chunk.is_file():
            # Restore only the exact disposable chunk.  DB repair state is not
            # rewritten here; the caller can inspect logs/history safely.
            shutil.copy2(backup_chunk, victim_chunk)
        raise
    finally:
        _stop_owned_processes(processes)
        for handle in log_handles:
            handle.close()


if __name__ == "__main__":
    main()
