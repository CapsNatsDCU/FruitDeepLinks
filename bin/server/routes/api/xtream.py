#!/usr/bin/env python3
"""Browse and manage persistent Xtream channels without exposing secrets."""

from __future__ import annotations

import os
import json
import sqlite3

from flask import Blueprint, Response, jsonify, request

from db.connection import db_exists, get_conn, resolve_db_path
from db.preferences import get_setting, save_settings
from server.logging_setup import log
from server.services.xtream_persistent import (
    ChannelNumberConflict,
    DuplicatePersistentChannel,
    PersistentChannelError,
    create_channel,
    delete_channel,
    ensure_schema as ensure_persistent_schema,
    get_channel,
    list_channels,
    normalize_name,
    page_streams,
    quality_for_stream,
    render_m3u,
    render_xmltv,
    save_stream_quality,
    update_channel,
)
from xtream_ingest import XtreamClient, XtreamError, load_metadata_configs
from sports_metadata import coverage, ensure_schema as ensure_sports_schema, utc_now


bp = Blueprint("xtream_api", __name__)


@bp.after_request
def sanitize_xtream_json(response):
    if response.is_json:
        try:
            from xtream_accounts import public_metadata
            with get_conn() as conn:
                response.set_data(json.dumps(public_metadata(response.get_json(), conn)))
        except Exception:
            response.set_data(json.dumps({"status": "error", "message": "Xtream configuration unavailable"}))
            response.status_code = 503
    return response


@bp.route("/api/xtream/pool")
def api_xtream_pool():
    from xtream_pool import XtreamPool
    try:
        return jsonify(XtreamPool(resolve_db_path()).status())
    except Exception as exc:
        return _safe_error(exc)


@bp.route("/api/xtream/pool/check", methods=["POST"])
@bp.route("/api/xtream/pool/accounts/<account_id>/check", methods=["POST"])
def api_xtream_pool_check(account_id=None):
    from xtream_pool import XtreamPool
    try:
        return jsonify(XtreamPool(resolve_db_path()).check_accounts(account_id))
    except Exception as exc:
        return _safe_error(exc)


@bp.route("/api/xtream/pool/accounts/<account_id>", methods=["PATCH"])
def api_xtream_pool_update(account_id):
    from xtream_pool import XtreamPool
    try:
        return jsonify(XtreamPool(resolve_db_path()).update(account_id, request.get_json(silent=True)))
    except Exception as exc:
        return _safe_error(exc)


@bp.route("/api/xtream/epg/refresh", methods=["POST"])
def api_xtream_epg_refresh():
    from xtream_epg import refresh_epg
    from xtream_pool import XtreamPool
    try:
        pool = XtreamPool(resolve_db_path())
        pool.check_accounts()
        with get_conn() as conn:
            config, client = _configured_client(conn)
            try:
                result = refresh_epg(conn, client, pool.accounts)
            finally:
                session = getattr(client, "session", None)
                if session is not None:
                    session.close()
        return jsonify({"status": "success", **result})
    except Exception as exc:
        return _safe_error(exc, 502)


@bp.route("/api/xtream/epg/status")
def api_xtream_epg_status():
    if not db_exists():
        return _read_database_error()
    with get_conn() as conn:
        try:
            rows = [dict(r) for r in conn.execute("SELECT * FROM xtream_epg_status ORDER BY persistent_id")]
        except sqlite3.OperationalError:
            rows = []
    return jsonify({"status": "success", "channels": rows})


@bp.route("/m3u/channels")
@bp.route("/xmltv/channels")
def channels_lineup():
    from server.services import channels_lineup as exports
    if not db_exists():
        return _read_database_error()
    try:
        with get_conn() as conn:
            if request.path.startswith("/m3u/"):
                server_url = str(get_setting(conn, "server_url", request.url_root.rstrip("/")))
                body, content_type = exports.m3u(conn, server_url), "audio/x-mpegurl; charset=utf-8"
            else:
                body, content_type = exports.xmltv(conn), "application/xml; charset=utf-8"
        return Response(body, content_type=content_type, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        return _safe_error(exc)


def _ensure_database() -> None:
    path = resolve_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        sqlite3.connect(str(path)).close()


def _read_database_error():
    """GET handlers must never create a database as a side effect."""
    return jsonify({"status": "error", "message": "Database not found; run a refresh first"}), 404


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
    if isinstance(exc, sqlite3.OperationalError) and any(
        marker in str(exc).lower() for marker in ("locked", "busy")
    ):
        return jsonify({"status": "error", "message": "Database is busy; retry after the current refresh finishes"}), 503
    # Do not stringify arbitrary transport exceptions: requests may include an
    # authenticated URL in their text.
    log(f"Persistent Xtream operation failed: {type(exc).__name__}", "ERROR")
    return jsonify({"status": "error", "message": "Persistent Xtream operation failed"}), 500


def _configured_client(conn):
    configs = load_metadata_configs(conn, os.environ)
    config = configs[0]
    config.validate(require_categories=False)
    client = XtreamClient(config)
    client.metadata_configs = configs
    return config, client


def _catalog_rows(conn, query=""):
    q = f"%{query.casefold()}%"
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM xtream_catalog_categories WHERE lower(name) LIKE ? OR category_id LIKE ? ORDER BY ignored,name,category_id",
            (q, q),
        )]
    except sqlite3.OperationalError:
        # Older databases are read safely as an empty discovery cache.  The
        # next explicit refresh/scan installs and fills this table.
        return []


def _recommendation_score(row, tokens):
    """Score category plus bounded cached preview names using structured tokens."""
    haystacks = [("category", str(row.get("name") or "").casefold())]
    try:
        samples = json.loads(row.get("samples_json") or "[]")
    except (TypeError, ValueError):
        samples = []
    for sample in samples[:25]:
        if isinstance(sample, dict):
            haystacks.append(("sample_stream", str(sample.get("name") or "").casefold()))
    reasons = []
    for token in sorted(tokens):
        if len(token) < 3:
            continue
        locations = sorted({kind for kind, text in haystacks if token in text})
        if locations:
            reasons.append({"match": token, "evidence": locations})
    # Participant/league evidence receives several independent matches; a
    # category title alone cannot silently enable anything.
    return len(reasons), reasons


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
    if not db_exists(): return _read_database_error()
    try:
        with get_conn() as conn:
            return jsonify({"status": "success", "categories": _catalog_rows(conn, request.args.get("q", ""))})
    except Exception as exc:
        return _safe_error(exc)


@bp.route("/api/xtream/discovery/categories/<category_id>/preview", methods=["POST"])
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
    """Rank disabled categories from wanted identities and bounded samples.

    This is a cached/materialized read.  Category scan and individual preview
    are explicit POST operations, so viewing My Sports cannot contact Xtream.
    """
    if not db_exists(): return _read_database_error()
    try:
        with get_conn() as conn:
            wanted = coverage(conn, days=90)
            event_ids = [item["canonical_event_id"] for item in wanted]
            tokens = {str(value).casefold() for item in wanted for value in (item.get("title"), *(p.get("display_name") for p in item.get("participants", []))) if value}
            if event_ids:
                marks = ",".join("?" for _ in event_ids)
                for row in conn.execute(f"SELECT s.name,l.name FROM canonical_events ce LEFT JOIN sports s ON s.id=ce.sport_id LEFT JOIN leagues l ON l.id=ce.league_id WHERE ce.id IN ({marks})", event_ids):
                    tokens.update(str(value).casefold() for value in row if value)
            catalog = [row for row in _catalog_rows(conn)
                       if not row["enabled"] and not row["ignored"] and not row.get("disappeared_utc")]
            recommendations = []
            for row in catalog:
                score, reasons = _recommendation_score(row, tokens)
                if score:
                    recommendations.append({"category_id": row["category_id"], "category_name": row["name"], "score": score,
                                            "reasons": reasons, "sampled": bool(row.get("samples_json") and row.get("samples_json") != "[]")})
        recommendations.sort(key=lambda x: (-x["score"], x["category_name"].casefold(), x["category_id"]))
        return jsonify({"status": "success", "recommendations": recommendations, "previewed_category_ids": [],
                        "selection_changed": False})
    except Exception as exc:
        return _safe_error(exc)


@bp.route("/api/xtream/categories")
def api_xtream_categories():
    if not db_exists(): return _read_database_error()
    try:
        with get_conn() as conn:
            selected_text = str(get_setting(conn, "xtream_category_ids", "") or "")
            selected = {item.strip() for item in selected_text.split(",") if item.strip()}
            cached = _catalog_rows(conn)
        categories = [{"category_id": str(row["category_id"]), "category_name": str(row.get("name") or f"Category {row['category_id']}"),
                       "selected": str(row["category_id"]) in selected}
                      for row in cached]
        known = {row["category_id"] for row in categories}
        categories.extend({"category_id": category_id, "category_name": f"Category {category_id} (not scanned)", "selected": True}
                          for category_id in sorted(selected - known))
        categories.sort(key=lambda row: (row["category_name"].casefold(), row["category_id"]))
        return jsonify({
            "status": "success",
            "categories": categories,
            "selected_category_ids": sorted(selected),
        })
    except Exception as exc:
        return _safe_error(exc, 502)


@bp.route("/api/xtream/categories/live", methods=["POST"])
def api_xtream_categories_live():
    """Browse the current provider catalog without changing event selections."""
    _ensure_database()
    try:
        with get_conn() as conn:
            config, client = _configured_client(conn)
            selected = set(config.category_ids)
            upstream = client.get_live_categories()
        categories = {}
        for row in upstream:
            category_id = str(row.get("category_id") or "").strip()
            if not category_id:
                continue
            categories[category_id] = {
                "category_id": category_id,
                "category_name": str(row.get("category_name") or f"Category {category_id}"),
                "selected": category_id in selected,
            }
        ordered = sorted(categories.values(), key=lambda row: (row["category_name"].casefold(), row["category_id"]))
        return jsonify({"status": "success", "categories": ordered,
                        "selected_category_ids": sorted(selected)})
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


@bp.route("/api/xtream/categories/<category_id>/streams", methods=["POST"])
def api_xtream_category_streams(category_id):
    """Explicitly browse an upstream category; never perform this on GET."""
    _ensure_database()
    try:
        with get_conn() as conn:
            _, client = _configured_client(conn)
            categories = {str(row.get("category_id")) for row in client.get_live_categories()}
            if str(category_id) not in categories:
                raise PersistentChannelError("The selected category is not currently available")
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


@bp.route("/api/xtream/persistent-channels/search", methods=["POST"])
def api_xtream_persistent_search():
    """Search the provider's live streams across every or selected category."""
    query = request.args.get("q", "").strip()
    scope = request.args.get("scope", "all")
    if not query or len(query) > 100:
        return jsonify({"status": "error", "message": "Enter a channel name of up to 100 characters"}), 400
    if scope not in {"all", "active"}:
        return jsonify({"status": "error", "message": "Unknown category search scope"}), 400
    _ensure_database()
    try:
        with get_conn() as conn:
            config, client = _configured_client(conn)
            categories = {
                str(row["category_id"]): str(row.get("category_name") or f"Category {row['category_id']}")
                for row in client.get_live_categories() if row.get("category_id") is not None
            }
            category_ids = set(categories)
            if scope == "active":
                category_ids.intersection_update(config.category_ids)
            try:
                streams = client.get_all_live_streams() if category_ids else []
            except XtreamError:
                streams = []
            needle = normalize_name(query)
            # Some Xtream implementations omit category_id from their full
            # stream response. Fetch each category only when that prevents an
            # accurate match; never silently drop matching channels.
            if category_ids and (not streams or any(
                not str(row.get("category_id") or "").strip()
                and needle in normalize_name(row.get("name")) for row in streams
            )):
                streams = [
                    {**row, "category_id": category_id}
                    for category_id in sorted(category_ids)
                    for row in client.get_live_streams(category_id)
                ]
        scoped = [
            {**row, "category_id": str(row["category_id"]),
             "category_name": categories[str(row["category_id"])]}
            for row in streams if str(row.get("category_id")) in category_ids
        ]
        result = page_streams(
            scoped, query=query,
            page=request.args.get("page", 1, type=int) or 1,
            page_size=request.args.get("page_size", 25, type=int) or 25,
        )
        with get_conn() as conn:
            ensure_persistent_schema(conn)
            for item in result["items"]:
                item["measured_quality"] = quality_for_stream(
                    conn, item["category_id"], item["stream_id"])
        return jsonify({"status": "success", "scope": scope,
                        "category_count": len(category_ids), **result})
    except Exception as exc:
        return _safe_error(exc, 502)


@bp.route("/api/xtream/persistent-channels/quality", methods=["POST"])
def api_xtream_persistent_quality():
    """Measure one provider stream on demand and cache its observed video quality."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "message": "Expected a JSON object"}), 400
    category_id = str(payload.get("category_id") or "").strip()
    stream_id = str(payload.get("stream_id") or "").strip()
    if not category_id or not stream_id or len(category_id) > 255 or len(stream_id) > 255:
        return jsonify({"status": "error", "message": "Category and stream IDs are required"}), 400
    _ensure_database()
    try:
        with get_conn() as conn:
            _, client = _configured_client(conn)
            try:
                categories = client.get_live_categories()
                if category_id not in {str(row.get("category_id")) for row in categories}:
                    raise PersistentChannelError("The selected category is not currently available")
                stream = next((row for row in client.get_live_streams(category_id)
                               if str(row.get("stream_id")) == stream_id), None)
                if stream is None:
                    raise PersistentChannelError("The selected stream is not currently available")
            finally:
                session = getattr(client, "session", None)
                if session is not None:
                    session.close()
        from server.services.xtream_quality import measure_stream_quality
        measured = measure_stream_quality(stream_id, stream.get("container_extension") or "ts")
        with get_conn() as conn:
            saved = save_stream_quality(conn, category_id, stream_id, measured)
        return jsonify({"status": "success", "category_id": category_id,
                        "stream_id": stream_id, "measured_quality": saved})
    except Exception as exc:
        return _safe_error(exc, 502)


@bp.route("/api/xtream/persistent-channels", methods=["GET", "POST"])
def api_xtream_persistent_channels():
    if request.method == "GET":
        if not db_exists(): return _read_database_error()
        try:
            with get_conn() as conn:
                channels = list_channels(conn)
            return jsonify({"status": "success", "channels": channels})
        except Exception as exc:
            return _safe_error(exc)

    _ensure_database()
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "message": "Expected a JSON object"}), 400
    try:
        category_id = str(payload.get("category_id") or "").strip()
        stream_id = str(payload.get("stream_id") or "").strip()
        with get_conn() as conn:
            _, client = _configured_client(conn)
            categories = client.get_live_categories()
            category = next(
                (row for row in categories if str(row.get("category_id")) == category_id), None
            )
            if category is None:
                raise PersistentChannelError("The selected category is not currently available")
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
    if request.method != "GET": _ensure_database()
    elif not db_exists(): return _read_database_error()
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
    # Tuning needs a read from the persistent channel catalog, but a missing
    # catalog is not a reason for a GET to create an empty SQLite database.
    if not db_exists():
        return Response("", status=404)
    try:
        with get_conn() as conn:
            channel = get_channel(conn, persistent_id)
            if not channel or not channel["enabled"]:
                return Response("", status=404)
            if channel["availability_status"] in {"unavailable", "needs_attention"}:
                return Response("", status=503)
        from server.services.xtream_proxy import proxy_stream
        return proxy_stream(channel["stream_id"], f"persistent:{persistent_id}", channel["stream_extension"])
    except (XtreamError, PersistentChannelError):
        return Response("", status=503)
    except Exception as exc:
        log(f"Persistent Xtream tune failed id={persistent_id}: {type(exc).__name__}", "ERROR")
        return Response("", status=500)


@bp.route("/m3u/persistent")
def m3u_xtream_persistent():
    if not db_exists(): return _read_database_error()
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
    if not db_exists(): return _read_database_error()
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
