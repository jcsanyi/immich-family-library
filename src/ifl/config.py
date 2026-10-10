"""Configuration loading. See config.example.toml for the shape."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS = {
    "service": {
        "readonly": True,
        "ledger_path": "data/ledger.sqlite",
        "settle_seconds": 60,
        "interval_seconds": 600,
    },
    "webhook": {"listen": "0.0.0.0:8080", "path": "/webhook/asset-created"},
    "family": {"dropbox_album": "Family Dropbox"},
    "limits": {"albums_per_pass": 5, "assets_per_pass": 20},
    "tags": {"moved": "family", "from_prefix": "from"},
    "warnings": {"reuploads_per_pass": 10},
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
    path: str = "/webhook/asset-created"


@dataclass(frozen=True)
class Config:
    immich_url: str
    family: AccountConfig
    dropbox_album: str
    members: dict[str, AccountConfig]
    ledger_path: Path = Path("data/ledger.sqlite")
    readonly: bool = True
    settle_seconds: int = 60  # leave an asset alone this long after its last change
    interval_seconds: int = 600  # the daemon's pause between passes
    webhook: WebhookConfig | None = None  # the daemon's receiver; None means no webhook
    albums_per_pass: int = 5  # 0 means no limit
    assets_per_pass: int = 20  # 0 means no limit
    moved_tag: str = "family"  # goes on a member's original when it's moved
    from_prefix: str = "from"  # the family copy is tagged "<from_prefix>/<member>"
    admin_api_key: str | None = None  # any admin user's key; only for the queue check
    reuploads_per_pass: int = 10  # warn about a member above this many re-uploads in a pass
    config_dir: Path = field(default=Path("."), compare=False)


def _account(section: str, raw: dict, name: str) -> AccountConfig:
    for key in ("email", "api_key"):
        if not raw.get(key):
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


def parse_config(raw: dict, config_dir: Path = Path(".")) -> Config:
    immich = raw.get("immich") or {}
    if not immich.get("url"):
        raise ConfigError("[immich] url is required")

    family_raw = raw.get("family") or {}
    family = _account("family", family_raw, "family")
    dropbox_album = family_raw.get("dropbox_album", DEFAULTS["family"]["dropbox_album"])
    if not isinstance(dropbox_album, str) or not dropbox_album.strip():
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
    ledger = Path(service.get("ledger_path", DEFAULTS["service"]["ledger_path"]))
    if not ledger.is_absolute():
        ledger = config_dir / ledger

    limits = raw.get("limits") or {}
    albums_per_pass = limits.get("albums_per_pass", DEFAULTS["limits"]["albums_per_pass"])
    if not isinstance(albums_per_pass, int) or albums_per_pass < 0:
        raise ConfigError("[limits] albums_per_pass must be a whole number (0 for no limit)")
    assets_per_pass = limits.get("assets_per_pass", DEFAULTS["limits"]["assets_per_pass"])
    if not isinstance(assets_per_pass, int) or assets_per_pass < 0:
        raise ConfigError("[limits] assets_per_pass must be a whole number (0 for no limit)")

    settle = service.get("settle_seconds", DEFAULTS["service"]["settle_seconds"])
    if not isinstance(settle, int) or settle < 0:
        raise ConfigError("[service] settle_seconds must be a whole number of seconds")
    interval = service.get("interval_seconds", DEFAULTS["service"]["interval_seconds"])
    if not isinstance(interval, int) or interval < 1:
        raise ConfigError(
            "[service] interval_seconds must be a whole number of seconds, at least 1"
        )

    webhook = None
    webhook_raw = raw.get("webhook")
    if webhook_raw:
        for key in ("url", "token"):
            if not webhook_raw.get(key) or not isinstance(webhook_raw[key], str):
                raise ConfigError(f"[webhook] is missing '{key}'")
        path = webhook_raw.get("path", DEFAULTS["webhook"]["path"])
        if not isinstance(path, str) or not path.startswith("/"):
            raise ConfigError("[webhook] path must start with /")
        webhook = WebhookConfig(
            url=webhook_raw["url"],
            token=webhook_raw["token"],
            listen=str(webhook_raw.get("listen", DEFAULTS["webhook"]["listen"])),
            path=path,
        )

    tags = raw.get("tags") or {}
    moved_tag = tags.get("moved", DEFAULTS["tags"]["moved"])
    from_prefix = tags.get("from_prefix", DEFAULTS["tags"]["from_prefix"])
    for key, value in (("moved", moved_tag), ("from_prefix", from_prefix)):
        if not isinstance(value, str) or not value.strip() or value.strip("/") != value:
            raise ConfigError(
                f"[tags] {key} must be a tag name without leading or trailing slashes"
            )

    warnings = raw.get("warnings") or {}
    reuploads = warnings.get("reuploads_per_pass", DEFAULTS["warnings"]["reuploads_per_pass"])
    if not isinstance(reuploads, int) or reuploads < 0:
        raise ConfigError("[warnings] reuploads_per_pass must be a whole number")

    admin_api_key = immich.get("admin_api_key") or None
    if admin_api_key is not None and not isinstance(admin_api_key, str):
        raise ConfigError("[immich] admin_api_key must be a string")

    return Config(
        immich_url=immich["url"].rstrip("/"),
        family=family,
        dropbox_album=dropbox_album,
        members=members,
        ledger_path=ledger,
        readonly=bool(service.get("readonly", DEFAULTS["service"]["readonly"])),
        settle_seconds=settle,
        interval_seconds=interval,
        webhook=webhook,
        albums_per_pass=albums_per_pass,
        assets_per_pass=assets_per_pass,
        moved_tag=moved_tag,
        from_prefix=from_prefix,
        admin_api_key=admin_api_key,
        reuploads_per_pass=reuploads,
        config_dir=config_dir,
    )


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

    default_ledger = cfg.config_dir / DEFAULTS["service"]["ledger_path"]
    service = {
        "readonly": cfg.readonly,
        "ledger_path": str(cfg.ledger_path),
        "settle_seconds": cfg.settle_seconds,
        "interval_seconds": cfg.interval_seconds,
    }
    limits = {"albums_per_pass": cfg.albums_per_pass, "assets_per_pass": cfg.assets_per_pass}
    tags = {"moved": cfg.moved_tag, "from_prefix": cfg.from_prefix}
    warnings = {"reuploads_per_pass": cfg.reuploads_per_pass}
    if minimal:
        defaults = {
            **{("service", k): v for k, v in DEFAULTS["service"].items()},
            ("service", "ledger_path"): str(default_ledger),
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
            "path": cfg.webhook.path,
        }
        if minimal:
            for key in ("listen", "path"):
                if wh[key] == DEFAULTS["webhook"][key]:
                    del wh[key]
        out["webhook"] = wh
    return out
