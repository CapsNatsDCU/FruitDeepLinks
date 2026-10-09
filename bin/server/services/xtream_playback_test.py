"""Explicit, account-pinned playback tests using bounded media workers."""
from __future__ import annotations

from xtream_accounts import safe_value
from xtream_ingest import XtreamError
from xtream_pool import PoolUnavailable
from server.services.xtream_persistent import list_channels, normalize_extension
from server.services.xtream_channel_cache import all_channels
from server.services.xtream_quality import _probe_bytes, _sample_media


def _saved_channel(pool):
    with pool.connection() as conn:
        persistent = list_channels(conn, enabled_only=True)
        candidates = [c for c in persistent if c["availability_status"] == "available"]
        candidates += [c for c in persistent if c["availability_status"] != "available"]
        if not candidates:
            candidates = all_channels(conn)
    if not candidates:
        return None
    channel = candidates[0]
    return {"stream_id": str(channel["stream_id"]),
            "category_id": str(channel["category_id"]),
            "name": channel.get("display_name") or channel.get("name"),
            "extension": channel.get("stream_extension") or channel.get("container_extension") or "ts"}


def _discover_channel(pool, lease):
    # The lease already owns this account's provider gate. Use only its
    # credentials; account failover here would hide a broken tested account.
    client = pool.client_factory(lease.account.config)
    try:
        categories = lease.account.config.category_ids
        if not categories:
            categories = tuple(str(c["category_id"]) for c in client.get_live_categories()
                               if c.get("category_id") is not None)
        if not categories:
            raise XtreamError("No provider category is available for a playback test")
        channels = client.get_live_streams(categories[0])
        channel = next((c for c in channels if c.get("stream_id") is not None), None)
        if channel is None:
            raise XtreamError("No channel is available in the test category")
        return {"stream_id": str(channel["stream_id"]), "name": channel.get("name"),
                "category_id": categories[0],
                "extension": normalize_extension(channel.get("container_extension"))}
    finally:
        session = getattr(client, "session", None)
        if session is not None:
            session.close()


def test_account_playback(pool, account_id=None):
    if account_id is not None and account_id not in {a.id for a in pool.accounts}:
        raise XtreamError("Xtream account not found")
    saved = _saved_channel(pool)
    skipped, checks = {}, {}
    for account in pool.accounts:
        if account_id is not None and account.id != account_id:
            continue
        state = next(a for a in pool.status()["accounts"] if a["id"] == account.id)
        if not state["enabled"]:
            skipped[account.id] = "disabled"
            continue
        try:
            lease = pool.acquire(saved["stream_id"] if saved else "manual-test",
                                 "manual_playback_test", account_id=account.id)
        except PoolUnavailable:
            skipped[account.id] = "occupied"
            continue
        outcome = "upstream_error"
        channel = saved
        try:
            if channel is None:
                channel = _discover_channel(pool, lease)
                lease.stream_id = channel["stream_id"]
                with pool.connection() as conn:
                    conn.execute("UPDATE xtream_leases SET stream_id=? WHERE lease_id=?",
                                 (lease.stream_id, lease.id))
            sample = _sample_media(lease, channel["stream_id"], channel["extension"],
                                   require_quiet=False, sample_seconds=2)
            # The sample worker has closed all provider transports. Keep the
            # reservation through local validation to serialize the result write.
            video = _probe_bytes(sample)
            pool.record_media_success(lease)
            from server.services.xtream_playback_quality import save_playback_quality
            save_playback_quality(pool.db_path, channel["stream_id"], channel.get("category_id"), video)
            outcome = "client_closed"
            checks[account.id] = {"status": "passed", "channel": channel, "video": video}
        except TimeoutError:
            outcome = "upstream_timeout"
            checks[account.id] = {"status": "failed", "channel": channel,
                                  "message": "Playback sample timed out"}
        except Exception:
            # Provider exceptions can contain authenticated URLs. Never return
            # exception text, or substitute another account after a failure.
            checks[account.id] = {"status": "failed", "channel": channel,
                                  "message": "Playback could not be verified on the test channel"}
        finally:
            lease.release(outcome)
    return safe_value({**pool.status(), "checks_skipped": skipped, "playback_checks": checks}, pool.accounts)
