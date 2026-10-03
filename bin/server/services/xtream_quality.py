"""Measure a small sample of Xtream media without exposing authenticated URLs."""
from __future__ import annotations

import json
import subprocess
import time
from fractions import Fraction

import requests

from db.connection import resolve_db_path
from server.services.xtream_proxy import _close
from xtream_hls import HLSStream
from xtream_ingest import XtreamError, build_stream_url
from xtream_pool import PoolUnavailable, XtreamPool


MAX_SAMPLE_BYTES = 4 * 1024 * 1024
MAX_SAMPLE_SECONDS = 15


def _parse_probe_output(output: bytes) -> dict:
    try:
        streams = json.loads(output).get("streams", [])
        video = next(row for row in streams if row.get("codec_type") == "video")
        width, height = int(video["width"]), int(video["height"])
        if not (1 <= width <= 16384 and 1 <= height <= 16384):
            raise ValueError()
        rate = video.get("avg_frame_rate") or video.get("r_frame_rate")
        fps = float(Fraction(str(rate))) if rate and rate != "0/0" else None
        if fps is not None and not (0 < fps <= 240):
            fps = None
        codec = str(video.get("codec_name") or "").lower()
        if not codec.isalnum() or len(codec) > 30:
            codec = None
        return {"width": width, "height": height,
                "fps": round(fps, 2) if fps is not None else None, "codec": codec}
    except (ValueError, TypeError, KeyError, StopIteration, ZeroDivisionError, OverflowError):
        raise XtreamError("Video resolution could not be measured from this stream") from None


def _probe_bytes(sample: bytes, runner=subprocess.run) -> dict:
    try:
        result = runner(
            ["ffprobe", "-v", "error", "-f", "mpegts", "-analyzeduration", "5000000",
             "-probesize", "5000000", "-show_entries",
             "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate",
             "-of", "json", "pipe:0"],
            input=sample, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=8, check=False,
        )
        if result.returncode:
            raise XtreamError("Video resolution could not be measured from this stream")
        return _parse_probe_output(result.stdout)
    except (OSError, subprocess.TimeoutExpired):
        raise XtreamError("Video resolution could not be measured from this stream") from None


def measure_stream_quality(stream_id, extension="ts", *, pool=None,
                           session_factory=requests.Session, runner=subprocess.run) -> dict:
    """Reserve one playback slot, sample media, then release it before returning."""
    pool = pool or XtreamPool(resolve_db_path())
    pool.check_accounts(due_only=True)
    excluded = set()
    while len(excluded) < len(pool.accounts):
        try:
            lease = pool.acquire(stream_id, "quality_probe", excluded=excluded)
        except PoolUnavailable:
            raise XtreamError("All Xtream playback slots are occupied or unavailable") from None
        excluded.add(lease.account.id)
        session = upstream = None
        outcome = "tune_failed"
        authentication = False
        account_failure = False
        try:
            from xtream_logging import protect_http_logs
            protect_http_logs(lease.account.config)
            session = session_factory()
            upstream = session.get(
                build_stream_url(lease.account.config, stream_id, "ts"),
                stream=True, timeout=(5, 5), headers={"Accept-Encoding": "identity"},
                allow_redirects=True,
            )
            authentication = upstream.status_code in {401, 403}
            account_failure = authentication or upstream.status_code in {429, 500, 502, 503, 504}
            if authentication:
                outcome = "authentication_failed"
                raise OSError("Provider authentication rejected")
            use_hls = upstream.status_code in {404, 415} and str(extension).lower() == "m3u8"
            if not use_hls:
                upstream.raise_for_status()
                chunks = iter(upstream.iter_content(chunk_size=64 * 1024))
                first = next(chunks, b"")
                use_hls = first.lstrip().startswith(b"#EXTM3U")
            if use_hls:
                _close(upstream)
                upstream = HLSStream(build_stream_url(lease.account.config, stream_id, "m3u8"), 5, lease.fd)
                chunks = iter(upstream.chunks())
                first = next(chunks, b"")
            if not first or first[0] != 0x47:
                outcome = "unsupported_transport"
                raise OSError("Provider returned no transport stream")
            sample = bytearray(first[:MAX_SAMPLE_BYTES])
            deadline = time.monotonic() + MAX_SAMPLE_SECONDS
            for chunk in chunks:
                if time.monotonic() >= deadline or len(sample) >= MAX_SAMPLE_BYTES:
                    break
                sample.extend(chunk[:MAX_SAMPLE_BYTES - len(sample)])
            measured = _probe_bytes(bytes(sample), runner)
            outcome = "client_closed"
            return measured
        except Exception as error:
            account_failure = account_failure or isinstance(error, (requests.ConnectionError, requests.Timeout, TimeoutError))
            if outcome == "tune_failed" and account_failure:
                outcome = "upstream_timeout" if isinstance(error, (requests.Timeout, TimeoutError)) else "upstream_error"
        finally:
            _close(upstream)
            _close(session)
            lease.release(outcome)
        if account_failure:
            pool.fail_account(lease.account.id, authentication=authentication)
    raise XtreamError("Video resolution could not be measured; check the stream and account pool")
