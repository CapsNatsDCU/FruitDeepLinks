"""Explicit operator actions for the optional Docker-host updater."""

import os
import re
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

from server.refresh import refresh_status
from update_protocol import BUSY_PHASES, host_online, mailbox_lock, read_json, write_json
from version_info import get_version

bp = Blueprint("updates", __name__)


def control_dir():
    configured = os.getenv("XSORT_UPDATE_DIR")
    return Path(configured) if configured else None


def status():
    directory = control_dir()
    state = read_json(directory / "status.json") if directory else {}
    online = bool(directory and host_online(directory))
    return {**state, "enabled": directory is not None, "online": online,
            "running_version": get_version(),
            "running_revision": os.getenv("FDL_BUILD_REVISION", "unknown"),
            "refresh_running": bool(refresh_status["running"])}


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
    if action not in {"check", "install"} or not isinstance(payload, dict):
        return jsonify(error="Invalid update request."), 400
    directory = control_dir()
    if not directory or not host_online(directory):
        return jsonify(error="The updater helper is not connected. See setup instructions."), 503
    with mailbox_lock(directory):
        state = read_json(directory / "status.json")
        if state.get("phase") in BUSY_PHASES or (directory / "request.json").exists():
            return jsonify(error="An update operation is already running."), 409
        revision = payload.get("revision", "")
        if action == "install":
            if refresh_status["running"]:
                return jsonify(error="Wait for the current refresh to finish."), 409
            if (not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision)
                    or revision != state.get("available_revision")
                    or not state.get("can_install")):
                return jsonify(error="Check for updates again before installing."), 409
        write_json(directory / "request.json", {
            "action": action, "revision": revision, "requested_at": time.time(),
        })
        write_json(directory / "status.json", {
            **state, "phase": "queued", "can_install": False,
            "message": "Update check queued." if action == "check" else "Installation queued.",
        })
    return jsonify(status="queued"), 202
