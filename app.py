from __future__ import annotations

import base64
import functools
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, flash, jsonify, redirect, render_template, request, url_for

from tunnel_manager import TunnelManager


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "tunnels.db"


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")
app.config["DATA_DIR"] = DATA_DIR

manager = TunnelManager(DB_PATH, DATA_DIR / "logs")
_started = False
_started_lock = threading.Lock()


def require_auth(view):
    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD", "")

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not password:
            return view(*args, **kwargs)

        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                raw = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
                user, passwd = raw.split(":", 1)
                if user == username and passwd == password:
                    return view(*args, **kwargs)
            except Exception:
                pass

        return Response(
            "Authentication required",
            401,
            {"WWW-Authenticate": 'Basic realm="Tunnel Manager"'},
        )

    return wrapped


def ensure_started() -> None:
    global _started
    with _started_lock:
        if _started:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        manager.init_db()
        manager.bootstrap_from_env()
        manager.rewrite_default_key_paths()
        manager.start_monitor()
        _started = True


@app.before_request
def boot() -> None:
    ensure_started()


def default_key_path() -> str:
    return os.environ.get("DEFAULT_SSH_KEY_PATH") or os.environ.get("SSH_KEY_RUNTIME_PATH", "/data/ssh/tunnel_key")


def host_to_form(row: sqlite3.Row | None = None) -> dict[str, Any]:
    defaults = {
        "name": "",
        "remote_user": os.environ.get("DEFAULT_SSH_REMOTE_USER", "root"),
        "remote_host": os.environ.get("DEFAULT_SSH_REMOTE_HOST", ""),
        "ssh_port": os.environ.get("DEFAULT_SSH_REMOTE_PORT", "22"),
        "remote_bind_ip": os.environ.get("DEFAULT_SSH_BIND_IP", "0.0.0.0"),
        "ssh_key_path": default_key_path(),
        "extra_args": os.environ.get("DEFAULT_AUTOSSH_EXTRA_ARGS", ""),
        "enabled": "1",
    }
    if row is None:
        return defaults
    data = dict(defaults)
    for key in data:
        if key in row.keys():
            data[key] = str(row[key])
    data["enabled"] = "1" if row["enabled"] else "0"
    return data


def tunnel_to_form(row: sqlite3.Row | None = None) -> dict[str, Any]:
    defaults = {
        "name": "",
        "host_id": "",
        "target_host": "",
        "port_mappings": "",
        "enabled": "1",
    }
    if row is None:
        return defaults
    data = dict(defaults)
    for key in ("name", "host_id", "target_host"):
        data[key] = str(row[key])
    data["port_mappings"] = manager.format_port_mappings(row["port_mappings"])
    data["enabled"] = "1" if row["enabled"] else "0"
    return data


def parse_host_form() -> tuple[dict[str, Any], list[str]]:
    data = {key: request.form.get(key, "").strip() for key in host_to_form()}
    errors: list[str] = []

    required = ["name", "remote_user", "remote_host", "ssh_port", "remote_bind_ip", "ssh_key_path"]
    for key in required:
        if not data[key]:
            errors.append(f"{key} is required")

    try:
        data["ssh_port"] = parse_port(data["ssh_port"])
    except ValueError:
        errors.append("ssh_port must be a port between 1 and 65535")

    data["enabled"] = 1 if request.form.get("enabled") == "1" else 0
    return data, errors


def parse_tunnel_form() -> tuple[dict[str, Any], list[str]]:
    data = {key: request.form.get(key, "").strip() for key in tunnel_to_form()}
    errors: list[str] = []

    for key in ("name", "host_id", "target_host", "port_mappings"):
        if not data[key]:
            errors.append(f"{key} is required")

    try:
        data["host_id"] = int(data["host_id"])
        if manager.get_host(data["host_id"]) is None:
            errors.append("host_id does not exist")
    except ValueError:
        errors.append("host_id is required")

    try:
        mappings = manager.parse_port_mappings(data["port_mappings"])
        data["port_mappings"] = manager.encode_port_mappings(mappings)
        data["port_mappings_text"] = manager.format_port_mappings(mappings)
    except ValueError as exc:
        errors.append(f"port_mappings is invalid: {exc}")
        data["port_mappings_text"] = data["port_mappings"]

    data["enabled"] = 1 if request.form.get("enabled") == "1" else 0
    return data, errors


def parse_port(value: str) -> int:
    port = int(value)
    if port < 1 or port > 65535:
        raise ValueError
    return port


def tunnel_view(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    mappings = manager.decode_port_mappings(row["port_mappings"])
    data["mappings"] = mappings
    data["mapping_text"] = manager.format_port_mappings(mappings)
    data["entrances"] = [f"{row['remote_bind_ip']}:{item['remote_port']}" for item in mappings]
    data["targets"] = [f"{row['target_host']}:{item['target_port']}" for item in mappings]
    return data


def host_usage_counts() -> dict[int, int]:
    counts: dict[int, int] = {}
    for tunnel in manager.list_tunnels():
        counts[tunnel["host_id"]] = counts.get(tunnel["host_id"], 0) + 1
    return counts


@app.get("/")
@require_auth
def index():
    tunnels = [tunnel_view(row) for row in manager.list_tunnels()]
    statuses = manager.statuses()
    hosts = manager.list_hosts()
    return render_template("index.html", tunnels=tunnels, statuses=statuses, hosts=hosts)


@app.get("/hosts")
@require_auth
def hosts():
    counts = host_usage_counts()
    rows = []
    for host in manager.list_hosts():
        item = dict(host)
        item["tunnel_count"] = counts.get(host["id"], 0)
        rows.append(item)
    return render_template("hosts.html", hosts=rows)


@app.get("/hosts/new")
@require_auth
def new_host():
    return render_template("host_form.html", host=None, form=host_to_form(), action=url_for("create_host"))


@app.post("/hosts")
@require_auth
def create_host():
    data, errors = parse_host_form()
    if errors:
        for error in errors:
            flash(error, "error")
        return render_template("host_form.html", host=None, form=data, action=url_for("create_host")), 400

    try:
        manager.create_host(data)
    except sqlite3.IntegrityError:
        flash("Host name already exists", "error")
        return render_template("host_form.html", host=None, form=data, action=url_for("create_host")), 409
    flash("Host created", "ok")
    return redirect(url_for("hosts"))


@app.get("/hosts/<int:host_id>/edit")
@require_auth
def edit_host(host_id: int):
    host = manager.get_host(host_id)
    if host is None:
        flash("Host not found", "error")
        return redirect(url_for("hosts"))
    return render_template("host_form.html", host=host, form=host_to_form(host), action=url_for("update_host", host_id=host_id))


@app.post("/hosts/<int:host_id>")
@require_auth
def update_host(host_id: int):
    host = manager.get_host(host_id)
    if host is None:
        flash("Host not found", "error")
        return redirect(url_for("hosts"))

    data, errors = parse_host_form()
    if errors:
        for error in errors:
            flash(error, "error")
        return render_template("host_form.html", host=host, form=data, action=url_for("update_host", host_id=host_id)), 400

    try:
        manager.update_host(host_id, data)
    except sqlite3.IntegrityError:
        flash("Host name already exists", "error")
        return render_template("host_form.html", host=host, form=data, action=url_for("update_host", host_id=host_id)), 409

    for tunnel in manager.list_tunnels(host_id=host_id):
        if data["enabled"] and tunnel["enabled"]:
            manager.restart_tunnel(tunnel["id"])
        else:
            manager.stop_tunnel(tunnel["id"])
    flash("Host updated", "ok")
    return redirect(url_for("hosts"))


@app.post("/hosts/<int:host_id>/delete")
@require_auth
def delete_host(host_id: int):
    if manager.list_tunnels(host_id=host_id):
        flash("Host has tunnels; delete or move those tunnels first", "error")
        return redirect(url_for("hosts"))
    try:
        manager.delete_host(host_id)
    except sqlite3.IntegrityError:
        flash("Host has tunnels; delete or move those tunnels first", "error")
        return redirect(url_for("hosts"))
    flash("Host deleted", "ok")
    return redirect(url_for("hosts"))


@app.get("/tunnels/new")
@require_auth
def new_tunnel():
    hosts = manager.list_hosts()
    if not hosts:
        flash("Create an SSH host before adding tunnels", "error")
        return redirect(url_for("new_host"))
    return render_template("tunnel_form.html", tunnel=None, form=tunnel_to_form(), hosts=hosts, action=url_for("create_tunnel"))


@app.post("/tunnels")
@require_auth
def create_tunnel():
    hosts = manager.list_hosts()
    data, errors = parse_tunnel_form()
    form = dict(data)
    form["port_mappings"] = data.get("port_mappings_text", request.form.get("port_mappings", ""))
    if errors:
        for error in errors:
            flash(error, "error")
        return render_template("tunnel_form.html", tunnel=None, form=form, hosts=hosts, action=url_for("create_tunnel")), 400

    try:
        tunnel_id = manager.create_tunnel(data)
    except sqlite3.IntegrityError:
        flash("Failed to create tunnel", "error")
        return render_template("tunnel_form.html", tunnel=None, form=form, hosts=hosts, action=url_for("create_tunnel")), 409
    if data["enabled"]:
        if not manager.start_tunnel(tunnel_id):
            flash("Tunnel saved but failed to start; check logs", "error")
            return redirect(url_for("logs", tunnel_id=tunnel_id))
    flash("Tunnel created", "ok")
    return redirect(url_for("index"))


@app.get("/tunnels/<int:tunnel_id>/edit")
@require_auth
def edit_tunnel(tunnel_id: int):
    tunnel = manager.get_tunnel(tunnel_id)
    if tunnel is None:
        flash("Tunnel not found", "error")
        return redirect(url_for("index"))
    return render_template(
        "tunnel_form.html",
        tunnel=tunnel,
        form=tunnel_to_form(tunnel),
        hosts=manager.list_hosts(),
        action=url_for("update_tunnel", tunnel_id=tunnel_id),
    )


@app.post("/tunnels/<int:tunnel_id>")
@require_auth
def update_tunnel(tunnel_id: int):
    tunnel = manager.get_tunnel(tunnel_id)
    if tunnel is None:
        flash("Tunnel not found", "error")
        return redirect(url_for("index"))

    hosts = manager.list_hosts()
    data, errors = parse_tunnel_form()
    form = dict(data)
    form["port_mappings"] = data.get("port_mappings_text", request.form.get("port_mappings", ""))
    if errors:
        for error in errors:
            flash(error, "error")
        return render_template("tunnel_form.html", tunnel=tunnel, form=form, hosts=hosts, action=url_for("update_tunnel", tunnel_id=tunnel_id)), 400

    try:
        manager.update_tunnel(tunnel_id, data)
    except sqlite3.IntegrityError:
        flash("Failed to update tunnel", "error")
        return render_template("tunnel_form.html", tunnel=tunnel, form=form, hosts=hosts, action=url_for("update_tunnel", tunnel_id=tunnel_id)), 409

    if data["enabled"]:
        if not manager.restart_tunnel(tunnel_id):
            flash("Tunnel saved but failed to restart; check logs", "error")
            return redirect(url_for("logs", tunnel_id=tunnel_id))
    else:
        manager.stop_tunnel(tunnel_id)
    flash("Tunnel updated", "ok")
    return redirect(url_for("index"))


@app.post("/tunnels/<int:tunnel_id>/start")
@require_auth
def start_tunnel(tunnel_id: int):
    manager.set_enabled(tunnel_id, True)
    if manager.start_tunnel(tunnel_id):
        flash("Tunnel started", "ok")
    else:
        flash("Tunnel failed to start; check logs", "error")
    return redirect(url_for("index"))


@app.post("/tunnels/<int:tunnel_id>/stop")
@require_auth
def stop_tunnel(tunnel_id: int):
    manager.set_enabled(tunnel_id, False)
    manager.stop_tunnel(tunnel_id)
    flash("Tunnel stopped", "ok")
    return redirect(url_for("index"))


@app.post("/tunnels/<int:tunnel_id>/restart")
@require_auth
def restart_tunnel(tunnel_id: int):
    manager.set_enabled(tunnel_id, True)
    if manager.restart_tunnel(tunnel_id):
        flash("Tunnel restarted", "ok")
    else:
        flash("Tunnel failed to restart; check logs", "error")
    return redirect(url_for("index"))


@app.post("/tunnels/<int:tunnel_id>/delete")
@require_auth
def delete_tunnel(tunnel_id: int):
    manager.delete_tunnel(tunnel_id)
    flash("Tunnel deleted", "ok")
    return redirect(url_for("index"))


@app.get("/tunnels/<int:tunnel_id>/logs")
@require_auth
def logs(tunnel_id: int):
    tunnel = manager.get_tunnel(tunnel_id)
    if tunnel is None:
        flash("Tunnel not found", "error")
        return redirect(url_for("index"))
    return render_template("logs.html", tunnel=tunnel_view(tunnel), log_text=manager.read_log(tunnel_id))


@app.get("/api/status")
@require_auth
def api_status():
    payload = []
    statuses = manager.statuses()
    for tunnel in manager.list_tunnels():
        item = tunnel_view(tunnel)
        item["status"] = statuses.get(tunnel["id"], {"state": "stopped"})
        payload.append(item)
    return jsonify({"now": datetime.utcnow().isoformat() + "Z", "tunnels": payload})


if __name__ == "__main__":
    ensure_started()
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")))
