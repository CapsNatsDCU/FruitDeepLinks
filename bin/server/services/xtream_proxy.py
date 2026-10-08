"""Bounded-memory, one-upstream-per-client Xtream proxy for Channels DVR."""
from __future__ import annotations

import os
import threading

import requests
from flask import Response, request

from db.connection import resolve_db_path
from xtream_curl import CurlStream
from xtream_hls import HLSStream
from xtream_ingest import build_stream_url
from xtream_hosts import host_configs, record_host_success
from xtream_pool import PoolUnavailable, XtreamPool
from xtream_transport import configure_session


def _timeout():
    try:
        return max(10, min(600, float(os.getenv("XTREAM_STREAM_IDLE_TIMEOUT", "60"))))
    except ValueError:
        return 60


def _close(resource):
    if resource is not None:
        try:
            resource.close()
        except Exception:
            # Closing a failed transport may raise an authenticated URL.
            pass


class OwnedStream:
    """WSGI close works even when the body iterator was never started."""
    def __init__(self, lease, upstream, session, chunks, first):
        self.lease, self.upstream, self.session = lease, upstream, session
        self.chunks, self.first = chunks, first
        self.outcome = "client_closed"
        self.closed = False
        self.lock = threading.Lock()

    def __iter__(self):
        try:
            yield self.first
            yield from self.chunks
            self.outcome = "upstream_eof"
        except GeneratorExit:
            raise
        except (requests.Timeout, TimeoutError):
            self.outcome = "upstream_timeout"
        except Exception:
            # Never allow Werkzeug to log requests/FFmpeg exception strings.
            self.outcome = "upstream_error"
        finally:
            self.close()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            _close(self.upstream)
            _close(self.session)
            self.lease.release(self.outcome)


def _failure(message, status=503):
    return Response(message + "\n", status=status, mimetype="text/plain",
                    headers={"Cache-Control": "no-store", "Retry-After": "5"})


def proxy_stream(stream_id, source, extension="ts", *, pool=None):
    """Return MPEG-TS, retaining a lease until both sides have been closed.

    HEAD is a local availability probe and never opens provider media. No
    downstream headers/cookies or provider redirects/headers are forwarded.
    """
    try:
        pool = pool or XtreamPool(resolve_db_path())
        if request.method == "HEAD":
            state = pool.status()
            return Response(status=200 if state["enabled"] else 503, content_type="video/mp2t",
                            headers={"Cache-Control": "no-store"})
        # A known usable account can serve media even when player_api.php is
        # timing out. Do not delay every tune with sequential metadata checks;
        # use them to bootstrap or recover only when no cached slot is usable.
        if not any(account["available"] > 0 for account in pool.status()["accounts"]):
            pool.check_accounts(due_only=True)
    except Exception:
        return _failure("Xtream pool is unavailable; check Settings")

    excluded = set()
    last_status = 503
    while len(excluded) < len(pool.accounts):
        try:
            lease = pool.acquire(stream_id, source, excluded=excluded)
        except PoolUnavailable:
            return _failure("All Xtream capacity is occupied or unavailable", last_status)
        except Exception:
            return _failure("Xtream capacity reservation failed")
        excluded.add(lease.account.id)
        outcome = "tune_failed"
        account_failure = False
        for host_index, host_config in host_configs(lease.account.config, lease.gate_fd):
            session, upstream = None, None
            authentication = False
            validated_media = False
            try:
                from xtream_logging import protect_http_logs
                protect_http_logs(host_config)
                session = configure_session(requests.Session())
                # This provider's root-path .ts endpoint redirects to MPEG-TS,
                # even when catalogue metadata advertises m3u8.
                url = build_stream_url(host_config, stream_id, "ts")
                upstream = session.get(url, stream=True, timeout=(10, _timeout()),
                                       headers={"Accept-Encoding": "identity"}, allow_redirects=True)
                authentication = upstream.status_code in {401, 403}
                if authentication:
                    # Some providers accept these credentials through curl but
                    # reject Python's HTTP client for the same live URL.
                    _close(upstream)
                    upstream = CurlStream(url, _timeout(), lease.fd, lease.gate_fd)
                    chunks = iter(upstream.chunks())
                    first = next(chunks, b"")
                    use_hls = first.lstrip().startswith(b"#EXTM3U")
                else:
                    use_hls = upstream.status_code in {404, 415} and str(extension).lower() == "m3u8"
                if not authentication and not use_hls:
                    upstream.raise_for_status()
                    chunks = iter(upstream.iter_content(chunk_size=188 * 64))
                    first = next(chunks, b"")
                    if first.lstrip().startswith(b"#EXTM3U"):
                        use_hls = True
                if use_hls:
                    _close(upstream)
                    upstream = HLSStream(build_stream_url(host_config, stream_id, "m3u8"), _timeout(), lease.fd, lease.gate_fd)
                    chunks = iter(upstream.chunks())
                    first = next(chunks, b"")
                if not first or first[0] != 0x47 or (len(first) > 188 and first[188] != 0x47):
                    outcome = "unsupported_transport"
                    raise OSError("Upstream returned no playable media")
                validated_media = True
                record_host_success(lease.gate_fd, host_index)
                body = OwnedStream(lease, upstream, session, chunks, first)
                response = Response(body, content_type="video/mp2t", headers={
                    "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
                response.call_on_close(body.close)
                return response
            except Exception as error:
                last_status = 502
                account_failure |= (
                    isinstance(error, (requests.ConnectionError, requests.Timeout, TimeoutError))
                    or getattr(upstream, "status_code", None) in {429, 500, 502, 503, 504}
                    or (authentication and not validated_media)
                )
                # Never open the other host until this socket/process closes.
                _close(upstream)
                _close(session)
            except BaseException:
                _close(upstream)
                _close(session)
                lease.release("tune_failed")
                raise
        lease.release(outcome)
        if account_failure:
            pool.fail_account(lease.account.id)
    return _failure("Xtream upstream tune failed; check pool diagnostics", last_status)
