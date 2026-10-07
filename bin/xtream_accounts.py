"""Deployment-only Xtream secrets and safe, stable account identities.

One pool represents accounts for the same provider. Catalog sizes may differ;
the tune path validates media from the selected account before serving it.
SQLite stores only health and operator controls.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, quote_plus, urlsplit

DEFAULT_ACCOUNTS_FILE = Path("/run/secrets/xtream-accounts.json")


@dataclass(frozen=True)
class Account:
    id: str
    label: str
    enabled: bool
    config: Any = field(repr=False)
    capacity_override: int | None = None

    @property
    def fingerprint(self) -> str:
        return config_fingerprint(self.config)


def config_fingerprint(config) -> str:
    values = (config.server_url, config.username, config.password)
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def capacity(value: Any) -> int | None:
    from xtream_ingest import XtreamError
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)) or not 1 <= int(value) <= 10000:
        raise XtreamError("Account capacity must be an integer between 1 and 10000")
    return int(value)


def validate_server(url: str) -> None:
    from xtream_ingest import XtreamError
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                 and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment
                 and not any(char.isspace() for char in url))
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise XtreamError("Xtream server must be an HTTP(S) base URL without credentials or query parameters")


def load_accounts(conn=None, environ: Mapping[str, str] | None = None) -> list[Account]:
    from xtream_ingest import XtreamError, _load_legacy_config
    env = os.environ if environ is None else environ
    base = _load_legacy_config(conn, env)
    path = env.get("XTREAM_ACCOUNTS_FILE", "").strip()
    raw = env.get("XTREAM_ACCOUNTS_JSON", "").strip()
    if path and raw:
        raise XtreamError("Configure only one of XTREAM_ACCOUNTS_FILE or XTREAM_ACCOUNTS_JSON")
    if not path and not raw and DEFAULT_ACCOUNTS_FILE.is_file():
        path = str(DEFAULT_ACCOUNTS_FILE)
    if path:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise XtreamError("Xtream account secret file could not be read") from None
    if not path and not raw:
        if not (base.username or base.password or base.server_url):
            return []
        if base.server_url:
            validate_server(base.server_url)
        return [Account("legacy", "Legacy account", True, base,
                        capacity(env.get("XTREAM_CAPACITY_OVERRIDE")))]
    try:
        rows = json.loads(raw)
        if isinstance(rows, dict):
            rows = rows["accounts"]
        if not isinstance(rows, list):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise XtreamError("Xtream account configuration must contain a JSON accounts list") from None
    result = []
    seen_ids, seen_credentials = set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise XtreamError("Each Xtream account must be an object")
        account_id = row.get("id", "")
        if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account_id) or account_id in seen_ids:
            raise XtreamError("Xtream accounts require unique stable IDs (letters, digits, dash, underscore)")
        if not isinstance(row.get("enabled", True), bool):
            raise XtreamError("Account enabled must be true or false")
        if any(not isinstance(row.get(key), str) or not row[key] for key in ("server_url", "username", "password")):
            raise XtreamError("Each Xtream account requires a server URL, username and password")
        server = row["server_url"].rstrip("/")
        validate_server(server)
        config = replace(base, server_url=server, username=row["username"], password=row["password"])
        account = Account(account_id, str(row.get("label") or f"Account {len(result) + 1}")[:100],
                          row.get("enabled", True), config, capacity(row.get("capacity_override")))
        # Duplicate credentials do not create extra physical provider capacity.
        if account.fingerprint in seen_credentials:
            raise XtreamError("Duplicate Xtream credentials cannot be configured as separate accounts")
        seen_ids.add(account_id)
        seen_credentials.add(account.fingerprint)
        result.append(account)
    return result


def safe_value(value: Any, accounts: list[Account]) -> Any:
    """Redact values at metadata/export boundaries, including encoded secrets."""
    if isinstance(value, dict):
        return {key: safe_value(item, accounts) for key, item in value.items()}
    if isinstance(value, list):
        return [safe_value(item, accounts) for item in value]
    if not isinstance(value, str):
        return value
    secrets = {form for account in accounts for secret in (account.config.username, account.config.password)
               if secret for form in (secret, quote(secret, safe=""), quote_plus(secret))}
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    return value


def public_metadata(value: Any, conn=None) -> Any:
    """Use all accounts, even disabled ones, when sanitizing cached metadata."""
    return safe_value(value, load_accounts(conn))
