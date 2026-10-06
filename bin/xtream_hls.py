"""Optional HLS-to-TS remux, owned by the same downstream stream lease.

FFmpeg copies compressed media (no transcoding). A tiny master playlist passes
the authenticated URL over stdin so it is absent from process arguments/logs.
"""
from __future__ import annotations

import os
import select
import subprocess

from xtream_process import close_media_process
from xtream_transport import proxy_url


class HLSStream:
    def __init__(self, url: str, idle_timeout: float, lease_fd: int):
        self.idle_timeout = idle_timeout
        proxy = proxy_url()
        child_env = None
        if proxy:
            # FFmpeg's HLS demuxer opens nested playlist/segment URLs itself.
            # Its HTTP protocol reads http_proxy from the child environment;
            # an -http_proxy input option does not reach those nested requests.
            child_env = {**os.environ, "http_proxy": proxy, "https_proxy": proxy,
                         "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy,
                         "no_proxy": "", "NO_PROXY": ""}
        self.process = subprocess.Popen(
            ["ffmpeg", "-nostats", "-hide_banner", "-loglevel", "quiet",
             "-protocol_whitelist", "pipe,http,https,tcp,tls,crypto",
             "-rw_timeout", str(int(idle_timeout * 1_000_000)),
             "-f", "hls", "-i", "pipe:0", "-map", "0:v?", "-map", "0:a?",
             "-c", "copy", "-f", "mpegts", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0,
            **({"env": child_env} if child_env else {}),
            # Keep the reservation alive if the Python worker dies before
            # FFmpeg notices its broken stdout pipe or upstream idle timeout.
            pass_fds=(lease_fd,),
        )
        try:
            self.process.stdin.write(("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=10000000\n" + url + "\n").encode())
            self.process.stdin.close()
        except BaseException:
            self.close()
            raise

    def chunks(self):
        while True:
            ready, _, _ = select.select([self.process.stdout], [], [], self.idle_timeout)
            if not ready:
                raise TimeoutError("HLS media idle timeout")
            chunk = self.process.stdout.read(64 * 1024)
            if not chunk:
                if self.process.wait(timeout=5) != 0:
                    raise OSError("HLS remux failed")
                return
            yield chunk

    def close(self):
        close_media_process(self.process)
