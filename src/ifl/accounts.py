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

from ifl import __version__
from ifl.config import AccountConfig, Config
from ifl.immich import ImmichError, describe_error, guard

log = logging.getLogger(__name__)

# The Immich minor that the pinned client was generated from. Nobody has run
# the service against a later one.
MIN_SERVER = (3, 3, 0)

# docs/configuration.md has the table and which call needs what. No set has
# `apiKey.read`: a key reads its own entry, as the startup check does, without it.
MEMBER_PERMISSIONS = {
    "user.read",
    "album.read",
    "asset.read",
    "albumAsset.create",  # add own photos to a family album
    "albumAsset.delete",  # a moved original out of its albums; external photos out of the dropbox
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
# Written out, not derived from the member set: the family key reads no
# original and deletes no album, and should not have to be able to.
FAMILY_PERMISSIONS = {
    "user.read",
    "album.read",
    "album.create",
    "albumUser.create",
    "albumUser.update",  # promote a member who is only a viewer on a family album
    "albumAsset.create",  # the copy into the family albums
    "asset.read",
    "asset.upload",  # the family copy, and the checksum lookup
    "asset.update",  # its provenance entry
    "asset.edit.create",  # its edits
    "asset.delete",  # only to restore a trashed copy; the service trashes nothing of the family's
    "partner.read",
    "partner.create",  # share the family library with each member
}
# The admin key does one thing, read the job queues before a pass. `user.read`
# is what the startup check needs to confirm whose key it is.
ADMIN_PERMISSIONS = {"user.read", "queue.read"}


class AccountError(Exception):
    pass


@dataclass
class Account:
    name: str
    email: str
    user_id: UUID
    client: AsyncClient

    def __post_init__(self) -> None:
        # The helpers catch `ImmichError` and nothing else, so no account may
        # hold a bare client, whoever built the account.
        if isinstance(self.client, AsyncClient):
            self.client = guard(self.client)

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
            await close_client(a.client)


async def close_client(client: AsyncClient) -> None:
    """Closing is housekeeping at the end. A client that will not close must not
    stop the others from closing, or change what the command reports."""
    try:
        await client.close()
    except Exception as e:  # noqa: BLE001 - see above; there is nothing to do about it either
        log.debug("could not close a client: %s", describe_error(e))


async def open_account(
    url: str,
    cfg: AccountConfig,
    *,
    required: set[str],
    must_be_admin: bool = False,
    check_version: bool = False,
) -> Account:
    client = guard(AsyncClient(base_url=url, api_key=cfg.api_key))
    try:
        if check_version:
            # Before anything else. An Immich that is too old answers the key
            # check with a reply the client cannot read, and the version is
            # what explains that.
            log.info("ifl %s, server %s at %s", __version__, await check_server(client), url)
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
    except BaseException as e:
        await close_client(client)
        # Immich's answer to a key is the check itself: a refusal is a wrong key.
        # An outage says nothing about the key, and rises as itself.
        if isinstance(e, ImmichError) and e.refused:
            raise AccountError(f"[{cfg.name}] key check failed: {e}") from e
        raise
    log.info("account ok: %s (%s)", cfg.name, me.email)
    return Account(name=cfg.name, email=me.email, user_id=me.id, client=client)


async def check_server(client: AsyncClient) -> str:
    try:
        v = await client.server.get_server_version()
    except ImmichError as e:
        if not e.refused:
            raise
        # Still the startup check, so it reports as the key check beside it does.
        raise AccountError(f"server version check failed: {e}") from e
    version = (v.major, v.minor, v.patch)
    if version < MIN_SERVER:
        raise AccountError(
            f"server is {'.'.join(map(str, version))}, need {'.'.join(map(str, MIN_SERVER))}+"
        )
    if version[:2] > MIN_SERVER[:2]:
        log.warning(
            "server is %s; this release was built for Immich %d.%d and is not tested on a "
            "later one. Look for a matching release of the service.",
            ".".join(map(str, version)),
            *MIN_SERVER[:2],
        )
    return ".".join(map(str, version))


async def open_accounts(cfg: Config) -> Accounts:
    opened: list[Account] = []
    try:
        family = await open_account(
            cfg.immich.url, cfg.family_account, required=FAMILY_PERMISSIONS, check_version=True
        )
        opened.append(family)
        members: dict[str, Account] = {}
        # The config compares emails as written; Immich's user id is what counts.
        # The admin key is left out: it may be the family's or a member's.
        participants = {family.user_id: family}
        for name, m in cfg.member_accounts.items():
            members[name] = await open_account(cfg.immich.url, m, required=MEMBER_PERMISSIONS)
            opened.append(members[name])
            other = participants.setdefault(members[name].user_id, members[name])
            if other is not members[name]:
                raise AccountError(
                    f"[{other.name}] and [{name}] are the same Immich user, {other.email}. "
                    "Each account can be configured once."
                )
        admin = None
        if cfg.immich.admin_api_key:
            admin_cfg = AccountConfig(name="admin", api_key=cfg.immich.admin_api_key)
            admin = await open_account(
                cfg.immich.url, admin_cfg, required=ADMIN_PERMISSIONS, must_be_admin=True
            )
            opened.append(admin)
    except Exception:
        for a in opened:
            await close_client(a.client)
        raise
    return Accounts(family=family, members=members, admin=admin)
