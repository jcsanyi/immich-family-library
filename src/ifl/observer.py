"""Look at the instance and say what would happen. Never writes.

Each pass is three scans, in this order:

1. Albums. Member-owned albums shared with the family user become family-owned
   albums (conversion). Running this first means the later scans in the same
   pass already see the converted album as family-owned.
2. Family albums. The invariant: the dropbox exists and every configured
   member is an editor on every family-owned album (reconcile).
3. Assets. The one rule: a member-owned asset sitting in a family-owned album
   gets moved to the family account.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import UUID

from immichpy.client.generated.models.album_user_role import AlbumUserRole

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
class ShareFix:
    """Members a family album is missing, or has only as viewers."""

    album: AlbumSummary
    add: frozenset[UUID]
    promote: frozenset[UUID]


@dataclass
class ReconcileScan:
    create_dropbox: str | None = None  # name of the dropbox album to create
    fixes: list[ShareFix] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.create_dropbox or self.fixes)


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


async def family_albums(accounts: Accounts) -> list[AlbumSummary]:
    family = accounts.family
    return [a for a in await list_albums(family.client) if a.owner_id == family.user_id]


async def scan_reconcile(accounts: Accounts, dropbox_album: str) -> ReconcileScan:
    """What it takes to make the family albums consistent. Never removes anyone."""
    scan = ReconcileScan()
    albums = await family_albums(accounts)
    if not any(a.name == dropbox_album for a in albums):
        scan.create_dropbox = dropbox_album
    members = {m.user_id for m in accounts.members.values()}
    for album in albums:
        add = members - album.user_ids
        promote = {
            u for u in members & album.user_ids if album.roles.get(u) != AlbumUserRole.EDITOR
        }
        if add or promote:
            scan.fixes.append(ShareFix(album, frozenset(add), frozenset(promote)))
    return scan


async def scan_assets(accounts: Accounts, dropbox_album: str) -> AssetScan:
    """Member-owned assets in family-owned albums."""
    scan = AssetScan()
    by_id = accounts.by_user_id()
    family = accounts.family
    names = _names(accounts)

    albums = await family_albums(accounts)
    scan.dropbox = next((a for a in albums if a.name == dropbox_album), None)

    seen: set[UUID] = set()
    for album in albums:
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


def describe_reconcile(scan: ReconcileScan, accounts: Accounts) -> list[str]:
    names = {uid: a.name for uid, a in accounts.by_user_id().items()}
    lines = []
    if scan.create_dropbox:
        lines.append(f"CREATE dropbox {scan.create_dropbox!r}")
    for f in scan.fixes:
        if f.add:
            lines.append(
                f"SHARE {f.album.name!r} with {', '.join(sorted(names[u] for u in f.add))}"
            )
        if f.promote:
            who = ", ".join(sorted(names[u] for u in f.promote))
            lines.append(f"PROMOTE {who} to editor on {f.album.name!r}")
    return lines or ["family albums consistent"]


def describe_assets(scan: AssetScan) -> list[str]:
    lines = []
    for m in scan.moves:
        tag = " [dropbox]" if scan.dropbox and m.album.id == scan.dropbox.id else ""
        lines.append(f"MOVE {m.file_name} from {m.owner.name}, in {m.album.name!r}{tag}")
    lines += [f"BLOCKED {b.what}: {b.reason}" for b in scan.blocked]
    return lines or ["no assets to move"]
