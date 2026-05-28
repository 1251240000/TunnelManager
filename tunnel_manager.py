from __future__ import annotations

import json
import os
import shlex
import signal
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


class TunnelManager:
    def __init__(self, db_path: Path, log_dir: Path) -> None:
        self.db_path = db_path
        self.log_dir = log_dir
        self.processes: dict[int, subprocess.Popen] = {}
        self.proc_lock = threading.RLock()
        self.monitor_started = False
        self.monitor_thread: threading.Thread | None = None

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tunnels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    remote_user TEXT NOT NULL,
                    remote_host TEXT NOT NULL,
                    ssh_port INTEGER NOT NULL,
                    remote_bind_ip TEXT NOT NULL,
                    remote_bind_port INTEGER NOT NULL,
                    target_host TEXT NOT NULL,
                    target_port INTEGER NOT NULL,
                    ssh_key_path TEXT NOT NULL,
                    extra_args TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tunnels_remote_bind
                ON tunnels(remote_host, ssh_port, remote_bind_ip, remote_bind_port)
                """
            )

    def bootstrap_from_env(self) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM tunnels").fetchone()
            if row["count"]:
                return

        json_spec = os.environ.get("BOOTSTRAP_TUNNELS", "").strip()
        if json_spec:
            records = json.loads(json_spec)
            if not isinstance(records, list):
                raise ValueError("BOOTSTRAP_TUNNELS must be a JSON array")
            for record in records:
                self.create_tunnel(self._normalize_record(record))
            return

        if os.environ.get("SSH_REMOTE_HOST") and os.environ.get("SSH_TUNNEL_PORT"):
            for record in self._legacy_env_records():
                self.create_tunnel(record)

    def _legacy_env_records(self) -> list[dict[str, Any]]:
        tunnel_ports = [p.strip() for p in os.environ.get("SSH_TUNNEL_PORT", "").split(",") if p.strip()]
        target_ports = [p.strip() for p in os.environ.get("SSH_TARGET_PORT", "").split(",") if p.strip()]
        if len(tunnel_ports) != len(target_ports):
            raise ValueError("SSH_TUNNEL_PORT and SSH_TARGET_PORT must have the same number of ports")

        records = []
        for index, (remote_bind_port, target_port) in enumerate(zip(tunnel_ports, target_ports), start=1):
            records.append(
                {
                    "name": os.environ.get("SSH_TUNNEL_NAME", f"legacy-{index}"),
                    "remote_user": os.environ.get("SSH_REMOTE_USER", "root"),
                    "remote_host": os.environ["SSH_REMOTE_HOST"],
                    "ssh_port": int(os.environ.get("SSH_REMOTE_PORT", "22")),
                    "remote_bind_ip": os.environ.get("SSH_BIND_IP", "0.0.0.0"),
                    "remote_bind_port": int(remote_bind_port),
                    "target_host": os.environ.get("SSH_TARGET_HOST", "127.0.0.1"),
                    "target_port": int(target_port),
                    "ssh_key_path": os.environ.get("SSH_KEY_PATH", "/id_rsa"),
                    "extra_args": os.environ.get("AUTOSSH_EXTRA_ARGS", ""),
                    "enabled": 1,
                }
            )
        return records

    def _normalize_record(self, record: dict[str, Any]) -> dict[str, Any]:
        data = dict(record)
        data.setdefault("remote_user", "root")
        data.setdefault("ssh_port", 22)
        data.setdefault("remote_bind_ip", "0.0.0.0")
        data.setdefault("ssh_key_path", "/id_rsa")
        data.setdefault("extra_args", "")
        data.setdefault("enabled", 1)
        for key in ("ssh_port", "remote_bind_port", "target_port"):
            data[key] = int(data[key])
        data["enabled"] = 1 if data["enabled"] else 0
        return data

    def list_tunnels(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM tunnels ORDER BY id DESC").fetchall()

    def get_tunnel(self, tunnel_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM tunnels WHERE id = ?", (tunnel_id,)).fetchone()

    def create_tunnel(self, data: dict[str, Any]) -> int:
        now = datetime.utcnow().isoformat()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO tunnels
                (name, remote_user, remote_host, ssh_port, remote_bind_ip, remote_bind_port,
                 target_host, target_port, ssh_key_path, extra_args, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["name"],
                    data["remote_user"],
                    data["remote_host"],
                    int(data["ssh_port"]),
                    data["remote_bind_ip"],
                    int(data["remote_bind_port"]),
                    data["target_host"],
                    int(data["target_port"]),
                    data["ssh_key_path"],
                    data.get("extra_args", ""),
                    int(data.get("enabled", 1)),
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def update_tunnel(self, tunnel_id: int, data: dict[str, Any]) -> None:
        now = datetime.utcnow().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE tunnels
                SET name = ?, remote_user = ?, remote_host = ?, ssh_port = ?,
                    remote_bind_ip = ?, remote_bind_port = ?, target_host = ?,
                    target_port = ?, ssh_key_path = ?, extra_args = ?,
                    enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    data["name"],
                    data["remote_user"],
                    data["remote_host"],
                    int(data["ssh_port"]),
                    data["remote_bind_ip"],
                    int(data["remote_bind_port"]),
                    data["target_host"],
                    int(data["target_port"]),
                    data["ssh_key_path"],
                    data.get("extra_args", ""),
                    int(data.get("enabled", 1)),
                    now,
                    tunnel_id,
                ),
            )

    def rewrite_default_key_paths(self) -> int:
        runtime_path = os.environ.get("SSH_KEY_RUNTIME_PATH") or os.environ.get("DEFAULT_SSH_KEY_PATH")
        if not runtime_path:
            return 0

        old_paths = [
            path
            for path in (
                "~/.ssh/id_rsa",
                "~/.ssh/id_ed25519",
                "/id_rsa",
                "/run/secrets/tunnel_id_rsa",
                "/host_ssh/id_rsa",
                "/host_ssh/id_ed25519",
                "/host_ssh/id_ecdsa",
                "/host_ssh/id_dsa",
                "/data/ssh/id_rsa",
            )
            if path != runtime_path
        ]
        if not old_paths:
            return 0

        placeholders = ", ".join("?" for _ in old_paths)
        params = [runtime_path, datetime.utcnow().isoformat(), *old_paths]
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE tunnels SET ssh_key_path = ?, updated_at = ? WHERE ssh_key_path IN ({placeholders})",
                params,
            )
            return int(cur.rowcount)

    def set_enabled(self, tunnel_id: int, enabled: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE tunnels SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, datetime.utcnow().isoformat(), tunnel_id),
            )

    def delete_tunnel(self, tunnel_id: int) -> None:
        self.stop_tunnel(tunnel_id)
        with self.connect() as conn:
            conn.execute("DELETE FROM tunnels WHERE id = ?", (tunnel_id,))

    def build_command(self, tunnel: sqlite3.Row) -> list[str]:
        remote = f"{tunnel['remote_user']}@{tunnel['remote_host']}"
        reverse = (
            f"{tunnel['remote_bind_ip']}:{tunnel['remote_bind_port']}:"
            f"{tunnel['target_host']}:{tunnel['target_port']}"
        )
        command = [
            os.environ.get("AUTOSSH_PATH", "autossh"),
            "-M",
            "0",
            "-N",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            os.environ.get("STRICT_HOST_KEY_CHECKING", "StrictHostKeyChecking=accept-new"),
            "-i",
            tunnel["ssh_key_path"],
            "-p",
            str(tunnel["ssh_port"]),
            "-R",
            reverse,
        ]
        extra_args = tunnel["extra_args"].strip()
        if extra_args:
            command.extend(shlex.split(extra_args))
        command.append(remote)
        return command

    def start_tunnel(self, tunnel_id: int) -> bool:
        tunnel = self.get_tunnel(tunnel_id)
        if tunnel is None:
            return False

        with self.proc_lock:
            existing = self.processes.get(tunnel_id)
            if existing and existing.poll() is None:
                return True

            command = self.build_command(tunnel)
            log_path = self.log_dir / f"tunnel-{tunnel_id}.log"
            log_file = log_path.open("ab", buffering=0)
            log_file.write(f"\n[{datetime.utcnow().isoformat()}Z] starting: {' '.join(command)}\n".encode())

            key_path = Path(tunnel["ssh_key_path"])
            if not key_path.is_file():
                log_file.write(
                    (
                        f"[{datetime.utcnow().isoformat()}Z] failed: SSH private key not found: {key_path}. "
                        "Mount ${HOME}/.ssh to /host_ssh, set SSH_KEY_NAME if needed, then recreate the container.\n"
                    ).encode()
                )
                log_file.close()
                return False
            try:
                key_path.parent.chmod(0o700)
                key_path.chmod(0o600)
            except OSError as exc:
                log_file.write(f"[{datetime.utcnow().isoformat()}Z] warning: could not chmod SSH key: {exc}\n".encode())

            env = os.environ.copy()
            env.setdefault("AUTOSSH_GATETIME", "0")
            env.setdefault("AUTOSSH_LOGLEVEL", "7")
            try:
                proc = subprocess.Popen(
                    command,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                )
            except Exception as exc:
                log_file.write(f"[{datetime.utcnow().isoformat()}Z] failed: {exc}\n".encode())
                log_file.close()
                return False
            log_file.close()
            self.processes[tunnel_id] = proc
            return True

    def stop_tunnel(self, tunnel_id: int) -> None:
        with self.proc_lock:
            proc = self.processes.pop(tunnel_id, None)
        if not proc or proc.poll() is not None:
            return

        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=3)
        except ProcessLookupError:
            pass

    def restart_tunnel(self, tunnel_id: int) -> bool:
        self.stop_tunnel(tunnel_id)
        return self.start_tunnel(tunnel_id)

    def statuses(self) -> dict[int, dict[str, Any]]:
        result: dict[int, dict[str, Any]] = {}
        with self.proc_lock:
            for tunnel_id, proc in list(self.processes.items()):
                code = proc.poll()
                result[tunnel_id] = {
                    "state": "running" if code is None else "exited",
                    "pid": proc.pid,
                    "returncode": code,
                }
        return result

    def read_log(self, tunnel_id: int, max_bytes: int = 64 * 1024) -> str:
        path = self.log_dir / f"tunnel-{tunnel_id}.log"
        if not path.exists():
            return ""
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")

    def start_monitor(self) -> None:
        if self.monitor_started:
            return
        self.monitor_started = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()

    def _monitor_loop(self) -> None:
        while True:
            try:
                enabled = [row["id"] for row in self.list_tunnels() if row["enabled"]]
                for tunnel_id in enabled:
                    proc = self.processes.get(tunnel_id)
                    if proc is None or proc.poll() is not None:
                        self.start_tunnel(tunnel_id)
                for tunnel_id in list(self.processes.keys()):
                    if tunnel_id not in enabled:
                        self.stop_tunnel(tunnel_id)
            except Exception as exc:
                self._write_manager_log(f"monitor error: {exc}")
            time.sleep(int(os.environ.get("MONITOR_INTERVAL", "5")))

    def _write_manager_log(self, message: str) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with (self.log_dir / "manager.log").open("ab") as handle:
            handle.write(f"[{datetime.utcnow().isoformat()}Z] {message}\n".encode())
