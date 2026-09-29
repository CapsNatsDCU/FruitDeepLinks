"""Redact known Xtream secrets even if HTTP-library debug logging is enabled."""
import logging
import threading
from urllib.parse import quote, quote_plus

_lock = threading.Lock()
_secrets = set()


class _TransportRedactor(logging.Filter):
    def filter(self, record):
        with _lock:
            secrets = sorted(_secrets, key=len, reverse=True)
        message = record.getMessage()
        for secret in secrets:
            message = message.replace(secret, "[REDACTED]")
        record.msg, record.args = message, ()
        # HTTP transport exceptions may carry request URLs separately.
        if record.exc_info:
            record.exc_info, record.exc_text = None, None
        return True


_filter = _TransportRedactor()


def protect_http_logs(config):
    with _lock:
        for secret in (config.username, config.password):
            if secret:
                _secrets.update((secret, quote(secret, safe=""), quote_plus(secret)))
    for name in ("urllib3.connectionpool", "urllib3.util.retry", "requests.packages.urllib3.connectionpool"):
        logger = logging.getLogger(name)
        if _filter not in logger.filters:
            logger.addFilter(_filter)
