"""Configuration loading. See config.example.toml for the shape."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from urllib.parse import urlsplit

# The one place in the code that holds a default. `parse_config` fills every
# field of `Config` from the file or from here, so the dataclasses carry none.
# config.example.toml shows the same values, and a test holds it to them.
DEFAULTS = {
    "service": {
        "readonly": True,
        "state_path": "data/state.json",
        "temp_dir": "/tmp/immich-family-library",
        "settle_seconds": 60,
        "interval_seconds": 600,
    },
    "webhook": {"listen": "0.0.0.0:8080"},
    "family": {"dropbox_album": "Family Dropbox"},
    "limits": {"albums_per_pass": 5, "assets_per_pass": 20},
    "tags": {"moved": "family", "from_prefix": "from"},
    "warnings": {"report_at": "22:00"},
}


# Every section and key the file may hold. Anything else is an error, so a
# typo or an old name fails at startup instead of silently taking the default.
SCHEMA = {
    "immich": {"url", "admin_api_key"},
    "service": {"readonly", "state_path", "temp_dir", "settle_seconds", "interval_seconds"},
    "webhook": {"url", "token", "listen"},
    "family": {"email", "api_key", "dropbox_album"},
    "members": {"email", "api_key"},  # per [members.<name>] table
    "limits": {"albums_per_pass", "assets_per_pass"},
    "tags": {"moved", "from_prefix"},
    "warnings": {"report_at"},
}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class AccountConfig:
    name: str
    api_key: str
    email: str | None = None  # checked against the key's owner at startup when given


@dataclass(frozen=True)
class WebhookConfig:
    url: str  # what Immich calls, e.g. http://ifl:8080/webhook/asset-created
    token: str  # sent by Immich in the X-IFL-Token header; the receiver checks it
    listen: str

    @property
    def path(self) -> str:
        """What the receiver serves: the path Immich is told to call, so the
        two cannot disagree."""
        return urlsplit(self.url).path or "/"

    @property
    def bind(self) -> tuple[str, int]:
        """`listen` as the host and port to bind. An empty host means every
        address. Raises ValueError when `listen` is not `host:port`."""
        host, sep, port = self.listen.rpartition(":")
        bracketed = host.startswith("[") and host.endswith("]")
        half_bracketed = not bracketed and (host.startswith("[") or host.endswith("]"))
        if (
            not sep
            or not port.isascii()
            or not port.isdigit()
            or half_bracketed
            or (":" in host and not bracketed)
        ):
            raise ValueError(f"not host:port: {self.listen!r}")
        return (host[1:-1] if bracketed else host) or "0.0.0.0", int(port)


@dataclass(frozen=True)
class Config:
    immich_url: str
    family: AccountConfig
    dropbox_album: str
    members: dict[str, AccountConfig]
    state_path: Path
    temp_dir: Path  # holds one original at a time, between its download and its upload
    readonly: bool
    settle_seconds: int  # leave an asset alone this long after its last change
    interval_seconds: int  # the daemon's pause between passes
    webhook: WebhookConfig | None  # the daemon's receiver; None means no webhook
    albums_per_pass: int  # 0 means no limit
    assets_per_pass: int  # 0 means no limit
    moved_tag: str  # goes on a member's original when it's moved
    from_prefix: str  # the family copy is tagged "<from_prefix>/<member>"
    admin_api_key: str | None  # any admin user's key; only for the queue check
    report_at: time  # local time of day for the daily re-upload report
    config_dir: Path = field(compare=False)


KINDS = {str: "a string", bool: "true or false", int: "a whole number"}


def _typed(section: str, raw: dict, key: str, kind: type):
    """The setting, or its default when the file leaves it out. A value of the
    wrong type is an error: `readonly = "false"` would otherwise read as true."""
    if key not in raw:
        return DEFAULTS.get(section, {}).get(key)
    value = raw[key]
    # A bool is an int in Python, and `assets_per_pass = true` must not read as 1.
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ConfigError(f"[{section}] {key} must be {KINDS[kind]}")
    return value


def _account(section: str, raw: dict, name: str) -> AccountConfig:
    for key in ("email", "api_key"):
        if not _typed(section, raw, key, str):
            raise ConfigError(f"[{section}] is missing '{key}'")
    return AccountConfig(name=name, email=raw["email"], api_key=raw["api_key"])


def load_config(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    return parse_config(raw, config_dir=path.parent)


def _check_keys(raw: dict) -> None:
    for section, body in raw.items():
        if section not in SCHEMA:
            raise ConfigError(f"unknown section [{section}]")
        if not isinstance(body, dict):
            raise ConfigError(f"[{section}] must be a table")
        if section == "members":
            for name, member in body.items():
                if not isinstance(member, dict):
                    raise ConfigError(f"[members.{name}] must be a table")
                for key in member:
                    if key not in SCHEMA["members"]:
                        raise ConfigError(f"[members.{name}] has an unknown key '{key}'")
            continue
        for key in body:
            if key not in SCHEMA[section]:
                raise ConfigError(f"[{section}] has an unknown key '{key}'")


def parse_config(raw: dict, config_dir: Path = Path(".")) -> Config:
    _check_keys(raw)
    immich = raw.get("immich") or {}
    if not _typed("immich", immich, "url", str):
        raise ConfigError("[immich] url is required")

    family_raw = raw.get("family") or {}
    family = _account("family", family_raw, "family")
    dropbox_album = _typed("family", family_raw, "dropbox_album", str)
    if not dropbox_album.strip():
        raise ConfigError("[family] dropbox_album must be a non-empty album name")

    members = {}
    for name, m in (raw.get("members") or {}).items():
        members[name] = _account(f"members.{name}", m, name)
    if not members:
        raise ConfigError("at least one [members.<name>] section is required")

    seen_emails = {family.email}
    for m in members.values():
        if m.email in seen_emails:
            raise ConfigError(f"email {m.email} is used by more than one account")
        seen_emails.add(m.email)

    service = raw.get("service") or {}
    readonly = _typed("service", service, "readonly", bool)
    state_path_raw = _typed("service", service, "state_path", str)
    if not state_path_raw:
        raise ConfigError("[service] state_path must name a file")
    state_path = Path(state_path_raw)
    if not state_path.is_absolute():
        state_path = config_dir / state_path
    temp_dir_raw = _typed("service", service, "temp_dir", str)
    if not temp_dir_raw:
        raise ConfigError("[service] temp_dir must name a directory")
    temp_dir = Path(temp_dir_raw)
    if not temp_dir.is_absolute():
        temp_dir = config_dir / temp_dir

    limits = raw.get("limits") or {}
    albums_per_pass = _typed("limits", limits, "albums_per_pass", int)
    if albums_per_pass < 0:
        raise ConfigError("[limits] albums_per_pass must be a whole number (0 for no limit)")
    assets_per_pass = _typed("limits", limits, "assets_per_pass", int)
    if assets_per_pass < 0:
        raise ConfigError("[limits] assets_per_pass must be a whole number (0 for no limit)")

    settle = _typed("service", service, "settle_seconds", int)
    if settle < 0:
        raise ConfigError("[service] settle_seconds must be a whole number of seconds")
    interval = _typed("service", service, "interval_seconds", int)
    if interval < 1:
        raise ConfigError(
            "[service] interval_seconds must be a whole number of seconds, at least 1"
        )

    webhook = None
    webhook_raw = raw.get("webhook")
    if webhook_raw:
        for key in ("url", "token"):
            if not _typed("webhook", webhook_raw, key, str):
                raise ConfigError(f"[webhook] is missing '{key}'")
        webhook = WebhookConfig(
            url=webhook_raw["url"],
            token=webhook_raw["token"],
            listen=_typed("webhook", webhook_raw, "listen", str),
        )
        _check_webhook(webhook)

    tags = raw.get("tags") or {}
    moved_tag = _typed("tags", tags, "moved", str)
    from_prefix = _typed("tags", tags, "from_prefix", str)
    for key, value in (("moved", moved_tag), ("from_prefix", from_prefix)):
        if not value.strip() or value.strip("/") != value:
            raise ConfigError(
                f"[tags] {key} must be a tag name without leading or trailing slashes"
            )

    warnings = raw.get("warnings") or {}
    report_at = _time_of_day(warnings.get("report_at", DEFAULTS["warnings"]["report_at"]))

    admin_api_key = _typed("immich", immich, "admin_api_key", str) or None

    return Config(
        immich_url=immich["url"].rstrip("/"),
        family=family,
        dropbox_album=dropbox_album,
        members=members,
        state_path=state_path,
        temp_dir=temp_dir,
        readonly=readonly,
        settle_seconds=settle,
        interval_seconds=interval,
        webhook=webhook,
        albums_per_pass=albums_per_pass,
        assets_per_pass=assets_per_pass,
        moved_tag=moved_tag,
        from_prefix=from_prefix,
        admin_api_key=admin_api_key,
        report_at=report_at,
        config_dir=config_dir,
    )


def _check_webhook(webhook: WebhookConfig) -> None:
    try:
        url = urlsplit(webhook.url)
        ok = url.scheme in ("http", "https") and bool(url.hostname)
    except ValueError:
        ok = False
    if not ok:
        raise ConfigError("[webhook] url must be an http or https URL")
    try:
        _, port = webhook.bind
        ok = 1 <= port <= 65535
    except ValueError:
        ok = False
    if not ok:
        raise ConfigError(
            '[webhook] listen must be "host:port" with a port from 1 to 65535, '
            'for example "0.0.0.0:8080", ":8080" or "[::]:8080"'
        )


def _time_of_day(value: object) -> time:
    if isinstance(value, str) and re.fullmatch(r"\d\d:\d\d", value):
        try:
            return time.fromisoformat(value)
        except ValueError:
            pass
    raise ConfigError('[warnings] report_at must be a time of day as "HH:MM"')


def redact(secret: str) -> str:
    """Stars for all but the last four characters, enough to tell keys apart."""
    return "*" * (len(secret) - 4) + secret[-4:] if len(secret) > 4 else "*" * len(secret)


def to_raw(cfg: Config, *, minimal: bool = False) -> dict:
    """The effective config in the file's shape, secrets redacted.

    Paths come out resolved, as the service uses them. With `minimal`, settings
    that have a default are left out when they match it; everything required is
    always included.
    """

    def account(a: AccountConfig) -> dict:
        out = {"email": a.email} if a.email else {}
        return {**out, "api_key": redact(a.api_key)}

    out: dict = {
        "immich": {"url": cfg.immich_url},
        "service": {
            "readonly": cfg.readonly,
            "state_path": str(cfg.state_path),
            "temp_dir": str(cfg.temp_dir),
            "settle_seconds": cfg.settle_seconds,
            "interval_seconds": cfg.interval_seconds,
        },
        "limits": {"albums_per_pass": cfg.albums_per_pass, "assets_per_pass": cfg.assets_per_pass},
        "tags": {"moved": cfg.moved_tag, "from_prefix": cfg.from_prefix},
        "warnings": {"report_at": cfg.report_at.strftime("%H:%M")},
        "family": {**account(cfg.family), "dropbox_album": cfg.dropbox_album},
        "members": {name: account(m) for name, m in cfg.members.items()},
    }
    if cfg.admin_api_key:
        out["immich"]["admin_api_key"] = redact(cfg.admin_api_key)
    if cfg.webhook:
        out["webhook"] = {
            "url": cfg.webhook.url,
            "token": redact(cfg.webhook.token),
            "listen": cfg.webhook.listen,
        }
    if minimal:
        # The one default that is not a constant: it is relative to the config file.
        default_state = str(cfg.config_dir / DEFAULTS["service"]["state_path"])
        defaults = {**DEFAULTS, "service": {**DEFAULTS["service"], "state_path": default_state}}
        for section, settings in defaults.items():
            for key, default in settings.items():
                if section in out and key in out[section] and out[section][key] == default:
                    del out[section][key]
        out = {section: settings for section, settings in out.items() if settings}
    return out
