"""Stage 1: look at the instance and say what would happen. Never writes.

Each poll is two scans, in this order:

1. Albums. Member-owned albums shared with the family user become family-owned
   albums (conversion). Running this first means the asset scan in the same
   poll already sees the converted album as family-owned.
2. Assets. The one rule: a member-owned asset sitting in a family-owned album
   gets moved to the family account.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import UUID

from ifl.accounts import Account, Accounts
from ifl.immich import AlbumSummary, UserNames, iter_album_assets, list_albums

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Blocked:
    """Something a rule applies to but we can't act on. Reported, seen again next poll."""

    what: str
    reason: str
    album_id: UUID | None = None


@dataclass(frozen=True)
class ConvertAlbum:
    album: AlbumSummary
    owner: Account
    contributor_ids: frozenset[UUID]


@dataclass
class AlbumScan:
    conversions: list[ConvertAlbum] = field(default_factory=list)
    blocked: list[Blocked] = field(default_factory=list)


@dataclass(frozen=True)
class MoveAsset:
    asset_id: UUID
    checksum: str
    owner: Account
    album: AlbumSummary
    file_name: str


@dataclass
class AssetScan:
    moves: list[MoveAsset] = field(default_factory=list)
    blocked: list[Blocked] = field(default_factory=list)
    dropbox: AlbumSummary | None = None


def _names(accounts: Accounts) -> UserNames:
    return UserNames(
        accounts.family.client, {uid: a.name for uid, a in accounts.by_user_id().items()}
    )


async def scan_albums(accounts: Accounts) -> AlbumScan:
    """Member-owned albums that have the family user on them."""
    scan = AlbumScan()
    by_id = accounts.by_user_id()
    family_id = accounts.family.user_id
    names = _names(accounts)

    for member in accounts.members.values():
        for album in await list_albums(member.client):
            if album.owner_id != member.user_id or family_id not in album.user_ids:
                continue
            contributors: set[UUID] = set()
            async for asset in iter_album_assets(member.client, album.id):
                contributors.add(asset.owner_id)
            outsiders = [c for c in contributors if c not in by_id]
            if outsiders:
                who = sorted([await names.describe(c, album) for c in outsiders])
                scan.blocked.append(
                    Blocked(
                        f"album {album.name!r} owned by {member.name}",
                        f"has photos from non-participants: {', '.join(who)}",
                        album_id=album.id,
                    )
                )
                continue
            scan.conversions.append(
                ConvertAlbum(album=album, owner=member, contributor_ids=frozenset(contributors))
            )
    return scan


async def scan_assets(accounts: Accounts, dropbox_album: str) -> AssetScan:
    """Member-owned assets in family-owned albums."""
    scan = AssetScan()
    by_id = accounts.by_user_id()
    family = accounts.family
    names = _names(accounts)

    family_albums = [a for a in await list_albums(family.client) if a.owner_id == family.user_id]
    scan.dropbox = next((a for a in family_albums if a.name == dropbox_album), None)
    if scan.dropbox is None:
        scan.blocked.append(Blocked("dropbox", f"no family-owned album named {dropbox_album!r}"))

    seen: set[UUID] = set()
    for album in family_albums:
        async for asset in iter_album_assets(family.client, album.id):
            if asset.owner_id == family.user_id or asset.id in seen or asset.is_trashed:
                continue
            seen.add(asset.id)
            owner = by_id.get(asset.owner_id)
            if owner is None:
                who = await names.describe(asset.owner_id, album)
                scan.blocked.append(
                    Blocked(
                        f"photo {asset.original_file_name} in {album.name!r}",
                        f"owner {who} is not a participant (no API key)",
                    )
                )
                continue
            scan.moves.append(
                MoveAsset(
                    asset_id=asset.id,
                    checksum=asset.checksum,
                    owner=owner,
                    album=album,
                    file_name=asset.original_file_name,
                )
            )
    return scan


def describe_albums(scan: AlbumScan) -> list[str]:
    lines = [
        f"CONVERT album {c.album.name!r} ({c.album.asset_count} assets) owned by {c.owner.name}"
        for c in scan.conversions
    ]
    lines += [f"BLOCKED {b.what}: {b.reason}" for b in scan.blocked]
    return lines or ["no albums to convert"]


def describe_assets(scan: AssetScan) -> list[str]:
    lines = []
    for m in scan.moves:
        tag = " [dropbox]" if scan.dropbox and m.album.id == scan.dropbox.id else ""
        lines.append(f"MOVE {m.file_name} from {m.owner.name}, in {m.album.name!r}{tag}")
    lines += [f"BLOCKED {b.what}: {b.reason}" for b in scan.blocked]
    return lines or ["no assets to move"]
