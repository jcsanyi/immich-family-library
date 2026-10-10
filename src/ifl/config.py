"""Configuration loading. See config.example.toml for the shape."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


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
    poll_interval_seconds: int = 60
    ledger_path: Path = Path("data/ledger.sqlite")
    observe_only: bool = True
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
    if not family_raw.get("dropbox_album"):
        raise ConfigError("[family] dropbox_album is required")

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
    ledger = Path(service.get("ledger_path", "data/ledger.sqlite"))
    if not ledger.is_absolute():
        ledger = config_dir / ledger

    return Config(
        immich_url=immich["url"].rstrip("/"),
        family=family,
        dropbox_album=family_raw["dropbox_album"],
        members=members,
        poll_interval_seconds=int(service.get("poll_interval_seconds", 60)),
        ledger_path=ledger,
        observe_only=bool(service.get("observe_only", True)),
        config_dir=config_dir,
    )
