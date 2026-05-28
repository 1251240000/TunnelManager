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


def row_to_form(row: sqlite3.Row | None = None) -> dict[str, Any]:
    defaults = {
        "name": "",
        "remote_user": os.environ.get("DEFAULT_SSH_REMOTE_USER", "root"),
        "remote_host": os.environ.get("DEFAULT_SSH_REMOTE_HOST", ""),
        "ssh_port": os.environ.get("DEFAULT_SSH_REMOTE_PORT", "22"),
        "remote_bind_ip": os.environ.get("DEFAULT_SSH_BIND_IP", "0.0.0.0"),
        "remote_bind_port": "",
        "target_host": "",
        "target_port": "",
        "ssh_key_path": os.environ.get("DEFAULT_SSH_KEY_PATH", "/id_rsa"),
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


def parse_form() -> tuple[dict[str, Any], list[str]]:
    fields = row_to_form()
    data: dict[str, Any] = {}
    errors: list[str] = []

    for key in fields:
        data[key] = request.form.get(key, "").strip()

    required = [
        "name",
        "remote_user",
        "remote_host",
        "ssh_port",
        "remote_bind_ip",
        "remote_bind_port",
        "target_host",
        "target_port",
        "ssh_key_path",
    ]
    for key in required:
        if not data[key]:
            errors.append(f"{key} is required")

    for key in ("ssh_port", "remote_bind_port", "target_port"):
        try:
            value = int(data[key])
            if value < 1 or value > 65535:
                raise ValueError
            data[key] = value
        except ValueError:
            errors.append(f"{key} must be a port between 1 and 65535")

    data["enabled"] = 1 if request.form.get("enabled") == "1" else 0
    return data, errors


@app.get("/")
@require_auth
def index():
    tunnels = manager.list_tunnels()
    statuses = manager.statuses()
    return render_template("index.html", tunnels=tunnels, statuses=statuses)


@app.get("/tunnels/new")
@require_auth
def new_tunnel():
    return render_template("form.html", tunnel=None, form=row_to_form(), action=url_for("create_tunnel"))


@app.post("/tunnels")
@require_auth
def create_tunnel():
    data, errors = parse_form()
    if errors:
        for error in errors:
            flash(error, "error")
        return render_template("form.html", tunnel=None, form=data, action=url_for("create_tunnel")), 400

    try:
        tunnel_id = manager.create_tunnel(data)
    except sqlite3.IntegrityError:
        flash("Remote bind endpoint already exists", "error")
        return render_template("form.html", tunnel=None, form=data, action=url_for("create_tunnel")), 409
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
    return render_template("form.html", tunnel=tunnel, form=row_to_form(tunnel), action=url_for("update_tunnel", tunnel_id=tunnel_id))


@app.post("/tunnels/<int:tunnel_id>")
@require_auth
def update_tunnel(tunnel_id: int):
    tunnel = manager.get_tunnel(tunnel_id)
    if tunnel is None:
        flash("Tunnel not found", "error")
        return redirect(url_for("index"))

    data, errors = parse_form()
    if errors:
        for error in errors:
            flash(error, "error")
        return render_template("form.html", tunnel=tunnel, form=data, action=url_for("update_tunnel", tunnel_id=tunnel_id)), 400

    try:
        manager.update_tunnel(tunnel_id, data)
    except sqlite3.IntegrityError:
        flash("Remote bind endpoint already exists", "error")
        return render_template("form.html", tunnel=tunnel, form=data, action=url_for("update_tunnel", tunnel_id=tunnel_id)), 409

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
    return render_template("logs.html", tunnel=tunnel, log_text=manager.read_log(tunnel_id))


@app.get("/api/status")
@require_auth
def api_status():
    payload = []
    statuses = manager.statuses()
    for tunnel in manager.list_tunnels():
        item = dict(tunnel)
        item["status"] = statuses.get(tunnel["id"], {"state": "stopped"})
        payload.append(item)
    return jsonify({"now": datetime.utcnow().isoformat() + "Z", "tunnels": payload})


if __name__ == "__main__":
    ensure_started()
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")))
