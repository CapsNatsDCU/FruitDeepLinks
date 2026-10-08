"""Optional HLS-to-TS remux, owned by the same downstream stream lease.

FFmpeg copies compressed media (no transcoding). A private loopback bootstrap
playlist keeps the provider URL out of process arguments/logs while letting
FFmpeg inherit HTTP options for nested playlists and segments.
"""
from __future__ import annotations

import os
import select
import subprocess
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from xtream_process import close_media_process
from xtream_transport import proxy_url, user_agent


class HLSStream:
    def __init__(self, url: str, idle_timeout: float, lease_fd: int, gate_fd: int | None = None):
        self.idle_timeout = idle_timeout
        agent = user_agent()
        proxy = proxy_url()
        child_env = {**os.environ}
        if proxy:
            # FFmpeg's HLS demuxer opens nested playlist/segment URLs itself.
            # Its HTTP protocol reads http_proxy from the child environment;
            # an -http_proxy input option does not reach those nested requests.
            child_env = {**os.environ, "http_proxy": proxy, "https_proxy": proxy,
                         "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy,
                         "no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}
        else:
            bypass = child_env.get("no_proxy", child_env.get("NO_PROXY", ""))
            child_env.update({"no_proxy": bypass + ",127.0.0.1",
                              "NO_PROXY": bypass + ",127.0.0.1"})
        seed_path = "/" + secrets.token_hex(24) + ".m3u8"
        playlist = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=10000000\n" + url + "\n").encode()

        class Bootstrap(BaseHTTPRequestHandler):
            timeout = 2

            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path != seed_path:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(playlist)))
                self.end_headers()
                try:
                    self.wfile.write(playlist)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.bootstrap = HTTPServer(("127.0.0.1", 0), Bootstrap)
        self.bootstrap_thread = threading.Thread(
            target=lambda: self.bootstrap.serve_forever(poll_interval=0.1), daemon=True)
        self.bootstrap_thread.start()
        seed_url = f"http://127.0.0.1:{self.bootstrap.server_port}{seed_path}"
        self.process = None
        try:
            self.process = subprocess.Popen(
                ["ffmpeg", "-nostats", "-hide_banner", "-loglevel", "quiet",
                 "-protocol_whitelist", "pipe,http,https,tcp,tls,crypto",
                 "-rw_timeout", str(int(idle_timeout * 1_000_000)),
                 "-user_agent", agent,
                 "-f", "hls", "-i", seed_url, "-map", "0:v?", "-map", "0:a?",
                 "-c", "copy", "-f", "mpegts", "pipe:1"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0,
                env=child_env,
                # Keep the reservation alive if the Python worker dies before
                # FFmpeg notices its broken stdout pipe or upstream idle timeout.
                pass_fds=(lease_fd,) if gate_fd is None else (lease_fd, gate_fd),
            )
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
        if self.process is not None:
            close_media_process(self.process)
        if self.bootstrap is not None:
            self.bootstrap.shutdown()
            self.bootstrap.server_close()
            self.bootstrap_thread.join(timeout=1)
            self.bootstrap = None
