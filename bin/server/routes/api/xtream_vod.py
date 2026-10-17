"""Movie, series and NAS download endpoints. Provider traffic is explicit."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from db.connection import db_exists, get_conn, resolve_db_path
from server.services import xtream_vod as vod
from xtream_accounts import public_metadata
from xtream_ingest import XtreamError

bp = Blueprint("xtream_vod_api", __name__)


def _error(exc, status=400):
    if isinstance(exc, KeyError):
        return jsonify(status="error", message=str(exc).strip("'")), 404
    if isinstance(exc, (ValueError, XtreamError)):
        return jsonify(status="error", message=str(exc)), status
    return jsonify(status="error", message="VOD operation failed"), 500


@bp.route("/api/xtream/vod/catalog")
def get_catalog():
    if not db_exists():
        return jsonify(status="error", message="Database not found; run a refresh first"), 404
    try:
        limit = int(request.args.get("limit", 50))
        offset = int(request.args.get("offset", 0))
        with get_conn() as conn:
            data = vod.catalog(conn, kind=request.args.get("kind", "all"),
                               query=request.args.get("q", ""),
                               category_id=request.args.get("category_id", ""),
                               limit=limit, offset=offset)
            return jsonify(status="success", **public_metadata(data, conn))
    except Exception as exc:
        return _error(exc)


@bp.route("/api/xtream/vod/catalog/refresh", methods=["POST"])
def refresh_catalog():
    try:
        result = vod.refresh_catalog()
        return jsonify(status="success", **result)
    except Exception as exc:
        return _error(exc, 502)


@bp.route("/api/xtream/vod/movie/<item_id>/details/refresh", methods=["POST"])
def refresh_movie_details(item_id):
    if not db_exists():
        return jsonify(status="error", message="Database not found"), 404
    try:
        details = vod.refresh_movie_details(item_id)
        return jsonify(status="success", **details)
    except Exception as exc:
        return _error(exc, 502)


@bp.route("/api/xtream/vod/series/<series_id>/episodes")
def get_episodes(series_id):
    if not db_exists():
        return jsonify(status="error", message="Database not found"), 404
    try:
        with get_conn() as conn:
            return jsonify(status="success", episodes=public_metadata(vod.episodes(conn, series_id), conn))
    except Exception as exc:
        return _error(exc)


@bp.route("/api/xtream/vod/series/<series_id>/episodes/refresh", methods=["POST"])
def refresh_episodes(series_id):
    try:
        count = vod.refresh_episodes(series_id)
        return jsonify(status="success", episode_count=count)
    except Exception as exc:
        return _error(exc, 502)


@bp.route("/api/xtream/vod/downloads", methods=["GET", "POST"])
def downloads():
    if not db_exists():
        return jsonify(status="error", message="Database not found"), 404
    try:
        if request.method == "GET":
            with get_conn() as conn:
                return jsonify(status="success", destination=str(vod.download_dir()),
                               destinations={"movies": str(vod.folder_dir("movies")),
                                             "tv": str(vod.folder_dir("tv"))},
                               jobs=public_metadata(vod.list_jobs(conn), conn))
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) not in ({"kind", "item_id"}, {"kind", "item_id", "folder"}):
            raise ValueError("Choose one saved movie or episode")
        with get_conn() as conn:
            job_id = vod.queue_download(conn, payload["kind"], payload["item_id"], payload.get("folder"))
        vod.ensure_worker()
        return jsonify(status="success", job_id=job_id), 202
    except Exception as exc:
        return _error(exc)


@bp.route("/api/xtream/vod/downloads/<int:job_id>", methods=["DELETE"])
def cancel_download(job_id):
    if not db_exists():
        return jsonify(status="error", message="Database not found"), 404
    try:
        with get_conn() as conn:
            if not vod.cancel_job(conn, job_id):
                return jsonify(status="error", message="No removable download with that ID"), 404
        return jsonify(status="success")
    except Exception as exc:
        return _error(exc)


@bp.route("/api/xtream/vod/downloads/stage", methods=["POST"])
def stage_download():
    if not db_exists():
        return jsonify(status="error", message="Database not found"), 404
    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValueError("Choose saved media")
        kind = payload.get("kind")
        with get_conn() as conn:
            if kind == "season" and set(payload) == {"kind", "item_id", "season"}:
                job_id = vod.stage_batch(conn, payload["item_id"], payload["season"])
            elif kind == "show" and set(payload) == {"kind", "item_id"}:
                job_id = vod.stage_batch(conn, payload["item_id"])
            elif kind in {"movie", "episode"} and set(payload) == {"kind", "item_id"}:
                job_id = vod.stage_download(conn, kind, payload["item_id"])
            else:
                raise ValueError("Choose a saved movie, episode, season, or show")
            state = conn.execute("SELECT state FROM xtream_vod_jobs WHERE id=?", (job_id,)).fetchone()[0]
        return jsonify(status="success", job_id=job_id, state=state), 201
    except Exception as exc:
        return _error(exc)


@bp.route("/api/xtream/vod/downloads/<int:job_id>/start", methods=["POST"])
def start_download(job_id):
    if not db_exists():
        return jsonify(status="error", message="Database not found"), 404
    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {"folder"}:
            raise ValueError("Choose Movies or TV Shows")
        with get_conn() as conn:
            if not vod.start_staged_download(conn, job_id, payload["folder"]):
                return jsonify(status="error", message="Download is no longer waiting to start"), 409
        vod.ensure_worker()
        return jsonify(status="success", job_id=job_id), 202
    except Exception as exc:
        return _error(exc)


@bp.route("/api/xtream/vod/downloads/<int:job_id>/retry", methods=["POST"])
def retry_download(job_id):
    if not db_exists():
        return jsonify(status="error", message="Database not found"), 404
    try:
        with get_conn() as conn:
            if not vod.retry_batch(conn, job_id):
                return jsonify(status="error", message="This group has no failed episodes to retry"), 409
        vod.ensure_worker()
        return jsonify(status="success", job_id=job_id), 202
    except Exception as exc:
        return _error(exc)
