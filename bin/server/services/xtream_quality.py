"""Measure a small sample of Xtream media without exposing authenticated URLs."""
from __future__ import annotations

import json
import fcntl
import math
import os
import subprocess
import signal
import sys
import time
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path

from db.connection import resolve_db_path
from xtream_ingest import XtreamError, build_stream_url
from xtream_activity import normal_activity_remaining, quality_probe_context
from xtream_hosts import host_configs
from xtream_pool import PoolUnavailable, XtreamPool
from xtream_quality_sample import MAX_SAMPLE_BYTES, MAX_SAMPLE_SECONDS


PROBE_INTERVAL_SECONDS = 30
FAILED_PROBE_PAUSE_SECONDS = 120


class QualityProbeDeferred(XtreamError):
    def __init__(self, retry_after, *, busy=False, message=None):
        self.retry_after = max(1, math.ceil(retry_after))
        message = message or ("Another resolution check is running." if busy else
                              "Resolution checks are paused to limit provider requests.")
        super().__init__(f"{message} Try again in {self.retry_after} seconds.")


def _eligible_probe_configs(pool, status=None):
    """Use cached health only: optional diagnostics must not perform recovery."""
    status = status if status is not None else pool.status()
    healthy = [row for row in status["accounts"]
               if row["enabled"] and row["health"] == "healthy"]
    if not healthy:
        raise QualityProbeDeferred(
            FAILED_PROBE_PAUSE_SECONDS,
            message="Resolution checks are paused because no enabled account is verified healthy.",
        )
    available = {row["id"] for row in healthy if row["available"] > 0}
    if not available:
        raise PoolUnavailable("No verified healthy Xtream playback slots are available")
    return tuple(account.config for account in pool.accounts if account.id in available)


def _require_quiet(db_path):
    remaining = normal_activity_remaining(db_path)
    if remaining > 0:
        raise QualityProbeDeferred(
            remaining,
            message="Resolution checks are paused for 10 minutes after channel or account activity.",
        )


@contextmanager
def quality_probe_guard(db_path, *, pool=None):
    """Serialize optional probes across workers; preserve pacing across restarts.

    This is separate from playback leases and does not change account health.
    The route holds the guard during catalog validation too, before any HTTP.
    """
    path = Path(db_path)
    directory = path.parent / (path.name + ".xtream-locks")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory / "quality-probe-gate", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise QualityProbeDeferred(5, busy=True) from None
        # A curl/FFmpeg child can retain its media lease after its Python
        # worker exits. Reap only unlocked leases before allowing a new probe.
        pool = pool or XtreamPool(path)
        status = pool.status()
        if any(lease["source"] == "quality_probe" for lease in status["leases"]):
            raise QualityProbeDeferred(5, busy=True)
        _require_quiet(path)
        if status["active"] or any(row["busy"] for row in status["accounts"]):
            raise QualityProbeDeferred(5, busy=True,
                message="Resolution checks are paused while channel or account activity is running.")
        try:
            next_allowed = float(os.read(fd, 128) or b"0")
        except ValueError:
            next_allowed = 0
        remaining = next_allowed - time.time()
        if math.isfinite(remaining) and remaining > 0:
            raise QualityProbeDeferred(remaining)
        configs = _eligible_probe_configs(pool, status)
        succeeded = False
        try:
            with quality_probe_context(path):
                yield configs
            succeeded = True
        finally:
            delay = PROBE_INTERVAL_SECONDS if succeeded else FAILED_PROBE_PAUSE_SECONDS
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, str(time.time() + delay).encode("ascii"))
            os.fsync(fd)
    finally:
        os.close(fd)


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
    except (ValueError, TypeError, KeyError, StopIteration, ZeroDivisionError, OverflowError, UnicodeError):
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


def _sample_media(lease, stream_id, extension, *, require_quiet=True, sample_seconds=None):
    """Kill all sample transports at a wall deadline, even on worker shutdown."""
    deadline = time.monotonic() + MAX_SAMPLE_SECONDS
    hosts = host_configs(lease.account.config, lease.gate_fd)
    arguments = {
        "ts_url": build_stream_url(hosts[0][1], stream_id, "ts"),
        "hls_url": build_stream_url(hosts[0][1], stream_id, "m3u8"),
        "primary_host_index": hosts[0][0],
        "extension": extension,
    }
    if sample_seconds is not None:
        arguments["sample_seconds"] = sample_seconds
    if len(hosts) > 1:
        arguments.update(alternate_ts_url=build_stream_url(hosts[1][1], stream_id, "ts"),
                         alternate_hls_url=build_stream_url(hosts[1][1], stream_id, "m3u8"),
                         alternate_host_index=hosts[1][0])
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve().parents[2] / "xtream_quality_sample.py"),
         str(deadline), str(lease.fd), str(lease.gate_fd)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        pass_fds=(lease.fd, lease.gate_fd), start_new_session=True,
    )
    try:
        payload = json.dumps(arguments).encode()
        while True:
            if require_quiet:
                _require_quiet(lease.pool.db_path)
            try:
                sample, _ = process.communicate(
                    input=payload,
                    timeout=min(0.25, max(0.001, deadline - time.monotonic())),
                )
                break
            except subprocess.TimeoutExpired:
                payload = None
                if time.monotonic() >= deadline:
                    raise TimeoutError("Resolution sample deadline reached") from None
        if require_quiet:
            _require_quiet(lease.pool.db_path)
        if process.returncode in {3, -signal.SIGKILL}:
            raise TimeoutError("Resolution sample deadline reached")
        if process.returncode or not sample or sample[0] != 0x47:
            raise OSError("Provider returned no usable resolution sample")
        if len(sample) > MAX_SAMPLE_BYTES:
            raise OSError("Resolution sample exceeded its byte limit")
        return sample
    finally:
        # Kill the private group before making its lease reusable. This also
        # catches a curl/FFmpeg child left behind after the sampler has exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
        for pipe in (process.stdin, process.stdout):
            if pipe and not pipe.closed:
                pipe.close()


def measure_stream_quality(stream_id, extension="ts", *, pool=None,
                           runner=subprocess.run, guarded=True) -> dict:
    """Sample in a bounded worker; close media and release its slot before analysis."""
    pool = pool or XtreamPool(resolve_db_path())
    if guarded:
        with quality_probe_guard(pool.db_path, pool=pool):
            return measure_stream_quality(stream_id, extension, pool=pool,
                                          runner=runner, guarded=False)
    _eligible_probe_configs(pool)
    _require_quiet(pool.db_path)
    try:
        lease = pool.acquire(stream_id, "quality_probe")
    except PoolUnavailable:
        raise PoolUnavailable("All Xtream playback slots are occupied or unavailable") from None
    outcome = "upstream_error"
    try:
        _require_quiet(pool.db_path)
        sample = _sample_media(lease, stream_id, extension)
        outcome = "client_closed"
    except QualityProbeDeferred:
        outcome = "client_closed"
        raise
    except TimeoutError:
        outcome = "upstream_timeout"
        raise XtreamError(
            "Resolution sampling timed out; playback accounts were not changed. "
            "Resolution checks are paused for two minutes before another attempt."
        ) from None
    except Exception:
        raise XtreamError(
            "Video resolution could not be measured on this attempt; playback accounts were not changed. "
            "Resolution checks are paused for two minutes before another attempt."
        ) from None
    finally:
        lease.release(outcome)
    # ffprobe only sees local bytes; provider media is already closed.
    return _probe_bytes(sample, runner)
