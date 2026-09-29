"""Fake provider media shared by tune-route regression tests; no network."""
from contextlib import contextmanager
from unittest.mock import Mock, patch


class HealthyAccountClient:
    def __init__(self, config):
        self.config = config
        self.session = Mock()
        self.last_account_check = {"health": "healthy", "error": None}

    def get_account_max_connections(self):
        return 1


class FakeMedia:
    status_code = 200
    headers = {"Content-Type": "video/mp2t"}

    def __init__(self, chunks=None, status=200):
        self.parts = chunks if chunks is not None else [b"\x47" * 188, b"\x47" * 188]
        self.status_code = status
        self.closed = False
        self.reads = 0

    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError("http://provider.example/live/demo%20user/secret%2Fpass/500.ts")

    def iter_content(self, chunk_size):
        for part in self.parts:
            self.reads += 1
            if isinstance(part, Exception):
                raise part
            yield part

    def close(self):
        self.closed = True


@contextmanager
def mocked_provider(media=None):
    session = Mock()
    session.get.side_effect = (lambda *a, **k: media if media is not None else FakeMedia())
    with patch("xtream_pool.XtreamClient", HealthyAccountClient), patch("server.services.xtream_proxy.requests.Session", return_value=session):
        yield session
