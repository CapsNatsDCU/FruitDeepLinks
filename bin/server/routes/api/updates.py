"""Explicit operator actions for the optional Docker-host updater."""

import os
import re
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

from db.connection import get_conn_or_none, resolve_db_path
from server.refresh import refresh_status
from update_protocol import BUSY_PHASES, WAITING_PHASE, host_online, mailbox_lock, read_json, write_json
from version_info import get_version

bp = Blueprint("updates", __name__)


def control_dir():
    configured = os.getenv("XSORT_UPDATE_DIR")
    return Path(configured) if configured else None


def activity():
    reasons = []
    if refresh_status["running"]:
        reasons.append("refresh")
    try:
        with get_conn_or_none() as conn:
            if conn is None:
                raise RuntimeError("Database unavailable")
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('xtream_vod_jobs','xtream_leases')")}
            if "xtream_vod_jobs" in tables and conn.execute(
                "SELECT 1 FROM xtream_vod_jobs WHERE state IN ('pending','running','cancelling') LIMIT 1").fetchone():
                reasons.append("downloads")
            if "xtream_leases" in tables and conn.execute(
                "SELECT 1 FROM xtream_leases LIMIT 1").fetchone():
                reasons.append("streams")
            from xtream_accounts import load_accounts
            from xtream_gate import AccountBusy, AccountGate
            gate = AccountGate(resolve_db_path())
            for account in load_accounts(conn):
                try:
                    fd = gate.acquire(account.config)
                except AccountBusy:
                    reasons.append("provider request")
                    break
                else:
                    os.close(fd)
    except Exception:
        reasons.append("activity_unavailable")
    return reasons


def status():
    directory = control_dir()
    state = read_json(directory / "status.json") if directory else {}
    online = bool(directory and host_online(directory))
    reasons = activity()
    return {**state, "enabled": directory is not None, "online": online,
            "running_version": get_version(),
            "running_revision": os.getenv("FDL_BUILD_REVISION", "unknown"),
            "refresh_running": bool(refresh_status["running"]),
            "idle_blockers": reasons}


@bp.route("/api/updates")
def get_updates():
    response = jsonify(status())
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/api/updates/<action>", methods=["POST"])
def request_update(action):
    # The existing app is a trusted-LAN administration UI. Its global CORS
    # policy is permissive, so enforce same-origin writes here explicitly.
    origin = request.headers.get("Origin")
    if (origin and origin != request.host_url.rstrip("/")) or request.headers.get("X-Xsort-Update") != "1":
        return jsonify(error="Use the Updates controls on this server."), 403
    payload = request.get_json(silent=True)
    if action not in {"check", "install", "install_when_idle", "cancel_waiting"} or not isinstance(payload, dict):
        return jsonify(error="Invalid update request."), 400
    directory = control_dir()
    if not directory or not host_online(directory):
        return jsonify(error="The updater helper is not connected. See setup instructions."), 503
    with mailbox_lock(directory):
        state = read_json(directory / "status.json")
        if action == "cancel_waiting":
            if (state.get("phase") != WAITING_PHASE or state.get("cancel_requested")
                    or (directory / "request.json").exists()):
                return jsonify(error="No scheduled installation is waiting."), 409
            write_json(directory / "request.json", {"action": action, "requested_at": time.time()})
            write_json(directory / "status.json", {**state, "cancel_requested": True,
                                                    "message": "Cancelling scheduled installation…"})
            return jsonify(status="queued"), 202
        if state.get("phase") in BUSY_PHASES or (directory / "request.json").exists():
            return jsonify(error="An update operation is already running."), 409
        revision = payload.get("revision", "")
        if action in {"install", "install_when_idle"}:
            if (not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision)
                    or revision != state.get("available_revision")
                    or not state.get("can_install")):
                return jsonify(error="Check for updates again before installing."), 409
            if action == "install" and activity():
                return jsonify(error="The app is busy. Choose Install when idle."), 409
        write_json(directory / "request.json", {
            "action": action, "revision": revision, "requested_at": time.time(),
        })
        write_json(directory / "status.json", {
            **state, "phase": "queued", "can_install": False,
            "message": ("Update check queued." if action == "check" else
                        "Waiting request queued." if action == "install_when_idle" else "Installation queued."),
        })
    return jsonify(status="queued"), 202
