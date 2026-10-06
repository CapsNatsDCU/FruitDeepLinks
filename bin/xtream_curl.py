"""Curl media reader for providers that reject Python's HTTP client.

Authenticated URLs are written to curl's stdin configuration, never its
process arguments, response headers, database, or application logs.
"""
from __future__ import annotations

import select
import subprocess

from xtream_process import close_media_process
from xtream_transport import curl_proxy_args


def _config_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


class CurlStream:
    def __init__(self, url: str, idle_timeout: float, lease_fd: int):
        self.idle_timeout = idle_timeout
        self.process = subprocess.Popen(
            ["curl", "--silent", "--location", "--fail", "--no-buffer",
             "--connect-timeout", "10", "--proto", "=http,https",
             "--proto-redir", "=http,https", *curl_proxy_args(), "--config", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0, pass_fds=(lease_fd,),
        )
        try:
            self.process.stdin.write(f'url = "{_config_quote(url)}"\n'.encode())
            self.process.stdin.close()
        except BaseException:
            self.close()
            raise

    def chunks(self):
        while True:
            ready, _, _ = select.select([self.process.stdout], [], [], self.idle_timeout)
            if not ready:
                raise TimeoutError("Curl media idle timeout")
            chunk = self.process.stdout.read(64 * 1024)
            if not chunk:
                if self.process.wait(timeout=5) != 0:
                    raise OSError("Curl media transport failed")
                return
            yield chunk

    def close(self):
        close_media_process(self.process)
