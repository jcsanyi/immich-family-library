"""Configuration loading. See config.example.toml for the shape."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from urllib.parse import urlsplit

DEFAULTS = {
    "service": {
        "readonly": True,
        "state_path": "data/state.json",
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
    "service": {"readonly", "state_path", "settle_seconds", "interval_seconds"},
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
    listen: str = "0.0.0.0:8080"

    @property
    def path(self) -> str:
        """What the receiver serves: the path Immich is told to call, so the
        two cannot disagree."""
        return urlsplit(self.url).path or "/"

    @property
    def bind(self) -> tuple[str, int]:
        """`listen` as the host and port to bind."""
        host, _, port = self.listen.rpartition(":")
        return host.strip("[]") or "0.0.0.0", int(port)


@dataclass(frozen=True)
class Config:
    immich_url: str
    family: AccountConfig
    dropbox_album: str
    members: dict[str, AccountConfig]
    state_path: Path = Path("data/state.json")
    readonly: bool = True
    settle_seconds: int = 60  # leave an asset alone this long after its last change
    interval_seconds: int = 600  # the daemon's pause between passes
    webhook: WebhookConfig | None = None  # the daemon's receiver; None means no webhook
    albums_per_pass: int = 5  # 0 means no limit
    assets_per_pass: int = 20  # 0 means no limit
    moved_tag: str = "family"  # goes on a member's original when it's moved
    from_prefix: str = "from"  # the family copy is tagged "<from_prefix>/<member>"
    admin_api_key: str | None = None  # any admin user's key; only for the queue check
    report_at: time = time(22, 0)  # local time of day for the daily re-upload report
    config_dir: Path = field(default=Path("."), compare=False)


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
    state_path = Path(_typed("service", service, "state_path", str))
    if not state_path.is_absolute():
        state_path = config_dir / state_path

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
    host, _, port = webhook.listen.rpartition(":")
    bracketed = host.startswith("[") and host.endswith("]")
    if (
        not port.isascii()
        or not port.isdigit()
        or not 1 <= int(port) <= 65535
        or (":" in host and not bracketed)
    ):
        raise ConfigError(
            '[webhook] listen must be "host:port" with a port from 1 to 65535, '
            'for example "0.0.0.0:8080" or "[::]:8080"'
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

    default_state = cfg.config_dir / DEFAULTS["service"]["state_path"]
    service = {
        "readonly": cfg.readonly,
        "state_path": str(cfg.state_path),
        "settle_seconds": cfg.settle_seconds,
        "interval_seconds": cfg.interval_seconds,
    }
    limits = {"albums_per_pass": cfg.albums_per_pass, "assets_per_pass": cfg.assets_per_pass}
    tags = {"moved": cfg.moved_tag, "from_prefix": cfg.from_prefix}
    warnings = {"report_at": cfg.report_at.strftime("%H:%M")}
    if minimal:
        defaults = {
            **{("service", k): v for k, v in DEFAULTS["service"].items()},
            ("service", "state_path"): str(default_state),
            **{("limits", k): v for k, v in DEFAULTS["limits"].items()},
            **{("tags", k): v for k, v in DEFAULTS["tags"].items()},
            **{("warnings", k): v for k, v in DEFAULTS["warnings"].items()},
        }
        sections = (
            ("service", service),
            ("limits", limits),
            ("tags", tags),
            ("warnings", warnings),
        )
        for section in sections:
            name, values = section
            for key in list(values):
                if values[key] == defaults[(name, key)]:
                    del values[key]

    out: dict = {"immich": {"url": cfg.immich_url}}
    if cfg.admin_api_key:
        out["immich"]["admin_api_key"] = redact(cfg.admin_api_key)
    for name, values in (
        ("service", service),
        ("limits", limits),
        ("tags", tags),
        ("warnings", warnings),
    ):
        if values:
            out[name] = values
    out["family"] = account(cfg.family)
    if not minimal or cfg.dropbox_album != DEFAULTS["family"]["dropbox_album"]:
        out["family"]["dropbox_album"] = cfg.dropbox_album
    out["members"] = {name: account(m) for name, m in cfg.members.items()}
    if cfg.webhook:
        wh = {
            "url": cfg.webhook.url,
            "token": redact(cfg.webhook.token),
            "listen": cfg.webhook.listen,
        }
        if minimal and wh["listen"] == DEFAULTS["webhook"]["listen"]:
            del wh["listen"]
        out["webhook"] = wh
    return out
