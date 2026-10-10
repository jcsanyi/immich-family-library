"""Configuration loading. See config.example.toml for the shape."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS = {
    "service": {"readonly": True, "ledger_path": "data/ledger.sqlite"},
    "family": {"dropbox_album": "Family Dropbox"},
    "limits": {"albums_per_pass": 5},
}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class AccountConfig:
    name: str
    email: str
    api_key: str


@dataclass(frozen=True)
class Config:
    immich_url: str
    family: AccountConfig
    dropbox_album: str
    members: dict[str, AccountConfig]
    ledger_path: Path = Path("data/ledger.sqlite")
    readonly: bool = True
    albums_per_pass: int = 5  # 0 means no limit
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

    return Config(
        immich_url=immich["url"].rstrip("/"),
        family=family,
        dropbox_album=dropbox_album,
        members=members,
        ledger_path=ledger,
        readonly=bool(service.get("readonly", DEFAULTS["service"]["readonly"])),
        albums_per_pass=albums_per_pass,
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
        return {"email": a.email, "api_key": redact(a.api_key)}

    default_ledger = cfg.config_dir / DEFAULTS["service"]["ledger_path"]
    service = {"readonly": cfg.readonly, "ledger_path": str(cfg.ledger_path)}
    limits = {"albums_per_pass": cfg.albums_per_pass}
    if minimal:
        if cfg.readonly == DEFAULTS["service"]["readonly"]:
            del service["readonly"]
        if cfg.ledger_path == default_ledger:
            del service["ledger_path"]
        if cfg.albums_per_pass == DEFAULTS["limits"]["albums_per_pass"]:
            del limits["albums_per_pass"]

    out: dict = {"immich": {"url": cfg.immich_url}}
    if service:
        out["service"] = service
    if limits:
        out["limits"] = limits
    out["family"] = account(cfg.family)
    if not minimal or cfg.dropbox_album != DEFAULTS["family"]["dropbox_album"]:
        out["family"]["dropbox_album"] = cfg.dropbox_album
    out["members"] = {name: account(m) for name, m in cfg.members.items()}
    return out
