"""Bounded resolution sample worker. Authenticated URLs arrive only on stdin.

The worker and its curl/FFmpeg children share a private process group. Its own
watchdog survives a web worker crash and closes that whole group at the deadline.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time

MAX_SAMPLE_BYTES = 4 * 1024 * 1024
MAX_SAMPLE_SECONDS = 8


def _capture_one(ts_url, hls_url, extension, lease_fd, *, deadline, gate_fd,
                 session_factory):
    import requests
    from xtream_curl import CurlStream
    from xtream_hls import HLSStream
    from xtream_transport import configure_session, media_chunks

    session = upstream = None
    try:
        session = configure_session((session_factory or requests.Session)())
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Resolution sample deadline reached")
        upstream = session.get(ts_url, stream=True, timeout=(min(5, remaining), min(3, remaining)),
                               headers={"Accept-Encoding": "identity"}, allow_redirects=True)
        authentication = upstream.status_code in {401, 403}
        if authentication:
            upstream.close()
            upstream = CurlStream(ts_url, 3, lease_fd, gate_fd)
            chunks = iter(upstream.chunks())
            first = next(chunks, b"")
            use_hls = first.lstrip().startswith(b"#EXTM3U")
        else:
            use_hls = upstream.status_code in {404, 415} and str(extension).lower() == "m3u8"
        if not authentication and not use_hls:
            upstream.raise_for_status()
            chunks = iter(media_chunks(upstream))
            first = next(chunks, b"")
            use_hls = first.lstrip().startswith(b"#EXTM3U")
        if use_hls:
            upstream.close()
            upstream = HLSStream(hls_url, 3, lease_fd, gate_fd)
            chunks = iter(upstream.chunks())
            first = next(chunks, b"")
        if not first or first[0] != 0x47:
            raise OSError("Provider returned no transport stream")
        sample = bytearray(first[:MAX_SAMPLE_BYTES])
        # Check before requesting the next chunk, including after the byte cap.
        while len(sample) < MAX_SAMPLE_BYTES and time.monotonic() < deadline:
            chunk = next(chunks, b"")
            if not chunk:
                break
            sample.extend(chunk[:MAX_SAMPLE_BYTES - len(sample)])
        return bytes(sample)
    except requests.Timeout:
        raise TimeoutError("Resolution sample read timed out") from None
    finally:
        for resource in (upstream, session):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass


def capture_sample(ts_url, hls_url, extension, lease_fd, *, deadline,
                   gate_fd=None, session_factory=None,
                   primary_host_index=0, alternate_ts_url=None,
                   alternate_hls_url=None, alternate_host_index=1):
    from xtream_hosts import record_host_success

    hosts = [(primary_host_index, ts_url, hls_url)]
    if alternate_ts_url and alternate_hls_url:
        hosts.append((alternate_host_index, alternate_ts_url, alternate_hls_url))
    for position, (index, candidate_ts, candidate_hls) in enumerate(hosts):
        try:
            sample = _capture_one(candidate_ts, candidate_hls, extension, lease_fd,
                                  deadline=deadline, gate_fd=gate_fd,
                                  session_factory=session_factory)
            record_host_success(gate_fd, index)
            return sample
        except Exception:
            if position == len(hosts) - 1 or time.monotonic() >= deadline:
                raise


def main():
    if os.getpgrp() != os.getpid():
        # Never allow accidental direct invocation to kill a caller's group.
        return 2
    deadline, lease_fd, gate_fd = float(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    # Default SIGALRM is not reliable inside blocking C calls. A separate thread
    # kills the process group, including any media child holding the lease fd.
    def expire():
        os.killpg(os.getpgrp(), signal.SIGKILL)

    watchdog = threading.Timer(max(0, deadline - time.monotonic()), expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        arguments = json.load(sys.stdin)
        sample = capture_sample(**arguments, lease_fd=lease_fd, gate_fd=gate_fd, deadline=deadline)
        sys.stdout.buffer.write(sample)
        sys.stdout.buffer.flush()
        return 0
    except (TimeoutError,):
        return 3
    except Exception:
        # No exception text: provider clients may include authenticated URLs.
        return 2


if __name__ == "__main__":
    sys.exit(main())
