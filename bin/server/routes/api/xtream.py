#!/usr/bin/env python3
"""Browse and manage persistent Xtream channels without exposing secrets."""

from __future__ import annotations

import os
import json
import sqlite3

from flask import Blueprint, Response, jsonify, redirect, request

from db.connection import get_conn, resolve_db_path
from db.preferences import get_setting, save_settings
from server.logging_setup import log
from server.services.xtream_persistent import (
    ChannelNumberConflict,
    DuplicatePersistentChannel,
    PersistentChannelError,
    create_channel,
    delete_channel,
    get_channel,
    list_channels,
    page_streams,
    render_m3u,
    render_xmltv,
    update_channel,
)
from xtream_ingest import XtreamClient, XtreamError, build_stream_url, load_config
from sports_metadata import coverage, ensure_schema as ensure_sports_schema, utc_now


bp = Blueprint("xtream_api", __name__)


def _ensure_database() -> None:
    path = resolve_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        sqlite3.connect(str(path)).close()


def _safe_error(exc: Exception, status: int = 400):
    if isinstance(exc, ChannelNumberConflict):
        return jsonify({"status": "error", "message": str(exc), "code": "channel_number_conflict"}), 409
    if isinstance(exc, DuplicatePersistentChannel):
        return jsonify({"status": "error", "message": str(exc), "code": "duplicate_stream"}), 409
    if isinstance(exc, PersistentChannelError):
        return jsonify({"status": "error", "message": str(exc)}), 400
    if isinstance(exc, XtreamError):
        return jsonify({"status": "error", "message": str(exc)}), status
    if isinstance(exc, KeyError):
        return jsonify({"status": "error", "message": str(exc).strip("'")}), 404
    # Do not stringify arbitrary transport exceptions: requests may include an
    # authenticated URL in their text.
    log(f"Persistent Xtream operation failed: {type(exc).__name__}", "ERROR")
    return jsonify({"status": "error", "message": "Persistent Xtream operation failed"}), 500


def _configured_client(conn):
    config = load_config(conn, os.environ)
    config.validate(require_categories=False)
    return config, XtreamClient(config)


def _catalog_rows(conn, query=""):
    q = f"%{query.casefold()}%"
    return [dict(row) for row in conn.execute(
        "SELECT * FROM xtream_catalog_categories WHERE lower(name) LIKE ? OR category_id LIKE ? ORDER BY ignored,name,category_id",
        (q, q),
    )]


@bp.route("/api/xtream/discovery/scan", methods=["POST"])
def api_xtream_discovery_scan():
    """Persist category metadata only; scan never changes selected IDs."""
    _ensure_database()
    try:
        with get_conn() as conn:
            ensure_sports_schema(conn)
            config, client = _configured_client(conn)
            upstream = client.get_live_categories()
            now = utc_now(); selected = set(config.category_ids)
            seen = set()
            for category in upstream:
                category_id = str(category.get("category_id") or "").strip()
                if not category_id: continue
                name = str(category.get("category_name") or f"Category {category_id}").strip()
                seen.add(category_id)
                conn.execute("INSERT INTO xtream_catalog_categories(category_id,name,normalized_name,enabled,ignored,first_seen_utc,last_seen_utc,disappeared_utc) VALUES(?,?,?,?,?,?,?,NULL) "
                             "ON CONFLICT(category_id) DO UPDATE SET name=excluded.name,normalized_name=excluded.normalized_name,enabled=excluded.enabled,last_seen_utc=excluded.last_seen_utc,disappeared_utc=NULL",
                             (category_id, name, name.casefold(), int(category_id in selected), 0, now, now))
            conn.execute("UPDATE xtream_catalog_categories SET disappeared_utc=? WHERE last_seen_utc < ?", (now, now))
            conn.commit()
            rows = _catalog_rows(conn)
        return jsonify({"status": "success", "categories": rows, "scan_changed_selection": False})
    except Exception as exc:
        return _safe_error(exc, 502)


@bp.route("/api/xtream/discovery/categories")
def api_xtream_discovery_categories():
    _ensure_database()
    try:
        with get_conn() as conn:
            ensure_sports_schema(conn)
            return jsonify({"status": "success", "categories": _catalog_rows(conn, request.args.get("q", ""))})
    except Exception as exc:
        return _safe_error(exc)


@bp.route("/api/xtream/discovery/categories/<category_id>/preview")
def api_xtream_discovery_preview(category_id):
    """Retrieve a bounded, credential-safe sample without enabling ingestion."""
    _ensure_database()
    try:
        with get_conn() as conn:
            ensure_sports_schema(conn)
            config, client = _configured_client(conn)
            categories = {str(row.get("category_id")): row for row in client.get_live_categories()}
            if str(category_id) not in categories: raise KeyError("Xtream category not found")
            streams = client.get_live_streams(str(category_id))
            samples = [{"stream_id": str(row.get("stream_id") or ""), "name": str(row.get("name") or ""),
                        "icon": row.get("stream_icon"), "epg_channel_id": row.get("epg_channel_id")} for row in streams[:50]]
            name = str(categories[str(category_id)].get("category_name") or category_id)
            now = utc_now()
            conn.execute("INSERT INTO xtream_catalog_categories(category_id,name,normalized_name,enabled,ignored,stream_count,samples_json,first_seen_utc,last_seen_utc) VALUES(?,?,?,?,?,?,?,?,?) "
                         "ON CONFLICT(category_id) DO UPDATE SET name=excluded.name,stream_count=excluded.stream_count,samples_json=excluded.samples_json,last_seen_utc=excluded.last_seen_utc",
                         (str(category_id), name, name.casefold(), int(str(category_id) in config.category_ids), 0, len(streams), json.dumps(samples), now, now))
            conn.commit()
        return jsonify({"status": "success", "category_id": str(category_id), "category_name": name,
                        "stream_count": len(streams), "samples": samples, "enabled_changed": False})
    except Exception as exc:
        return _safe_error(exc, 502)


@bp.route("/api/xtream/discovery/categories/<category_id>/ignore", methods=["POST", "DELETE"])
def api_xtream_discovery_ignore(category_id):
    _ensure_database()
    try:
        with get_conn() as conn:
            ensure_sports_schema(conn)
            ignored = request.method == "POST"
            changed = conn.execute("UPDATE xtream_catalog_categories SET ignored=? WHERE category_id=?", (int(ignored), str(category_id))).rowcount
            conn.commit()
        if not changed: return jsonify({"status": "error", "message": "Scan the category catalog first"}), 404
        return jsonify({"status": "success", "category_id": str(category_id), "ignored": ignored})
    except Exception as exc:
        return _safe_error(exc)


@bp.route("/api/xtream/discovery/recommendations")
def api_xtream_discovery_recommendations():
    """Rank disabled categories using canonical wanted metadata, never raw aliases alone."""
    _ensure_database()
    try:
        with get_conn() as conn:
            ensure_sports_schema(conn)
            wanted = coverage(conn, days=90)
            event_ids = [item["canonical_event_id"] for item in wanted]
            tokens = {str(value).casefold() for item in wanted for value in (item.get("title"), *(p.get("display_name") for p in item.get("participants", []))) if value}
            if event_ids:
                marks = ",".join("?" for _ in event_ids)
                for row in conn.execute(f"SELECT s.name,l.name FROM canonical_events ce LEFT JOIN sports s ON s.id=ce.sport_id LEFT JOIN leagues l ON l.id=ce.league_id WHERE ce.id IN ({marks})", event_ids):
                    tokens.update(str(value).casefold() for value in row if value)
            recommendations = []
            for row in _catalog_rows(conn):
                if row["enabled"] or row["ignored"] or row.get("disappeared_utc"): continue
                haystack = row["name"].casefold()
                hits = sorted(token for token in tokens if len(token) > 2 and token in haystack)
                if hits:
                    recommendations.append({"category_id": row["category_id"], "category_name": row["name"], "score": len(hits), "reasons": hits})
        recommendations.sort(key=lambda x: (-x["score"], x["category_name"].casefold(), x["category_id"]))
        return jsonify({"status": "success", "recommendations": recommendations})
    except Exception as exc:
        return _safe_error(exc)


@bp.route("/api/xtream/categories")
def api_xtream_categories():
    _ensure_database()
    try:
        with get_conn() as conn:
            config, client = _configured_client(conn)
            upstream = client.get_live_categories()
        selected = set(config.category_ids)
        categories = sorted(({
            "category_id": str(row["category_id"]),
            "category_name": str(row.get("category_name") or f"Category {row['category_id']}"),
            "selected": str(row["category_id"]) in selected,
        } for row in upstream if row.get("category_id") is not None),
            key=lambda row: (row["category_name"].casefold(), row["category_id"]))
        return jsonify({
            "status": "success",
            "categories": categories,
            "selected_category_ids": list(config.category_ids),
        })
    except Exception as exc:
        return _safe_error(exc, 502)


@bp.route("/api/xtream/categories", methods=["POST"])
def api_save_xtream_categories():
    """Persist an explicit category selection without ever handling secrets."""
    _ensure_database()
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("category_ids") if isinstance(payload, dict) else None
    if not isinstance(raw_ids, list) or any(not str(value).strip() for value in raw_ids):
        return jsonify({"status": "error", "message": "Category selection must be a list of category IDs"}), 400
    selected = list(dict.fromkeys(str(value).strip() for value in raw_ids))
    try:
        with get_conn() as conn:
            config, client = _configured_client(conn)
            available = {str(row.get("category_id")) for row in client.get_live_categories()}
            missing = [category_id for category_id in selected if category_id not in available]
            if missing:
                raise PersistentChannelError("One or more selected categories are no longer available")
            if not save_settings(conn, {"xtream_category_ids": ",".join(selected)}):
                raise PersistentChannelError("Could not save Xtream category selection")
        return jsonify({"status": "success", "selected_category_ids": selected})
    except Exception as exc:
        return _safe_error(exc, 502)


@bp.route("/api/xtream/categories/<category_id>/streams")
def api_xtream_category_streams(category_id):
    _ensure_database()
    try:
        with get_conn() as conn:
            config, client = _configured_client(conn)
            if str(category_id) not in config.category_ids:
                raise PersistentChannelError(
                    "Only categories configured in Xtream settings can be browsed"
                )
            streams = client.get_live_streams(str(category_id))
        result = page_streams(
            streams,
            query=request.args.get("q", ""),
            page=request.args.get("page", 1, type=int) or 1,
            page_size=request.args.get("page_size", 50, type=int) or 50,
        )
        return jsonify({"status": "success", "category_id": str(category_id), **result})
    except Exception as exc:
        return _safe_error(exc, 502)


@bp.route("/api/xtream/persistent-channels", methods=["GET", "POST"])
def api_xtream_persistent_channels():
    _ensure_database()
    if request.method == "GET":
        try:
            with get_conn() as conn:
                channels = list_channels(conn)
            return jsonify({"status": "success", "channels": channels})
        except Exception as exc:
            return _safe_error(exc)

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "message": "Expected a JSON object"}), 400
    try:
        category_id = str(payload.get("category_id") or "").strip()
        stream_id = str(payload.get("stream_id") or "").strip()
        with get_conn() as conn:
            config, client = _configured_client(conn)
            if category_id not in config.category_ids:
                raise PersistentChannelError(
                    "Only streams from configured Xtream categories can be added"
                )
            categories = client.get_live_categories()
            category = next(
                (row for row in categories if str(row.get("category_id")) == category_id), None
            )
            if category is None:
                raise PersistentChannelError("The configured category is not currently available")
            streams = client.get_live_streams(category_id)
            stream = next(
                (row for row in streams if str(row.get("stream_id")) == stream_id), None
            )
            if stream is None:
                raise PersistentChannelError("The selected stream is not currently available")
            channel = create_channel(
                conn,
                stream,
                category_id=category_id,
                category_name=category.get("category_name"),
                channel_number=payload.get("channel_number"),
                display_name=payload.get("display_name"),
                channel_id=payload.get("channel_id"),
                guide_id=payload.get("guide_id"),
                logo_override=payload.get("logo_override"),
                favorite_team=payload.get("favorite_team"),
                notes=payload.get("notes"),
                enabled=payload.get("enabled", True),
            )
        log(
            f"Added persistent Xtream channel id={channel['id']} stream_id={channel['stream_id']}",
            "INFO",
        )
        return jsonify({"status": "success", "channel": channel}), 201
    except Exception as exc:
        return _safe_error(exc)


@bp.route("/api/xtream/persistent-channels/<int:persistent_id>",
          methods=["GET", "PUT", "PATCH", "DELETE"])
def api_xtream_persistent_channel(persistent_id):
    _ensure_database()
    try:
        with get_conn() as conn:
            if request.method == "GET":
                channel = get_channel(conn, persistent_id)
                if channel is None:
                    raise KeyError("Persistent channel not found")
                return jsonify({"status": "success", "channel": channel})
            if request.method == "DELETE":
                if not delete_channel(conn, persistent_id):
                    raise KeyError("Persistent channel not found")
                log(f"Deleted persistent Xtream channel id={persistent_id}", "INFO")
                return jsonify({"status": "success", "deleted": persistent_id})
            payload = request.get_json(silent=True) or {}
            if not isinstance(payload, dict):
                raise PersistentChannelError("Expected a JSON object")
            channel = update_channel(conn, persistent_id, payload)
        log(f"Updated persistent Xtream channel id={persistent_id}", "INFO")
        return jsonify({"status": "success", "channel": channel})
    except Exception as exc:
        return _safe_error(exc)


@bp.route("/xtream/channel/<int:persistent_id>/stream", methods=["GET", "HEAD"])
def xtream_persistent_stream(persistent_id):
    _ensure_database()
    try:
        with get_conn() as conn:
            channel = get_channel(conn, persistent_id)
            if not channel or not channel["enabled"]:
                return Response("", status=404)
            if channel["availability_status"] in {"unavailable", "needs_attention"}:
                return Response("", status=503)
            config = load_config(conn, os.environ)
            target = build_stream_url(
                config, channel["stream_id"], channel["stream_extension"]
            )
        # Never include target in logs; it contains the environment-only secrets.
        log(
            f"XTREAM_PERSISTENT_STREAM id={persistent_id} stream_id={channel['stream_id']} redirect",
            "INFO",
        )
        response = redirect(target, code=302)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except (XtreamError, PersistentChannelError):
        return Response("", status=503)
    except Exception as exc:
        log(f"Persistent Xtream tune failed id={persistent_id}: {type(exc).__name__}", "ERROR")
        return Response("", status=500)


@bp.route("/m3u/persistent")
def m3u_xtream_persistent():
    _ensure_database()
    try:
        with get_conn() as conn:
            server_url = str(get_setting(conn, "server_url", request.url_root.rstrip("/")))
            body = render_m3u(conn, server_url)
        return Response(
            body,
            mimetype="audio/x-mpegurl",
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        return _safe_error(exc)


@bp.route("/xmltv/persistent")
def xmltv_xtream_persistent():
    _ensure_database()
    try:
        with get_conn() as conn:
            body = render_xmltv(conn)
        return Response(
            body,
            content_type="application/xml; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        return _safe_error(exc)
