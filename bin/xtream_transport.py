"""Opt-in HTTP proxy for Xtream provider traffic only."""
from __future__ import annotations

import os
from urllib.parse import urlsplit


def proxy_url() -> str | None:
    """Return a credential-free HTTP proxy URL or a safe configuration error."""
    value = os.getenv("XTREAM_HTTP_PROXY", "").strip()
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "http"
            and parsed.hostname is not None
            and parsed.port is not None
            and 1 <= parsed.port <= 65535
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
            and not any(char.isspace() for char in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("XTREAM_HTTP_PROXY must be a credential-free http://host:port URL")
    return value.rstrip("/")


def configure_session(session):
    """Apply the explicit Xtream proxy without changing other HTTP clients."""
    proxy = proxy_url()
    if proxy:
        session.trust_env = False
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def curl_proxy_args() -> list[str]:
    proxy = proxy_url()
    return ["--proxy", proxy, "--noproxy", ""] if proxy else []


def media_chunks(response):
    """Read enough TS to validate startup, then use larger forwarding chunks.

    urllib3's non-chunked stream reader waits to fill the requested amount.
    Waiting for 12 KiB before serving a tune adds avoidable startup buffering.
    Both iterators consume the same response; the second resumes after the
    prefix rather than replaying it. A short HLS body can still reach EOF.
    """
    prefix = next(response.iter_content(chunk_size=188 * 2), b"")
    if prefix:
        yield prefix
        yield from response.iter_content(chunk_size=188 * 64)
