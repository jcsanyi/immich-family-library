"""Configuration loading. See config.example.toml for the shape."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class AccountConfig:
    name: str
    api_key: str
    email: str | None = None  # to be matched against the key, or None if we don't care (admin key)


# a valid host:port value: [ipv6]:port, ipv4:port, hostname:port, or :port for every address
_HOST_PORT = re.compile(r"(?:\[(?P<v6>[^\[\]]+)\]|(?P<host>[^:\[\]]*)):(?P<port>[0-9]+)")


# The models below are the file's shape: every section and key it may hold, each with its
# type and its default. A key with no default must be set. config.example.toml shows every
# key, with these values where there's a default, and explains how each should be set.
class _Section(BaseModel):
    # Strict, so a value of the wrong type is an error and never a guess:
    # `readonly = "false"` would otherwise read as true, and `true` as the int 1.
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class ImmichConfig(_Section):
    url: str = Field(min_length=1)
    admin_api_key: str | None = None  # any admin user's key; only for the queue check

    @field_validator("url")
    @classmethod
    def _no_trailing_slash(cls, url: str) -> str:
        return url.rstrip("/")

    @field_validator("admin_api_key")
    @classmethod
    def _empty_is_none(cls, key: str | None) -> str | None:
        return key or None


class ServiceConfig(_Section):
    readonly: bool = True
    lock_path: Path = Field(Path("run.lock"), validate_default=True)  # the run lock; see lock.py
    # Holds one original at a time, between its download and its upload.
    temp_dir: Path = Field(Path("/tmp/immich-family-library"), validate_default=True)
    settle_seconds: int = Field(60, ge=0)  # leave an asset alone this long after its last change
    interval_seconds: int = Field(600, ge=1)  # the daemon's pause between passes

    @field_validator("lock_path", "temp_dir", mode="before")
    @classmethod
    def _resolve(cls, path: object, info: ValidationInfo) -> Path:
        if not isinstance(path, str | Path) or not str(path):
            raise ValueError("must be a non-empty path")
        # Relative to the config file. An absolute path replaces config_dir.
        return (info.context or {}).get("config_dir", Path(".")) / path


class WebhookConfig(_Section):
    url: str  # what Immich calls, e.g. http://ifl:8080/webhook/asset-created
    token: str = Field(min_length=1)  # Immich sends it back in the X-IFL-Token header
    listen: str = "0.0.0.0:8080"

    @field_validator("url")
    @classmethod
    def _http_url(cls, url: str) -> str:
        try:
            parts = urlsplit(url)
            ok = parts.scheme in ("http", "https") and bool(parts.hostname)
        except ValueError:
            ok = False
        if not ok:
            raise ValueError("must be an http or https URL")
        return url

    @field_validator("listen")
    @classmethod
    def _host_port(cls, listen: str) -> str:
        # Port 0 binds, but Immich has to be told a port to call.
        m = _HOST_PORT.fullmatch(listen)
        if not m or not 1 <= int(m["port"]) <= 65535:
            raise ValueError(
                'must be "host:port" with a port from 1 to 65535, '
                'for example "0.0.0.0:8080", ":8080" or "[::]:8080"'
            )
        return listen

    @property
    def path(self) -> str:
        """What the receiver serves: derived from the path Immich is told to call."""
        return urlsplit(self.url).path or "/"

    @property
    def bind(self) -> tuple[str, int]:
        """The host and port to bind, derived from `listen`. An empty host means every
        address. Raises ValueError when `listen` is not `host:port`."""
        m = _HOST_PORT.fullmatch(self.listen)
        if not m:
            raise ValueError(f"not host:port: {self.listen!r}")
        return m["v6"] or m["host"] or "0.0.0.0", int(m["port"])


class LimitsConfig(_Section):
    albums_per_pass: int = Field(5, ge=0)  # 0 means no limit
    assets_per_pass: int = Field(20, ge=0)  # 0 means no limit


class TagsConfig(_Section):
    moved: str = "family"  # goes on a member's original when it's moved
    from_prefix: str = "from"  # the family copy is tagged "<from_prefix>/<member>"

    @field_validator("moved", "from_prefix")
    @classmethod
    def _tag_name(cls, name: str) -> str:
        if not name.strip() or name.strip("/") != name:
            raise ValueError("must be a tag name without leading or trailing slashes")
        return name


class WarningsConfig(_Section):
    report_at: str = "22:00"  # local time of day for the daily re-upload report

    # Before the type check: TOML has its own time type, so `report_at = 22:00:00`
    # arrives as a time, and the generic "should be a valid string" would not say why.
    @field_validator("report_at", mode="before")
    @classmethod
    def _hh_mm(cls, value: object) -> object:
        if isinstance(value, str) and re.fullmatch(r"\d\d:\d\d", value):
            try:
                time.fromisoformat(value)
                return value
            except ValueError:
                pass
        raise ValueError('must be a time of day as "HH:MM"')

    @property
    def report_time(self) -> time:
        return time.fromisoformat(self.report_at)


class MemberConfig(_Section):
    email: str = Field(min_length=1)
    api_key: str = Field(min_length=1)


class FamilyConfig(MemberConfig):
    dropbox_album: str = "Family Dropbox"

    @field_validator("dropbox_album")
    @classmethod
    def _album_name(cls, name: str) -> str:
        if not name.strip():
            raise ValueError("must be a non-empty album name")
        return name


class Config(_Section):
    immich: ImmichConfig
    # A section the file leaves out is validated from {}, so its paths resolve too.
    service: ServiceConfig = Field({}, validate_default=True)
    webhook: WebhookConfig | None = None  # the daemon's receiver; None means no webhook
    limits: LimitsConfig = Field({}, validate_default=True)
    tags: TagsConfig = Field({}, validate_default=True)
    warnings: WarningsConfig = Field({}, validate_default=True)
    family: FamilyConfig
    members: dict[str, MemberConfig] = {}  # one per [members.<name>] table

    _config_dir: Path = PrivateAttr(Path("."))

    @model_validator(mode="after")
    def _accounts(self) -> Config:
        if not self.members:
            raise ValueError("at least one [members.<name>] section is required")
        emails = [self.family.email, *(m.email for m in self.members.values())]
        for email in emails:
            if emails.count(email) > 1:
                raise ValueError(f"email {email} is used by more than one account")
        return self

    @property
    def family_account(self) -> AccountConfig:
        return AccountConfig("family", self.family.api_key, self.family.email)

    @property
    def member_accounts(self) -> dict[str, AccountConfig]:
        return {name: AccountConfig(name, m.api_key, m.email) for name, m in self.members.items()}


def _describe(error: dict) -> str:
    """One pydantic error as one line, e.g. `members.a.api_key: Field required`."""
    where = ".".join(str(part) for part in error["loc"])
    # A ValueError from our own validators would otherwise read "Value error, ...".
    what = str(error["ctx"]["error"]) if error["type"] == "value_error" else error["msg"]
    return f"{where}: {what}" if where else what


def load_config(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"cannot read the config file {path}: {e}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    return parse_config(raw, config_dir=path.parent)


def parse_config(raw: dict, config_dir: Path = Path(".")) -> Config:
    try:
        cfg = Config.model_validate(raw, context={"config_dir": config_dir})
    except ValidationError as e:
        raise ConfigError("\n".join(_describe(error) for error in e.errors())) from None
    cfg._config_dir = config_dir
    return cfg


def redact(secret: str) -> str:
    """Stars for all but the last four characters, enough to tell keys apart."""
    return "*" * (len(secret) - 4) + secret[-4:] if len(secret) > 4 else "*" * len(secret)


def to_raw(cfg: Config, *, minimal: bool = False) -> dict:
    """The effective config in the file's shape, secrets redacted.

    Paths come out resolved, as the service uses them. With `minimal`, settings
    that have a default are left out when they match it; everything required is
    always included.
    """
    out = cfg.model_dump(mode="json", exclude_defaults=minimal, exclude_none=True)
    for account in (out["family"], *out["members"].values()):
        account["api_key"] = redact(account["api_key"])
    if "admin_api_key" in out["immich"]:
        out["immich"]["admin_api_key"] = redact(out["immich"]["admin_api_key"])
    if "webhook" in out:
        out["webhook"]["token"] = redact(out["webhook"]["token"])
    if minimal:
        # The one default that isn't a constant: it resolves against the config file.
        service = out.get("service", {})
        if service.get("lock_path") == str(cfg._config_dir / "run.lock"):
            del service["lock_path"]
        out = {section: settings for section, settings in out.items() if settings}
    return out
