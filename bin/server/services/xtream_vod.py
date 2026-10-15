"""Cached Xtream VOD catalog and a durable, single-download NAS queue."""
from __future__ import annotations

import fcntl
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import quote

import requests

from db.connection import get_conn, resolve_db_path
from xtream_accounts import load_accounts, safe_value
from xtream_gate import AccountBusy, AccountGate
from xtream_hosts import host_configs, record_host_success
from xtream_ingest import XtreamClient, XtreamError, load_metadata_configs
from xtream_pool import PoolUnavailable, XtreamPool
from xtream_transport import configure_session
from xtream_curl import CurlStream

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_EXTENSIONS = {"mp4", "mkv", "avi", "ts", "mov", "m4v"}
_PLEX_ID_SUFFIX = re.compile(r"\s+\{(?:tmdb-\d+|tvdb-\d+|imdb-tt\d+)\}\Z", re.IGNORECASE)
_YEAR_SUFFIX = re.compile(r"\s+\((?:18|19|20|21)\d{2}\)\Z")
_CHUNK = 256 * 1024
_worker: threading.Thread | None = None
_worker_guard = threading.Lock()


def ensure_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS xtream_vod_state (
            id INTEGER PRIMARY KEY CHECK(id=1), refreshed_at TEXT NOT NULL,
            movie_count INTEGER NOT NULL, series_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS xtream_vod_categories (
            kind TEXT NOT NULL, category_id TEXT NOT NULL, name TEXT NOT NULL,
            PRIMARY KEY(kind, category_id)
        );
        CREATE TABLE IF NOT EXISTS xtream_vod_items (
            kind TEXT NOT NULL, item_id TEXT NOT NULL, category_id TEXT NOT NULL,
            title TEXT NOT NULL, search_title TEXT NOT NULL, extension TEXT,
            size_bytes INTEGER, resolution TEXT,
            PRIMARY KEY(kind, item_id)
        );
        CREATE INDEX IF NOT EXISTS idx_xtream_vod_search ON xtream_vod_items(kind, search_title);
        CREATE TABLE IF NOT EXISTS xtream_vod_episodes (
            series_id TEXT NOT NULL, episode_id TEXT NOT NULL, season INTEGER NOT NULL,
            episode INTEGER NOT NULL, title TEXT NOT NULL, extension TEXT,
            size_bytes INTEGER, resolution TEXT,
            PRIMARY KEY(series_id, episode_id)
        );
        CREATE TABLE IF NOT EXISTS xtream_vod_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, item_id TEXT NOT NULL,
            series_id TEXT, title TEXT NOT NULL, extension TEXT NOT NULL,
            series_title TEXT, season INTEGER, episode INTEGER,
            folder TEXT NOT NULL DEFAULT 'root',
            state TEXT NOT NULL, bytes_done INTEGER NOT NULL DEFAULT 0,
            bytes_total INTEGER, filename TEXT, error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_xtream_vod_jobs_state ON xtream_vod_jobs(state,id);
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(xtream_vod_jobs)")}
    if "folder" not in columns:
        # Jobs queued before folder selection must keep their original root destination.
        conn.execute("ALTER TABLE xtream_vod_jobs ADD COLUMN folder TEXT NOT NULL DEFAULT 'root'")
    for name, kind in (("series_title", "TEXT"), ("season", "INTEGER"), ("episode", "INTEGER")):
        if name not in columns:
            # Earlier episode jobs retain their original flat file destination.
            conn.execute(f"ALTER TABLE xtream_vod_jobs ADD COLUMN {name} {kind}")
    for table in ("xtream_vod_items", "xtream_vod_episodes"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in (("size_bytes", "INTEGER"), ("resolution", "TEXT")):
            if name not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")


def _valid_id(value):
    value = str(value if value is not None else "").strip()
    if not _ID.fullmatch(value):
        raise ValueError("Invalid provider content ID")
    return value


def _extension(value):
    value = str(value or "").lower().lstrip(".")
    return value if value in _EXTENSIONS else None


def _size_bytes(*sources):
    """Accept only byte counts; duration and bitrate are not file sizes."""
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("size_bytes", "file_size", "filesize", "fileSize", "content_length", "contentLength", "size"):
            value = source.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                continue
            value = str(value).strip()
            if len(value) <= 19 and value.isascii() and value.isdecimal():
                count = int(value)
                if 0 < count < 2**63:
                    return count
    return None


def _resolution(*sources):
    """Use explicit dimensions or a provider resolution label, never a title hint."""
    for source in sources:
        if not isinstance(source, dict):
            continue
        video = source.get("video")
        for details in (video, source):
            if not isinstance(details, dict):
                continue
            width, height = details.get("width"), details.get("height")
            if all(isinstance(value, (int, str)) and not isinstance(value, bool)
                   and str(value).isascii() and str(value).isdecimal() for value in (width, height)):
                width, height = int(width), int(height)
                if 128 <= width <= 16384 and 96 <= height <= 8640:
                    return f"{width}×{height}"
            label = details.get("resolution")
            if isinstance(label, str):
                label = label.strip()
                dimensions = re.fullmatch(r"(\d{3,5})\s*[x×]\s*(\d{3,4})", label, re.IGNORECASE)
                if dimensions:
                    width, height = map(int, dimensions.groups())
                    if 128 <= width <= 16384 and 96 <= height <= 8640:
                        return f"{width}×{height}"
                if re.fullmatch(r"(?:480|576|720|1080|1440|2160|4320)p|[248]K", label, re.IGNORECASE):
                    return label.upper() if label.lower().endswith("k") else label.lower()
    return None


class VodProvider:
    """Use the configured account gate and host fallback for deliberate metadata reads."""

    def __init__(self, db_path=None):
        self.db_path = db_path or resolve_db_path()

    def fetch(self, action, **params):
        if action not in {"get_vod_categories", "get_vod_streams", "get_series_categories", "get_series", "get_series_info"}:
            raise ValueError("Unsupported VOD action")
        for value in params.values():
            _valid_id(value)
        with get_conn(self.db_path) as conn:
            configs = load_metadata_configs(conn)
        gate = AccountGate(self.db_path)
        busy = False
        for config in configs:
            try:
                with gate.hold(config, wait_seconds=0.1) as gate_fd:
                    for host_index, host_config in host_configs(config, gate_fd):
                        client = XtreamClient(host_config)
                        try:
                            payload = client._curl_payload(
                                action, category_id=params.get("category_id"),
                                extra_params={k: v for k, v in params.items() if k != "category_id"},
                            )
                            valid = isinstance(payload, dict) if action == "get_series_info" else isinstance(payload, list)
                            if valid:
                                record_host_success(gate_fd, host_index)
                                return payload
                        except XtreamError:
                            pass
                        finally:
                            client.session.close()
            except AccountBusy:
                busy = True
        if busy:
            raise XtreamError("Xtream accounts are occupied; try again after playback or another request finishes")
        raise XtreamError("Provider VOD catalog request failed; saved catalog was kept")


def _categories(rows, kind, accounts):
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            category_id = _valid_id(row.get("category_id"))
        except ValueError:
            continue
        if safe_value(category_id, accounts) != category_id:
            continue
        name = safe_value(str(row.get("category_name") or f"Category {category_id}")[:200], accounts)
        result[category_id] = (kind, category_id, name)
    return result


def _items(rows, kind, categories, accounts):
    result = {}
    id_key = "stream_id" if kind == "movie" else "series_id"
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            item_id = _valid_id(row.get(id_key))
            category_id = _valid_id(row.get("category_id"))
        except ValueError:
            continue
        if category_id not in categories or safe_value(item_id, accounts) != item_id:
            continue
        title = safe_value(str(row.get("name") or "").strip()[:300], accounts)
        if not title:
            continue
        result[item_id] = (kind, item_id, category_id, title, title.casefold(),
                           _extension(row.get("container_extension")) if kind == "movie" else None,
                           _size_bytes(row, row.get("info")) if kind == "movie" else None,
                           _resolution(row, row.get("info")) if kind == "movie" else None)
    return result


def refresh_catalog(provider=None, db_path=None):
    db_path = Path(db_path or resolve_db_path())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        sqlite3.connect(db_path).close()
    provider = provider or VodProvider(db_path)
    with get_conn(db_path) as conn:
        accounts = load_accounts(conn)
    movie_categories = _categories(provider.fetch("get_vod_categories"), "movie", accounts)
    series_categories = _categories(provider.fetch("get_series_categories"), "series", accounts)
    if not movie_categories and not series_categories:
        raise XtreamError("Provider returned no VOD categories; saved catalog was kept")
    def get_items(action, categories, kind):
        if not categories:
            return {}
        rows = provider.fetch(action)
        if not rows or any(isinstance(row, dict) and row.get("category_id") in (None, "") for row in rows):
            rows = [dict(row, category_id=category_id)
                    for category_id in categories for row in provider.fetch(action, category_id=category_id)]
        return _items(rows, kind, categories, accounts)
    movies = get_items("get_vod_streams", movie_categories, "movie")
    series = get_items("get_series", series_categories, "series")
    if (movie_categories and not movies) or (series_categories and not series):
        raise XtreamError("Provider returned an incomplete VOD catalog; saved catalog was kept")
    with get_conn(db_path) as conn, conn:
        ensure_schema(conn)
        conn.execute("DELETE FROM xtream_vod_items")
        conn.execute("DELETE FROM xtream_vod_categories")
        conn.executemany("INSERT INTO xtream_vod_categories VALUES (?,?,?)", [*movie_categories.values(), *series_categories.values()])
        conn.executemany("INSERT INTO xtream_vod_items (kind,item_id,category_id,title,search_title,extension,size_bytes,resolution) VALUES (?,?,?,?,?,?,?,?)", [*movies.values(), *series.values()])
        conn.execute("DELETE FROM xtream_vod_episodes WHERE series_id NOT IN (SELECT item_id FROM xtream_vod_items WHERE kind='series')")
        conn.execute("INSERT INTO xtream_vod_state VALUES (1,datetime('now'),?,?) ON CONFLICT(id) DO UPDATE SET refreshed_at=excluded.refreshed_at,movie_count=excluded.movie_count,series_count=excluded.series_count",
                     (len(movies), len(series)))
    return {"movie_count": len(movies), "series_count": len(series)}


def catalog(conn, *, kind="all", query="", category_id="", limit=50, offset=0):
    if kind not in {"all", "movie", "series"} or not 1 <= limit <= 100 or offset < 0 or len(query) > 200:
        raise ValueError("Invalid catalog filter")
    category_kind = "all"
    if category_id:
        if ":" in category_id:
            category_kind, category_id = category_id.split(":", 1)
            if category_kind not in {"movie", "series"}:
                raise ValueError("Invalid catalog category")
        _valid_id(category_id)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='xtream_vod_items'").fetchone():
        return {"cache": None, "categories": [], "items": [], "total": 0, "limit": limit, "offset": offset}
    state = conn.execute("SELECT * FROM xtream_vod_state WHERE id=1").fetchone()
    where = "WHERE (?='all' OR i.kind=?) AND (?='all' OR i.kind=?) AND (?='' OR i.category_id=?) AND i.search_title LIKE ? ESCAPE '\\'"
    escaped_query = query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    args = (kind, kind, category_kind, category_kind, category_id, category_id, f"%{escaped_query}%")
    total = conn.execute("SELECT count(*) FROM xtream_vod_items i " + where, args).fetchone()[0]
    columns = {row[1] for row in conn.execute("PRAGMA table_info(xtream_vod_items)")}
    details = ",".join(f"i.{name}" if name in columns else f"NULL AS {name}"
                       for name in ("size_bytes", "resolution"))
    items = [dict(r) for r in conn.execute(
        f"SELECT i.kind,i.item_id,i.category_id,i.title,i.extension,{details},c.name AS category_name FROM xtream_vod_items i JOIN xtream_vod_categories c ON c.kind=i.kind AND c.category_id=i.category_id "
        + where + " ORDER BY i.title,i.item_id LIMIT ? OFFSET ?", (*args, limit, offset))]
    categories = [dict(r) for r in conn.execute("SELECT * FROM xtream_vod_categories ORDER BY kind,name")]
    return {"cache": dict(state) if state else None, "categories": categories, "items": items,
            "total": total, "limit": limit, "offset": offset}


def episodes(conn, series_id):
    series_id = _valid_id(series_id)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='xtream_vod_items'").fetchone():
        raise KeyError("Series not found in saved catalog")
    if not conn.execute("SELECT 1 FROM xtream_vod_items WHERE kind='series' AND item_id=?", (series_id,)).fetchone():
        raise KeyError("Series not found in saved catalog")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(xtream_vod_episodes)")}
    details = ",".join(name if name in columns else f"NULL AS {name}"
                       for name in ("size_bytes", "resolution"))
    return [dict(r) for r in conn.execute(
        f"SELECT episode_id,season,episode,title,extension,{details} FROM xtream_vod_episodes WHERE series_id=? ORDER BY season,episode,episode_id", (series_id,))]


def refresh_episodes(series_id, provider=None, db_path=None):
    db_path = db_path or resolve_db_path()
    with get_conn(db_path) as conn:
        episodes(conn, series_id)
        accounts = load_accounts(conn)
    payload = (provider or VodProvider(db_path)).fetch("get_series_info", series_id=series_id)
    seasons = payload.get("episodes")
    if not isinstance(seasons, dict):
        raise XtreamError("Provider returned no episode list; saved episodes were kept")
    entries = {}
    for season_key, rows in seasons.items():
        if not isinstance(rows, list):
            continue
        try:
            season = int(season_key)
        except (TypeError, ValueError):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                episode_id = _valid_id(row.get("id"))
                episode = int(row.get("episode_num") or 0)
            except (ValueError, TypeError):
                continue
            if safe_value(episode_id, accounts) != episode_id:
                continue
            info = row.get("info") if isinstance(row.get("info"), dict) else {}
            ext = _extension(row.get("container_extension") or info.get("container_extension"))
            title = safe_value(str(row.get("title") or f"Episode {episode}")[:300], accounts)
            entries[episode_id] = (series_id, episode_id, season, episode, title, ext,
                                   _size_bytes(row, info), _resolution(row, info))
    if not entries:
        raise XtreamError("Provider returned no usable episodes; saved episodes were kept")
    with get_conn(db_path) as conn, conn:
        ensure_schema(conn)
        conn.execute("DELETE FROM xtream_vod_episodes WHERE series_id=?", (series_id,))
        conn.executemany("INSERT INTO xtream_vod_episodes (series_id,episode_id,season,episode,title,extension,size_bytes,resolution) VALUES (?,?,?,?,?,?,?,?)", entries.values())
    return len(entries)


def download_dir():
    path = Path(os.environ.get("XTREAM_DOWNLOAD_DIR") or "/app/out/xtream-downloads")
    if not path.is_absolute():
        raise ValueError("XTREAM_DOWNLOAD_DIR must be an absolute container path")
    return path


def folder_dir(folder):
    if folder == "root":
        return download_dir()
    names = {"movies": ("XTREAM_MOVIES_FOLDER", "Movies"),
             "tv": ("XTREAM_TV_FOLDER", "TV Shows")}
    if folder not in names:
        raise ValueError("Choose Movies or TV Shows")
    setting, default = names[folder]
    name = os.environ.get(setting, default).strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"{setting} must be one folder name")
    return download_dir() / name


def _media_component(value, limit=120):
    # Keep the provider's title while removing path separators and reserved filename characters.
    clean = re.sub(r'[\x00-\x1f/\\:*?"<>|]', " ", str(value or ""))
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    return clean.encode("utf-8")[:limit].decode("utf-8", errors="ignore").rstrip(" .")


def _plex_episode(job):
    return (job.get("kind") == "episode" and job.get("series_title")
            and isinstance(job.get("season"), int) and job["season"] >= 0
            and isinstance(job.get("episode"), int) and job["episode"] > 0)


def _existing_show_dir(root, show_name):
    exact = root / show_name
    if exact.is_symlink():
        raise OSError("Show folder is a symbolic link")
    if exact.exists():
        if not exact.is_dir():
            raise OSError("Show folder path is not a directory")
        return exact
    if not root.is_dir():
        return exact
    wanted = _PLEX_ID_SUFFIX.sub("", show_name).casefold()
    year_pattern = re.compile(re.escape(wanted) + r" \((?:18|19|20|21)\d{2}\)\Z")
    title_matches = []
    year_matches = []
    for entry in root.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            continue
        existing = _PLEX_ID_SUFFIX.sub("", entry.name).casefold()
        if existing == wanted:
            title_matches.append(entry)
        elif not _YEAR_SUFFIX.search(_PLEX_ID_SUFFIX.sub("", show_name)) and year_pattern.fullmatch(existing):
            year_matches.append(entry)
    matches = title_matches or year_matches
    if len(matches) > 1:
        raise OSError("Multiple existing show folders match this title")
    return matches[0] if matches else exact


def _job_download_dir(job):
    root = folder_dir(job.get("folder", "root"))
    if not _plex_episode(job):
        return root
    show_name = _media_component(job["series_title"])
    if not show_name:
        raise ValueError("Series title cannot be used as a folder name")
    show_dir = _existing_show_dir(root, show_name)
    return show_dir / f"Season {job['season']:02d}"


def _idle_timeout():
    try:
        value = int(os.environ.get("XTREAM_STREAM_IDLE_TIMEOUT", "60"))
    except ValueError:
        return 60
    return value if 10 <= value <= 600 else 60


def list_jobs(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='xtream_vod_jobs'").fetchone():
        return []
    return [dict(r) for r in conn.execute("SELECT * FROM xtream_vod_jobs ORDER BY CASE WHEN state='staged' THEN 0 ELSE 1 END,id DESC LIMIT 100")]


def _saved_download(conn, kind, item_id):
    if not isinstance(kind, str) or kind not in {"movie", "episode"}:
        raise ValueError("Choose a movie or episode")
    item_id = _valid_id(item_id)
    ensure_schema(conn)
    if kind == "movie":
        row = conn.execute("SELECT title,extension,NULL AS series_title,NULL AS season,NULL AS episode FROM xtream_vod_items WHERE kind='movie' AND item_id=?", (item_id,)).fetchone()
        series_id = None
    else:
        row = conn.execute("SELECT e.title,e.extension,e.series_id,e.season,e.episode,i.title AS series_title FROM xtream_vod_episodes e JOIN xtream_vod_items i ON i.kind='series' AND i.item_id=e.series_id WHERE e.episode_id=?", (item_id,)).fetchone()
        series_id = row["series_id"] if row else None
    if not row:
        raise KeyError("Title not found in saved catalog")
    if not row["extension"]:
        raise ValueError("This title has no supported direct-file format")
    if kind == "episode" and (not isinstance(row["season"], int) or row["season"] < 0
                              or not isinstance(row["episode"], int) or row["episode"] < 1
                              or not _media_component(row["series_title"])):
        raise ValueError("Episode needs a valid show title, season, and episode number")
    return item_id, row, series_id


def stage_download(conn, kind, item_id):
    item_id, row, series_id = _saved_download(conn, kind, item_id)
    folder = "movies" if kind == "movie" else "tv"
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT id FROM xtream_vod_jobs WHERE kind=? AND item_id=? AND state IN ('staged','pending','running') ORDER BY CASE WHEN state='staged' THEN 0 ELSE 1 END,id DESC LIMIT 1", (kind, item_id)).fetchone()
        if existing:
            return existing["id"]
        return conn.execute("INSERT INTO xtream_vod_jobs(kind,item_id,series_id,title,extension,series_title,season,episode,folder,state) VALUES (?,?,?,?,?,?,?,?,?,'staged')",
                            (kind, item_id, series_id, row["title"], row["extension"], row["series_title"], row["season"], row["episode"], folder)).lastrowid


def start_staged_download(conn, job_id, folder):
    if not isinstance(folder, str) or folder not in {"movies", "tv"}:
        raise ValueError("Choose Movies or TV Shows")
    folder_dir(folder)
    ensure_schema(conn)
    staged = conn.execute("SELECT kind,item_id,series_title,season,episode FROM xtream_vod_jobs WHERE id=? AND state='staged'", (job_id,)).fetchone()
    if not staged:
        return False
    # Staged episodes from an earlier release need the show metadata before Start.
    if staged["kind"] == "episode" and (not staged["series_title"] or staged["season"] is None or staged["episode"] is None):
        try:
            _, episode, _ = _saved_download(conn, "episode", staged["item_id"])
        except KeyError as exc:
            raise ValueError("Saved episode metadata is unavailable; remove and add it again") from exc
        details = (episode["series_title"], episode["season"], episode["episode"])
    else:
        details = None
    with conn:
        if details:
            result = conn.execute("UPDATE xtream_vod_jobs SET series_title=?,season=?,episode=?,folder=?,state='pending',updated_at=CURRENT_TIMESTAMP WHERE id=? AND state='staged'",
                                  (*details, folder, job_id))
        else:
            result = conn.execute("UPDATE xtream_vod_jobs SET folder=?,state='pending',updated_at=CURRENT_TIMESTAMP WHERE id=? AND state='staged'",
                                  (folder, job_id))
    return result.rowcount > 0


def queue_download(conn, kind, item_id, folder=None):
    item_id, row, series_id = _saved_download(conn, kind, item_id)
    folder = ("movies" if kind == "movie" else "tv") if folder is None else folder
    if not isinstance(folder, str) or folder not in {"movies", "tv"}:
        raise ValueError("Choose Movies or TV Shows")
    folder_dir(folder)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT id,state,filename FROM xtream_vod_jobs WHERE kind=? AND item_id=? AND folder=? AND state IN ('pending','running','cancelling','completed') ORDER BY id DESC LIMIT 1", (kind, item_id, folder)).fetchone()
        if existing and (existing["state"] != "completed" or (existing["filename"] and Path(existing["filename"]).is_file())):
            return existing["id"]
        return conn.execute("INSERT INTO xtream_vod_jobs(kind,item_id,series_id,title,extension,series_title,season,episode,folder,state) VALUES (?,?,?,?,?,?,?,?,?,'pending')",
                            (kind, item_id, series_id, row["title"], row["extension"], row["series_title"], row["season"], row["episode"], folder)).lastrowid


def cancel_job(conn, job_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='xtream_vod_jobs'").fetchone():
        return False
    with conn:
        removed = conn.execute("DELETE FROM xtream_vod_jobs WHERE id=? AND state='staged'", (job_id,))
        if removed.rowcount:
            return True
        result = conn.execute("UPDATE xtream_vod_jobs SET state=CASE WHEN state='running' THEN 'cancelling' ELSE 'cancelled' END,updated_at=CURRENT_TIMESTAMP WHERE id=? AND state IN ('pending','running')", (job_id,))
    return result.rowcount > 0


def _filename(job):
    if _plex_episode(job):
        marker = f" - s{job['season']:02d}e{job['episode']:02d}"
        tag = f" [xtream-{job['item_id']}-{job['id']}].{job['extension']}"
        budget = 240 - len((marker + tag).encode("utf-8"))
        show = _media_component(job["series_title"], min(100, budget // 2))
        title = _media_component(job["title"], budget - len(show.encode("utf-8")) - 3)
        suffix = f" - {title}" if title else ""
        return f"{show}{marker}{suffix}{tag}"
    stem = re.sub(r"[^\w .()-]+", "", job["title"], flags=re.UNICODE).strip(" .")[:100] or "Download"
    return f"{stem} [{job['kind']}-{job['item_id']}-{job['id']}].{job['extension']}"


def _media_url(config, job):
    section = "movie" if job["kind"] == "movie" else "series"
    return (f"{config.server_url}/{section}/{quote(config.username, safe='')}/"
            f"{quote(config.password, safe='')}/{quote(job['item_id'], safe='')}.{job['extension']}")


def _set_job(db_path, job_id, *, expected=None, **fields):
    with get_conn(db_path) as conn, conn:
        result = conn.execute("UPDATE xtream_vod_jobs SET " + ",".join(f"{key}=?" for key in fields)
                     + ",updated_at=CURRENT_TIMESTAMP WHERE id=?" + (" AND state=?" if expected else ""),
                     (*fields.values(), job_id, *((expected,) if expected else ())))
        return result.rowcount > 0


def _cancelled(db_path, job_id):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT state FROM xtream_vod_jobs WHERE id=?", (job_id,)).fetchone()
        return not row or row[0] in {"cancelling", "cancelled"}


def _run_job(db_path, job, pool):
    try:
        destination = _job_download_dir(job)
        destination.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as exc:
        _set_job(db_path, job["id"], expected="pending", state="failed", error=f"Destination unavailable: {exc}")
        return
    name = _filename(job)
    partial = destination / (name + ".part")
    final = destination / name
    lease = None
    response = None
    session = None
    outcome = "upstream_error"
    try:
        if not any(account["available"] > 0 for account in pool.status()["accounts"]):
            pool.check_accounts(due_only=True)
        lease = pool.acquire(job["item_id"], "vod_download")
        if not _set_job(db_path, job["id"], expected="pending", state="running", bytes_done=0, bytes_total=None, error=None):
            outcome = "client_closed"
            return
        if partial.exists():
            partial.unlink()
        for host_index, config in host_configs(lease.account.config, lease.gate_fd):
            session = configure_session(requests.Session())
            try:
                response = session.get(_media_url(config, job), stream=True, timeout=(10, _idle_timeout()),
                                       headers={"Accept-Encoding": "identity"})
                if response.status_code in {401, 403}:
                    response.close()
                    response = None
                    source = CurlStream(_media_url(config, job), _idle_timeout(), lease.fd, lease.gate_fd)
                    chunks = source.chunks()
                    total = None
                elif response.status_code == 200:
                    source = response
                    chunks = response.iter_content(chunk_size=_CHUNK)
                    total_header = response.headers.get("Content-Length")
                    total = int(total_header) if total_header and total_header.isdigit() else None
                else:
                    continue
                if total is not None and total <= 0:
                    raise OSError("empty")
                _set_job(db_path, job["id"], bytes_total=total)
                done = 0
                last_update = time.monotonic()
                try:
                    with partial.open("xb") as output:
                        os.chmod(partial, 0o600)
                        for chunk in chunks:
                            if _cancelled(db_path, job["id"]):
                                outcome = "client_closed"
                                return
                            if not chunk:
                                continue
                            if done == 0 and chunk.lstrip().lower().startswith((b"#extm3u", b"<html", b"<!doctype", b"{", b"[")):
                                raise OSError("provider did not return a direct media file")
                            output.write(chunk)
                            done += len(chunk)
                            if time.monotonic() - last_update >= 0.5:
                                _set_job(db_path, job["id"], bytes_done=done)
                                last_update = time.monotonic()
                        output.flush()
                        os.fsync(output.fileno())
                finally:
                    if source is not response:
                        source.close()
                if not done or (total is not None and done != total):
                    raise OSError("incomplete media file")
                with get_conn(db_path) as conn, conn:
                    conn.execute("BEGIN IMMEDIATE")
                    state = conn.execute("SELECT state FROM xtream_vod_jobs WHERE id=?", (job["id"],)).fetchone()
                    if not state or state[0] != "running":
                        outcome = "client_closed"
                        return
                    os.replace(partial, final)
                    conn.execute("UPDATE xtream_vod_jobs SET state='completed',bytes_done=?,filename=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                 (done, str(final), job["id"]))
                record_host_success(lease.gate_fd, host_index)
                outcome = "upstream_eof"
                return
            finally:
                if response is not None:
                    response.close()
                    response = None
                session.close()
                session = None
        raise OSError("provider did not return a direct media file")
    except PoolUnavailable:
        # A live stream or metadata request owns the account; leave the job queued.
        return
    except Exception:
        _set_job(db_path, job["id"], expected="running", state="failed", error="Download failed; check provider access, file format, and NAS space")
    finally:
        if response is not None:
            response.close()
        if session is not None:
            session.close()
        try:
            if lease is not None:
                lease.release(outcome)
        finally:
            partial.unlink(missing_ok=True)
            _set_job(db_path, job["id"], expected="cancelling", state="cancelled")


def run_worker(db_path=None, *, stop_event=None):
    db_path = Path(db_path or resolve_db_path())
    lock_path = db_path.with_name(db_path.name + ".vod-worker.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if not db_path.exists():
            return
        with get_conn(db_path) as conn, conn:
            ensure_schema(conn)
            abandoned = [dict(row) for row in conn.execute("SELECT * FROM xtream_vod_jobs WHERE state='cancelling'")]
            for job in abandoned:
                try:
                    (_job_download_dir(job) / (_filename(job) + ".part")).unlink(missing_ok=True)
                except (OSError, ValueError):
                    # Do not guess which ambiguous show directory owns the partial file.
                    pass
            conn.execute("UPDATE xtream_vod_jobs SET state='cancelled' WHERE state='cancelling'")
            conn.execute("UPDATE xtream_vod_jobs SET state='pending',bytes_done=0,bytes_total=NULL,error=NULL WHERE state='running'")
        while stop_event is None or not stop_event.is_set():
            try:
                with get_conn(db_path) as conn:
                    job = conn.execute("SELECT * FROM xtream_vod_jobs WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
                    job = dict(job) if job else None
                if job:
                    try:
                        _run_job(db_path, job, XtreamPool(db_path))
                    except Exception:
                        if not _set_job(db_path, job["id"], expected="running", state="failed", error="Download worker failed"):
                            _set_job(db_path, job["id"], expected="pending", state="failed", error="Download worker failed")
            except sqlite3.Error:
                # A refresh may briefly own the writer; keep the queue alive.
                pass
            if stop_event is not None:
                stop_event.wait(5)
            else:
                time.sleep(5)
    finally:
        os.close(fd)


def ensure_worker():
    global _worker
    with _worker_guard:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=run_worker, name="xtream-vod-downloads", daemon=True)
            _worker.start()
