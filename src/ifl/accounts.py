"""Per-account Immich clients and the startup checks that guard them.

Every account the service acts for gets its own client built from that
account's API key. Before anything else happens we check the key works, belongs
to the account the config says it does, and carries the permissions the stage
needs. A config pointed at the wrong server fails here, not later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from immichpy.client.main import AsyncClient

from ifl.config import AccountConfig, Config

log = logging.getLogger(__name__)

MIN_SERVER = (3, 3, 0)

# Permissions stage 1 needs. Later stages extend these.
MEMBER_PERMISSIONS = {"user.read", "apiKey.read", "album.read", "asset.read"}
FAMILY_PERMISSIONS = MEMBER_PERMISSIONS | {"partner.read"}


class AccountError(Exception):
    pass


@dataclass
class Account:
    name: str
    email: str
    user_id: UUID
    client: AsyncClient
    is_family: bool = False

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>"


@dataclass
class Accounts:
    family: Account
    members: dict[str, Account]

    def by_user_id(self) -> dict[UUID, Account]:
        out = {self.family.user_id: self.family}
        out.update({m.user_id: m for m in self.members.values()})
        return out

    async def close(self) -> None:
        for a in [self.family, *self.members.values()]:
            await a.client.close()


async def open_account(
    url: str, cfg: AccountConfig, *, required: set[str], is_family: bool = False
) -> Account:
    client = AsyncClient(base_url=url, api_key=cfg.api_key)
    try:
        me = await client.users.get_my_user()
        if me.email.lower() != cfg.email.lower():
            raise AccountError(
                f"[{cfg.name}] key belongs to {me.email}, config says {cfg.email}. "
                "Wrong key or wrong server."
            )
        key = await client.api_keys.get_my_api_key()
        have = {str(p.value) for p in key.permissions}
        missing = required - have if "all" not in have else set()
        if missing:
            raise AccountError(f"[{cfg.name}] key is missing permissions: {sorted(missing)}")
    except AccountError:
        await client.close()
        raise
    except Exception as e:
        await client.close()
        raise AccountError(f"[{cfg.name}] key check failed: {e}") from e
    log.info("account ok: %s (%s)", cfg.name, me.email)
    return Account(name=cfg.name, email=me.email, user_id=me.id, client=client, is_family=is_family)


async def check_server(client: AsyncClient) -> str:
    v = await client.server.get_server_version()
    version = (v.major, v.minor, v.patch)
    if version < MIN_SERVER:
        raise AccountError(
            f"server is {'.'.join(map(str, version))}, need {'.'.join(map(str, MIN_SERVER))}+"
        )
    return ".".join(map(str, version))


async def open_accounts(cfg: Config) -> Accounts:
    opened: list[Account] = []
    try:
        family = await open_account(
            cfg.immich_url, cfg.family, required=FAMILY_PERMISSIONS, is_family=True
        )
        opened.append(family)
        version = await check_server(family.client)
        log.info("server %s at %s", version, cfg.immich_url)
        members: dict[str, Account] = {}
        for name, m in cfg.members.items():
            members[name] = await open_account(cfg.immich_url, m, required=MEMBER_PERMISSIONS)
            opened.append(members[name])
    except Exception:
        for a in opened:
            await a.client.close()
        raise
    return Accounts(family=family, members=members)
