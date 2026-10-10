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

# Permissions the stages built so far need. Later stages extend these.
# DESIGN.md has the table and which call needs what.
MEMBER_PERMISSIONS = {
    "user.read",
    "apiKey.read",
    "album.read",
    "asset.read",
    "albumAsset.create",  # add own photos to a family album
    "album.delete",  # delete own album after conversion
    "asset.download",  # the original's bytes
    "asset.delete",  # trash the original
    "asset.edit.get",  # crop, rotate, mirror to carry over
    "assetFile.read",  # find the sidecar
    "assetFile.download",  # fetch the sidecar
    "tag.create",  # the moved tag, which also triggers the sidecar write
    "tag.asset",
    "sharedLink.read",  # a photo in a public link isn't moved
    "partner.read",  # warn when the member shares with a non-participant
    "workflow.read",  # the webhook workflow on each member's uploads
    "workflow.create",
    "workflow.update",
}
MEMBER_ONLY = {
    "tag.create",
    "tag.asset",
    "sharedLink.read",
    "workflow.read",
    "workflow.create",
    "workflow.update",
}
FAMILY_PERMISSIONS = (MEMBER_PERMISSIONS - MEMBER_ONLY) | {
    "album.create",
    "albumUser.create",
    "albumUser.update",  # promote a member who is only a viewer on a family album
    "partner.create",  # share the family library with each member
    "asset.upload",  # the family copy
    "asset.update",  # its provenance entry
    "asset.edit.create",  # its edits
}
# The admin key does one thing, read the job queues before a pass. The other two
# are what the startup check itself needs to confirm whose key it is.
ADMIN_PERMISSIONS = {"user.read", "apiKey.read", "queue.read"}


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
    admin: Account | None = None  # optional, queue check only; not a participant

    def by_user_id(self) -> dict[UUID, Account]:
        """Participants by id. The admin account isn't one."""
        out = {self.family.user_id: self.family}
        out.update({m.user_id: m for m in self.members.values()})
        return out

    async def close(self) -> None:
        for a in [self.family, *self.members.values(), *([self.admin] if self.admin else [])]:
            await a.client.close()


async def open_account(
    url: str,
    cfg: AccountConfig,
    *,
    required: set[str],
    is_family: bool = False,
    must_be_admin: bool = False,
) -> Account:
    client = AsyncClient(base_url=url, api_key=cfg.api_key)
    try:
        me = await client.users.get_my_user()
        if cfg.email is not None and me.email.lower() != cfg.email.lower():
            raise AccountError(
                f"[{cfg.name}] key belongs to {me.email}, config says {cfg.email}. "
                "Wrong key or wrong server."
            )
        if must_be_admin and not me.is_admin:
            raise AccountError(f"[{cfg.name}] key belongs to {me.email}, who is not an admin")
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
        admin = None
        if cfg.admin_api_key:
            admin_cfg = AccountConfig(name="admin", api_key=cfg.admin_api_key)
            admin = await open_account(
                cfg.immich_url, admin_cfg, required=ADMIN_PERMISSIONS, must_be_admin=True
            )
            opened.append(admin)
    except Exception:
        for a in opened:
            await a.client.close()
        raise
    return Accounts(family=family, members=members, admin=admin)
