"""Curl media reader for providers that reject Python's HTTP client.

Authenticated URLs are written to curl's stdin configuration, never its
process arguments, response headers, database, or application logs.
"""
from __future__ import annotations

import select
import subprocess


def _config_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


class CurlStream:
    def __init__(self, url: str, idle_timeout: float, lease_fd: int):
        self.idle_timeout = idle_timeout
        self.process = subprocess.Popen(
            ["curl", "-4", "--silent", "--location", "--fail", "--no-buffer",
             "--connect-timeout", "10", "--proto", "=http,https",
             "--proto-redir", "=http,https", "--config", "-"],
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
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe and not pipe.closed:
                pipe.close()
