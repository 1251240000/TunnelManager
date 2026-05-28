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


SCHEMA_VERSION = 2


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
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                conn.execute("DROP TABLE IF EXISTS tunnels")
                conn.execute("DROP TABLE IF EXISTS ssh_hosts")
                self._create_schema(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                return
            self._create_schema(conn)

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ssh_hosts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                remote_user TEXT NOT NULL,
                remote_host TEXT NOT NULL,
                ssh_port INTEGER NOT NULL,
                remote_bind_ip TEXT NOT NULL DEFAULT '0.0.0.0',
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
            CREATE TABLE IF NOT EXISTS tunnels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                host_id INTEGER NOT NULL REFERENCES ssh_hosts(id) ON DELETE RESTRICT,
                target_host TEXT NOT NULL,
                port_mappings TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ssh_hosts_name ON ssh_hosts(name)")

    def bootstrap_from_env(self) -> None:
        with self.connect() as conn:
            host_count = conn.execute("SELECT COUNT(*) AS count FROM ssh_hosts").fetchone()["count"]
            tunnel_count = conn.execute("SELECT COUNT(*) AS count FROM tunnels").fetchone()["count"]
            if host_count or tunnel_count:
                return

        host_records = os.environ.get("BOOTSTRAP_HOSTS", "").strip()
        if host_records:
            records = json.loads(host_records)
            if not isinstance(records, list):
                raise ValueError("BOOTSTRAP_HOSTS must be a JSON array")
            for record in records:
                self.create_host(self._normalize_host_record(record))

        tunnel_records = os.environ.get("BOOTSTRAP_TUNNELS", "").strip()
        if tunnel_records:
            records = json.loads(tunnel_records)
            if not isinstance(records, list):
                raise ValueError("BOOTSTRAP_TUNNELS must be a JSON array")
            for record in records:
                self.create_tunnel(self._normalize_tunnel_record(record))
            return

        if os.environ.get("SSH_REMOTE_HOST") and os.environ.get("SSH_TUNNEL_PORT"):
            host_id = self.create_host(
                {
                    "name": os.environ.get("SSH_HOST_NAME", os.environ["SSH_REMOTE_HOST"]),
                    "remote_user": os.environ.get("SSH_REMOTE_USER", "root"),
                    "remote_host": os.environ["SSH_REMOTE_HOST"],
                    "ssh_port": int(os.environ.get("SSH_REMOTE_PORT", "22")),
                    "remote_bind_ip": os.environ.get("SSH_BIND_IP", "0.0.0.0"),
                    "ssh_key_path": os.environ.get("SSH_KEY_PATH", os.environ.get("SSH_KEY_RUNTIME_PATH", "/data/ssh/tunnel_key")),
                    "extra_args": os.environ.get("AUTOSSH_EXTRA_ARGS", ""),
                    "enabled": 1,
                }
            )
            self.create_tunnel(self._legacy_env_tunnel(host_id))

    def _legacy_env_tunnel(self, host_id: int) -> dict[str, Any]:
        tunnel_ports = [p.strip() for p in os.environ.get("SSH_TUNNEL_PORT", "").split(",") if p.strip()]
        target_ports = [p.strip() for p in os.environ.get("SSH_TARGET_PORT", "").split(",") if p.strip()]
        if len(tunnel_ports) != len(target_ports):
            raise ValueError("SSH_TUNNEL_PORT and SSH_TARGET_PORT must have the same number of ports")

        return {
            "name": os.environ.get("SSH_TUNNEL_NAME", "legacy-tunnel"),
            "host_id": host_id,
            "target_host": os.environ.get("SSH_TARGET_HOST", "127.0.0.1"),
            "port_mappings": self.encode_port_mappings(
                [{"remote_port": int(remote), "target_port": int(target)} for remote, target in zip(tunnel_ports, target_ports)]
            ),
            "enabled": 1,
        }

    def _normalize_host_record(self, record: dict[str, Any]) -> dict[str, Any]:
        data = dict(record)
        data.setdefault("name", data.get("remote_host", "ssh-host"))
        data.setdefault("remote_user", "root")
        data.setdefault("ssh_port", 22)
        data.setdefault("remote_bind_ip", "0.0.0.0")
        data.setdefault("ssh_key_path", os.environ.get("SSH_KEY_RUNTIME_PATH", "/data/ssh/tunnel_key"))
        data.setdefault("extra_args", "")
        data.setdefault("enabled", 1)
        data["ssh_port"] = int(data["ssh_port"])
        data["enabled"] = 1 if data["enabled"] else 0
        return data

    def _normalize_tunnel_record(self, record: dict[str, Any]) -> dict[str, Any]:
        data = dict(record)
        if "host_name" in data and "host_id" not in data:
            host = self.get_host_by_name(str(data["host_name"]))
            if host is None:
                raise ValueError(f"Unknown host_name: {data['host_name']}")
            data["host_id"] = host["id"]
        data.setdefault("enabled", 1)
        if isinstance(data.get("port_mappings"), str):
            mappings = self.parse_port_mappings(data["port_mappings"])
            data["port_mappings"] = self.encode_port_mappings(mappings)
        else:
            data["port_mappings"] = self.encode_port_mappings(data["port_mappings"])
        data["host_id"] = int(data["host_id"])
        data["enabled"] = 1 if data["enabled"] else 0
        return data

    @staticmethod
    def parse_port_mappings(value: str) -> list[dict[str, int]]:
        tokens = [
            token.strip()
            for token in value.replace("\r", "\n").replace(",", "\n").replace(";", "\n").splitlines()
            if token.strip()
        ]
        mappings: list[dict[str, int]] = []
        seen_remote_ports: set[int] = set()

        for token in tokens:
            normalized = token.replace("->", ":").replace("=", ":")
            parts = [part.strip() for part in normalized.split(":")]
            if len(parts) == 1:
                remote_port = target_port = TunnelManager._parse_port(parts[0])
            elif len(parts) == 2:
                remote_port = TunnelManager._parse_port(parts[0])
                target_port = TunnelManager._parse_port(parts[1])
            else:
                raise ValueError(f"Invalid port mapping: {token}")
            if remote_port in seen_remote_ports:
                raise ValueError(f"Duplicate remote port: {remote_port}")
            seen_remote_ports.add(remote_port)
            mappings.append({"remote_port": remote_port, "target_port": target_port})

        if not mappings:
            raise ValueError("At least one port mapping is required")
        return mappings

    @staticmethod
    def _parse_port(value: str) -> int:
        try:
            port = int(value)
        except ValueError as exc:
            raise ValueError(f"{value} is not a valid port") from exc
        if port < 1 or port > 65535:
            raise ValueError(f"{value} is not a valid port")
        return port

    @staticmethod
    def encode_port_mappings(mappings: list[dict[str, Any]]) -> str:
        normalized = []
        for mapping in mappings:
            remote_port = TunnelManager._parse_port(str(mapping["remote_port"]))
            target_port = TunnelManager._parse_port(str(mapping["target_port"]))
            normalized.append({"remote_port": remote_port, "target_port": target_port})
        return json.dumps(normalized, separators=(",", ":"))

    @staticmethod
    def decode_port_mappings(value: str) -> list[dict[str, int]]:
        data = json.loads(value)
        return [{"remote_port": int(item["remote_port"]), "target_port": int(item["target_port"])} for item in data]

    @staticmethod
    def format_port_mappings(value: str | list[dict[str, int]]) -> str:
        mappings = TunnelManager.decode_port_mappings(value) if isinstance(value, str) else value
        return ", ".join(
            str(item["remote_port"])
            if item["remote_port"] == item["target_port"]
            else f"{item['remote_port']}:{item['target_port']}"
            for item in mappings
        )

    def list_hosts(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM ssh_hosts ORDER BY name ASC, id ASC").fetchall()

    def get_host(self, host_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM ssh_hosts WHERE id = ?", (host_id,)).fetchone()

    def get_host_by_name(self, name: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM ssh_hosts WHERE name = ?", (name,)).fetchone()

    def create_host(self, data: dict[str, Any]) -> int:
        now = datetime.utcnow().isoformat()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO ssh_hosts
                (name, remote_user, remote_host, ssh_port, remote_bind_ip, ssh_key_path,
                 extra_args, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["name"],
                    data["remote_user"],
                    data["remote_host"],
                    int(data["ssh_port"]),
                    data["remote_bind_ip"],
                    data["ssh_key_path"],
                    data.get("extra_args", ""),
                    int(data.get("enabled", 1)),
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def update_host(self, host_id: int, data: dict[str, Any]) -> None:
        now = datetime.utcnow().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE ssh_hosts
                SET name = ?, remote_user = ?, remote_host = ?, ssh_port = ?,
                    remote_bind_ip = ?, ssh_key_path = ?, extra_args = ?, enabled = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    data["name"],
                    data["remote_user"],
                    data["remote_host"],
                    int(data["ssh_port"]),
                    data["remote_bind_ip"],
                    data["ssh_key_path"],
                    data.get("extra_args", ""),
                    int(data.get("enabled", 1)),
                    now,
                    host_id,
                ),
            )

    def set_host_enabled(self, host_id: int, enabled: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE ssh_hosts SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, datetime.utcnow().isoformat(), host_id),
            )

    def delete_host(self, host_id: int) -> None:
        for tunnel in self.list_tunnels(host_id=host_id):
            self.stop_tunnel(tunnel["id"])
        with self.connect() as conn:
            conn.execute("DELETE FROM ssh_hosts WHERE id = ?", (host_id,))

    def list_tunnels(self, host_id: int | None = None) -> list[sqlite3.Row]:
        sql = """
            SELECT t.*, h.name AS host_name, h.remote_user, h.remote_host, h.ssh_port,
                   h.remote_bind_ip, h.ssh_key_path, h.extra_args, h.enabled AS host_enabled
            FROM tunnels t
            JOIN ssh_hosts h ON h.id = t.host_id
        """
        params: tuple[Any, ...] = ()
        if host_id is not None:
            sql += " WHERE t.host_id = ?"
            params = (host_id,)
        sql += " ORDER BY t.id DESC"
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def get_tunnel(self, tunnel_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT t.*, h.name AS host_name, h.remote_user, h.remote_host, h.ssh_port,
                       h.remote_bind_ip, h.ssh_key_path, h.extra_args, h.enabled AS host_enabled
                FROM tunnels t
                JOIN ssh_hosts h ON h.id = t.host_id
                WHERE t.id = ?
                """,
                (tunnel_id,),
            ).fetchone()

    def create_tunnel(self, data: dict[str, Any]) -> int:
        now = datetime.utcnow().isoformat()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO tunnels
                (name, host_id, target_host, port_mappings, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["name"],
                    int(data["host_id"]),
                    data["target_host"],
                    data["port_mappings"],
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
                SET name = ?, host_id = ?, target_host = ?, port_mappings = ?,
                    enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    data["name"],
                    int(data["host_id"]),
                    data["target_host"],
                    data["port_mappings"],
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
                f"UPDATE ssh_hosts SET ssh_key_path = ?, updated_at = ? WHERE ssh_key_path IN ({placeholders})",
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
        ]
        for mapping in self.decode_port_mappings(tunnel["port_mappings"]):
            reverse = (
                f"{tunnel['remote_bind_ip']}:{mapping['remote_port']}:"
                f"{tunnel['target_host']}:{mapping['target_port']}"
            )
            command.extend(["-R", reverse])
        extra_args = tunnel["extra_args"].strip()
        if extra_args:
            command.extend(shlex.split(extra_args))
        command.append(remote)
        return command

    def start_tunnel(self, tunnel_id: int) -> bool:
        tunnel = self.get_tunnel(tunnel_id)
        if tunnel is None or not tunnel["host_enabled"]:
            return False

        with self.proc_lock:
            existing = self.processes.get(tunnel_id)
            if existing and existing.poll() is None:
                return True

            command = self.build_command(tunnel)
            log_path = self.log_dir / f"tunnel-{tunnel_id}.log"
            log_file = log_path.open("ab", buffering=0)
            log_file.write(f"\n[{datetime.utcnow().isoformat()}Z] starting: {shlex.join(command)}\n".encode())

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
                enabled = [row["id"] for row in self.list_tunnels() if row["enabled"] and row["host_enabled"]]
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
