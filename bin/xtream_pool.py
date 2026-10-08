"""Atomic account allocation across threads/workers on one Fruit host.

SQLite serializes reservations. Each reservation owns a kernel flock for its
entire socket lifetime. A crashed process loses the lock, so stale leases can
be reclaimed without age-based guesses or killing long recordings.
"""
from __future__ import annotations

import fcntl
import math
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from xtream_accounts import Account, capacity, load_accounts, safe_value
from xtream_gate import AccountBusy, AccountGate
from xtream_activity import mark_normal_activity
from xtream_ingest import XtreamClient, XtreamError
from xtream_pool_schema import ensure_schema


class PoolUnavailable(XtreamError):
    pass


def scheduler_capacity(conn):
    """Return the current pool limit without network calls or DB mutations.

An absent pool preserves the old provider_capacities behavior. Once accounts
are checked, the derived sum supersedes the historical single-account limit.
"""
    try:
        rows = {r[0]: r for r in conn.execute("SELECT account_id,fingerprint,enabled_override,capacity_override,discovered_capacity,health FROM xtream_account_state")}
    except sqlite3.OperationalError:
        return None
    accounts = load_accounts(conn)
    if not accounts:
        return None
    total = 0
    for account in accounts:
        row = rows.get(account.id)
        if (row and row[1] == account.fingerprint and account.enabled and account.config.enabled
                and row[2] != 0 and row[5] in {"healthy", "degraded"}):
            requested = row[3] or account.capacity_override or row[4] or 1
            # The account gate serializes metadata and media together, so the
            # implemented local capacity is one even if the provider reports more.
            total += min(requested, row[4] or requested, 1)
    return total


@dataclass
class Lease:
    pool: "XtreamPool" = field(repr=False)
    id: str
    account: Account = field(repr=False)
    stream_id: str
    source: str
    started: float
    fd: int = field(repr=False)
    gate_fd: int = field(repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def release(self, outcome: str = "client_closed") -> None:
        with self._lock:
            if self.fd < 0:
                return
            fd, self.fd = self.fd, -1
            gate_fd, self.gate_fd = self.gate_fd, -1
            self.pool.finish(self, outcome, fd, gate_fd)


class XtreamPool:
    def __init__(self, db_path, environ=None, client_factory=None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_dir = self.db_path.parent / (self.db_path.name + ".xtream-locks")
        self.lock_dir.mkdir(mode=0o700, exist_ok=True)
        self.gate = AccountGate(self.db_path)
        self.client_factory = client_factory or XtreamClient
        with self.connection() as conn:
            ensure_schema(conn)
            self.accounts = load_accounts(conn, environ)
            self.enabled = bool(self.accounts and self.accounts[0].config.enabled)
            for account in self.accounts:
                conn.execute("INSERT OR IGNORE INTO xtream_account_state(account_id,fingerprint) VALUES(?,?)",
                             (account.id, account.fingerprint))
                conn.execute("UPDATE xtream_account_state SET fingerprint=?,health='unknown',discovered_capacity=NULL,"
                             "last_checked=NULL,last_success=NULL,last_media_success=NULL,last_error=NULL,retry_after=0,exclusive_for_background=0 "
                             "WHERE account_id=? AND fingerprint<>?",
                             (account.fingerprint, account.id, account.fingerprint))

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _state(self, conn, account):
        row = dict(conn.execute("SELECT * FROM xtream_account_state WHERE account_id=?", (account.id,)).fetchone())
        if row["fingerprint"] != account.fingerprint:
            row["health"] = "unknown"
        row.update(id=account.id, label=safe_value(row["label_override"] or account.label, self.accounts),
                   enabled=self.enabled and account.enabled and row["enabled_override"] != 0)
        row["configured_override"] = row["capacity_override"] if row["capacity_override"] is not None else account.capacity_override
        requested = row["configured_override"] or row["discovered_capacity"] or 1
        provider_capacity = min(requested, row["discovered_capacity"] or requested)
        row["effective_capacity"] = min(provider_capacity, 1)
        row["capacity_source"] = ("local_serialization" if provider_capacity > 1
                                  else "provider_limit" if row["discovered_capacity"] and requested > row["discovered_capacity"]
                                  else "override" if row["configured_override"] else "discovered" if row["discovered_capacity"]
                                  else "conservative_default")
        return row

    def check_accounts(self, account_id=None, *, due_only=False):
        """Explicit health check or bounded tune-time recovery; never logs URLs."""
        if account_id and account_id not in {a.id for a in self.accounts}:
            raise XtreamError("Xtream account not found")
        skipped = {}
        checked_hosts = {}
        for account in self.accounts:
            if account_id and account.id != account_id:
                continue
            with self.connection() as conn:
                state = self._state(conn, account)
            if not state["enabled"]:
                skipped[account.id] = "disabled"
                continue
            now = time.time()
            # Authentication failures retry automatically after five minutes;
            # transient failures recover after 30 seconds or explicit Test.
            next_check = max(state["retry_after"], (state["last_checked"] or 0) + (300 if state["health"] in {"healthy", "unhealthy"} else 30))
            if due_only and now < next_check:
                continue
            try:
                gate_fd = self.gate.acquire(account.config)
            except AccountBusy:
                # A health check must never contact an account during media or
                # another provider operation, or replace its cached health.
                skipped[account.id] = "occupied"
                continue
            with self.connection() as conn:
                if not self._state(conn, account)["enabled"]:
                    os.close(gate_fd)
                    skipped[account.id] = "disabled"
                    continue
            client = None
            try:
                client = self.client_factory(account.config)
                client.host_gate_fd = gate_fd
                maximum = capacity(client.get_account_max_connections())
                check = getattr(client, "last_account_check", None) or {
                    "health": "healthy" if maximum else "unreachable", "error": None if maximum else "Provider account check failed"}
                checked_hosts[account.id] = check.get("host_route", "configured" if check.get("health") == "healthy" else "configured_failed")
                health = check["health"] if check.get("health") in {"healthy", "unhealthy", "unreachable"} else "unreachable"
                error = None if health == "healthy" else (
                    check.get("error") or ("Account authentication or subscription rejected"
                                           if health == "unhealthy" else "Provider account check failed")
                )
            except Exception:
                maximum, health, error = None, "unreachable", "Provider account check failed"
            finally:
                if client is not None and hasattr(client, "session"):
                    try:
                        client.session.close()
                    except Exception:
                        pass
                os.close(gate_fd)
            with self.connection() as conn:
                # A transient metadata outage must not invalidate credentials
                # that were previously authenticated successfully. Metadata
                # failures must not put media on cooldown: the live stream may
                # still work when player_api.php is unavailable or rejected.
                # Preserve a cooldown caused by an actual failed media tune;
                # an explicit successful account test may clear it.
                if health == "unreachable" and (state["last_success"] or state["last_media_success"]):
                    health = "degraded"
                conn.execute("UPDATE xtream_account_state SET discovered_capacity=COALESCE(?,discovered_capacity),"
                             "health=?,last_checked=?,last_success=CASE WHEN ?='healthy' THEN ? ELSE last_success END,"
                             "last_error=?,retry_after=CASE WHEN ?='healthy' AND ?=0 THEN 0 ELSE retry_after END "
                             "WHERE account_id=? AND fingerprint=?",
                             (maximum, health, now, health, now, error, health, int(due_only), account.id, account.fingerprint))
        result = self.status()
        result["checks_skipped"] = skipped
        result["checked_hosts"] = checked_hosts
        return result

    def update(self, account_id, payload):
        if not isinstance(payload, dict) or set(payload) - {"label", "enabled", "capacity_override", "reserved_for_fruit"}:
            raise XtreamError("Only label, enabled, capacity_override and reserved_for_fruit can be edited here")
        account = next((a for a in self.accounts if a.id == account_id), None)
        if account is None:
            raise XtreamError("Xtream account not found")
        fields = {}
        if "enabled" in payload:
            if not isinstance(payload["enabled"], bool):
                raise XtreamError("Enabled must be true or false")
            if payload["enabled"] and not account.enabled:
                raise XtreamError("Enable this account in deployment configuration first")
            fields["enabled_override"] = int(payload["enabled"])
        if "capacity_override" in payload:
            fields["capacity_override"] = capacity(payload["capacity_override"])
        if "reserved_for_fruit" in payload:
            if not isinstance(payload["reserved_for_fruit"], bool):
                raise XtreamError("Reserved for Fruit must be true or false")
            fields["exclusive_for_background"] = int(payload["reserved_for_fruit"])
        if "label" in payload:
            label = payload["label"]
            if not isinstance(label, str) or len(label) > 100 or any(ord(c) < 32 for c in label):
                raise XtreamError("Label must be at most 100 printable characters")
            fields["label_override"] = safe_value(label.strip(), self.accounts) or None
        if fields:
            gate_fd = -1
            if "enabled_override" in fields or "exclusive_for_background" in fields:
                try:
                    gate_fd = self.gate.acquire(account.config)
                except AccountBusy:
                    raise XtreamError("Account is in use; stop its stream or wait for the provider request, then retry") from None
            try:
                with self.connection() as conn:
                    conn.execute("UPDATE xtream_account_state SET " + ",".join(f"{k}=?" for k in fields) + " WHERE account_id=?", (*fields.values(), account_id))
            finally:
                if gate_fd >= 0:
                    os.close(gate_fd)
        return self.status()

    def _lock_path(self, lease_id):
        return self.lock_dir / lease_id

    def _reap(self, conn):
        for row in conn.execute("SELECT * FROM xtream_leases").fetchall():
            path = self._lock_path(row["lease_id"])
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                conn.execute("DELETE FROM xtream_leases WHERE lease_id=?", (row["lease_id"],))
                self._history(conn, row["account_id"], row["stream_id"], row["source"], row["started"], "worker_stopped")
                if row["source"] != "quality_probe":
                    mark_normal_activity(self.db_path)
                path.unlink(missing_ok=True)
            finally:
                os.close(fd)

    def acquire(self, stream_id, source, *, excluded=(), account_id=None) -> Lease:
        manual_test = source == "manual_playback_test" and account_id is not None
        if source == "manual_playback_test" and not manual_test:
            raise XtreamError("Manual playback tests require a specific account")
        if account_id is not None and account_id not in {a.id for a in self.accounts}:
            raise XtreamError("Xtream account not found")
        if source != "quality_probe":
            mark_normal_activity(self.db_path)
        if not self.enabled:
            raise PoolUnavailable("Xtream is disabled or no accounts are configured")
        if source != "quality_probe":
            # A normal tune has priority over an optional resolution sample.
            # Give the sampler a short chance to see the activity marker,
            # close its child, and release its account before allocating.
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                status = self.status()
                probing = any(row["source"] == "quality_probe" for row in status["leases"])
                other_slot = any(row["id"] not in excluded and row["available"] > 0
                                 for row in status["accounts"])
                if not probing or other_slot:
                    break
                time.sleep(0.05)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._reap(conn)
            live = conn.execute("SELECT account_id,fingerprint FROM xtream_leases").fetchall()
            candidates = []
            for account in self.accounts:
                if account_id is not None and account.id != account_id:
                    continue
                state = self._state(conn, account)
                active = sum(row["account_id"] == account.id or row["fingerprint"] == account.fingerprint for row in live)
                # Optional diagnostics cannot recover or sample degraded accounts.
                # Enforce this inside the allocation transaction if health changed
                # after the route's preflight. Playback retains its fallback.
                allowed_health = {"healthy"} if source == "quality_probe" else {"healthy", "degraded"}
                if (account.id not in excluded and state["enabled"]
                        and (manual_test or state["health"] in allowed_health)
                        and (manual_test or state["retry_after"] <= time.time())
                        and active < state["effective_capacity"]):
                    # A sequential quality scan must not hammer the first
                    # account alphabetically.  Reuse the least recently
                    # released account after balancing active capacity.
                    last_used = conn.execute(
                        "SELECT COALESCE(MAX(ended), 0) FROM xtream_stream_history WHERE account_id=?",
                        (account.id,),
                    ).fetchone()[0]
                    candidates.append((active / state["effective_capacity"], last_used, account.id, account, state))
            gate_fd = -1
            selected = None
            for candidate in sorted(candidates, key=lambda item: item[:3]):
                try:
                    gate_fd = self.gate.acquire(candidate[3].config)
                except AccountBusy:
                    continue
                selected = candidate
                break
            if selected is None:
                self._history(conn, None, str(stream_id), source, None, "capacity_unavailable")
                # Commit diagnostics before raising, rather than rolling back.
                conn.commit()
                raise PoolUnavailable("All Xtream capacity is occupied or unavailable")
            _, _, _, account, state = selected
            lease_id, started = uuid.uuid4().hex, time.time()
            try:
                fd = os.open(self._lock_path(lease_id), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except BaseException:
                os.close(gate_fd)
                raise
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                conn.execute("INSERT INTO xtream_leases VALUES(?,?,?,?,?,?)", (lease_id, account.id, str(stream_id), source, started, account.fingerprint))
                conn.commit()
            except BaseException:
                os.close(fd)
                os.close(gate_fd)
                self._lock_path(lease_id).unlink(missing_ok=True)
                raise
        self._log(f'Allocated account "{state["label"]}" to stream {safe_value(str(stream_id), self.accounts)}')
        return Lease(self, lease_id, replace(account, label=state["label"]), str(stream_id), source, started, fd, gate_fd)

    def _history(self, conn, account_id, stream_id, source, started, outcome):
        conn.execute("INSERT INTO xtream_stream_history(account_id,stream_id,source,started,ended,outcome) VALUES(?,?,?,?,?,?)",
                     (account_id, safe_value(stream_id, self.accounts), source, started, time.time(), outcome))
        conn.execute("DELETE FROM xtream_stream_history WHERE id NOT IN (SELECT id FROM xtream_stream_history ORDER BY id DESC LIMIT 100)")

    def finish(self, lease, outcome, fd, gate_fd):
        allowed = {"client_closed", "upstream_eof", "upstream_error", "upstream_timeout", "tune_failed", "authentication_failed", "unsupported_transport"}
        outcome = outcome if outcome in allowed else "upstream_error"
        child_holds_lock = False
        try:
            with self.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                # Close the parent's lock while the database transaction still
                # protects the row. Curl/FFmpeg may retain its inherited lock.
                os.close(fd)
                fd = -1
                lock_fd = os.open(self._lock_path(lease.id), os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        child_holds_lock = True
                    else:
                        conn.execute("DELETE FROM xtream_leases WHERE lease_id=?", (lease.id,))
                        self._history(conn, lease.account.id, lease.stream_id, lease.source, lease.started, outcome)
                        self._lock_path(lease.id).unlink(missing_ok=True)
                finally:
                    os.close(lock_fd)
        except sqlite3.Error:
            self._log("Lease database cleanup deferred until next allocation")
        finally:
            # If SQLite failed before the transaction started, the next pool
            # read will reclaim the row once this lock closes.
            if fd >= 0:
                os.close(fd)
            try:
                if lease.source != "quality_probe":
                    mark_normal_activity(self.db_path)
            finally:
                os.close(gate_fd)
        if child_holds_lock:
            self._log("Xtream media child still holds a stream reservation")
        else:
            self._log(f'Released account "{safe_value(lease.account.label, self.accounts)}" from stream {safe_value(lease.stream_id, self.accounts)} ({outcome})')

    def record_media_success(self, lease):
        """Record playable tune evidence without enabling background probes.

        Media success does not establish that the account API is healthy. Match
        the credential fingerprint so an old worker cannot update rotated secrets.
        """
        with self.connection() as conn:
            conn.execute("UPDATE xtream_account_state SET last_media_success=?,retry_after=0,"
                         "health=CASE WHEN health IN ('unknown','unreachable','unhealthy') THEN 'degraded' ELSE health END "
                         "WHERE account_id=? AND fingerprint=?",
                         (time.time(), lease.account.id, lease.account.fingerprint))

    def fail_account(self, account_id, *, authentication=False):
        account = next((a for a in self.accounts if a.id == account_id), None)
        if account is None:
            return
        now = time.time()
        with self.connection() as conn:
            conn.execute("UPDATE xtream_account_state SET health=?,last_error=?,retry_after=?,last_checked=? WHERE account_id=? AND fingerprint=?",
                         ("unhealthy" if authentication else "degraded", "Stream authentication rejected" if authentication else "Upstream tune failed",
                          now + (300 if authentication else 30), now, account_id, account.fingerprint))

    def status(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._reap(conn)
            leases = [dict(row) for row in conn.execute("SELECT * FROM xtream_leases ORDER BY started")]
            accounts = []
            now = time.time()
            for account in self.accounts:
                state = self._state(conn, account)
                active = sum(row["account_id"] == account.id or row["fingerprint"] == account.fingerprint for row in leases)
                usable = state["enabled"] and state["health"] in {"healthy", "degraded"}
                try:
                    gate_fd = self.gate.acquire(account.config)
                except AccountBusy:
                    busy = True
                else:
                    busy = False
                    os.close(gate_fd)
                retry_seconds = max(0, math.ceil(state["retry_after"] - now))
                available = max(0, state["effective_capacity"] - active) if usable and not busy and not retry_seconds else 0
                if not state["enabled"]:
                    reason = "disabled"
                elif active >= state["effective_capacity"]:
                    reason = "stream_in_use"
                elif busy:
                    reason = "provider_request_in_progress"
                elif retry_seconds:
                    reason = "cooldown"
                elif not usable:
                    reason = state["health"]
                else:
                    reason = "ready_degraded" if state["health"] == "degraded" else "ready"
                keys = ("id", "label", "enabled", "health", "discovered_capacity", "configured_override", "effective_capacity", "capacity_source", "last_checked", "last_success", "last_media_success", "last_error")
                accounts.append({**{key: state[key] for key in keys}, "reserved_for_fruit": bool(state["exclusive_for_background"]),
                                 "active": active, "available": available, "busy": busy,
                                 "availability_reason": reason, "retry_after_seconds": retry_seconds,
                                 "capacity": state["effective_capacity"] if usable else 0})
            history = [dict(row) for row in conn.execute("SELECT * FROM xtream_stream_history ORDER BY id DESC LIMIT 30")]
        labels = {row["id"]: row["label"] for row in accounts}
        fingerprint_labels = {a.fingerprint: labels[a.id] for a in self.accounts}
        for lease in leases:
            fingerprint = lease.pop("fingerprint", "")
            lease["account_label"] = labels.get(lease["account_id"], fingerprint_labels.get(fingerprint, "Removed account"))
            lease["age_seconds"] = round(max(0, time.time() - lease["started"]), 1)
        total = sum(row["capacity"] for row in accounts)
        return safe_value({"provider": "xtream", "enabled": self.enabled, "capacity": total, "active": len(leases),
                           "available": sum(row["available"] for row in accounts), "accounts": accounts,
                           "leases": leases, "recent_streams": history}, self.accounts)

    @staticmethod
    def _log(message):
        from server.logging_setup import log
        log(message, "INFO")
