"""Ask Channels DVR to reload its configured Fruit playlist and guide."""

from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from time import sleep


def refresh_source(host: str, source_name: str, *, timeout: int = 10) -> dict[str, str | None]:
    """Return an error for each failed request; never mistake HTTP 4xx for success."""
    base = f"http://{host}:8089"
    name = quote(source_name, safe="")
    requests = (
        ("playlist", "POST", f"{base}/providers/m3u/sources/{name}/refresh"),
        ("guide", "PUT", f"{base}/dvr/lineups/XMLTV-{name}"),
    )
    result: dict[str, str | None] = {}
    for label, method, url in requests:
        try:
            with urlopen(Request(url, method=method), timeout=timeout) as response:
                # urlopen raises HTTPError for 4xx/5xx. Keep this explicit for
                # test doubles and unusual handlers that return another status.
                status = response.status
                result[label] = None if 200 <= status < 300 else f"HTTP {status}"
        except HTTPError as exc:
            result[label] = f"HTTP {exc.code}"
            exc.close()
        except (URLError, TimeoutError, OSError) as exc:
            result[label] = type(exc).__name__
        if label == "playlist":
            # Give Channels time to replace its lineup before reloading XMLTV.
            sleep(2)
    return result
