"""Look at the instance and say what would happen. Never writes.

Each pass is three scans, in this order:

1. Albums. Member-owned albums shared with the family user become family-owned
   albums (conversion). Running this first means the later scans in the same
   pass already see the converted album as family-owned.
2. Family albums. The invariant: the dropbox exists, every configured member
   is an editor on every family-owned album, and the family partner-shares
   with every member (reconcile).
3. Assets. The one rule: a member-owned asset sitting in a family-owned album
   gets moved to the family account. Plus re-uploads: a member-owned asset,
   uploaded since the last look, whose checksum the family already owns. The
   same move handles both; a re-upload just has nothing to upload.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from immichpy.client.generated.models.album_user_role import AlbumUserRole

from ifl.accounts import Account, Accounts
from ifl.immich import (
    AlbumSummary,
    UserNames,
    iter_album_assets,
    iter_assets_created_after,
    list_albums,
    list_partner_ids,
    owned_checksums,
)
from ifl.ledger import Ledger

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Blocked:
    """Something a rule applies to but we can't act on. Reported, seen again next poll."""

    what: str
    reason: str
    album_id: UUID | None = None
    asset_id: UUID | None = None


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
    partner_with: frozenset[UUID] = frozenset()  # members the family doesn't share with yet

    def __bool__(self) -> bool:
        return bool(self.create_dropbox or self.fixes or self.partner_with)


@dataclass(frozen=True)
class MoveAsset:
    asset_id: UUID
    checksum: str
    owner: Account
    album: AlbumSummary | None  # the family album it was found in; None for a re-upload
    file_name: str
    reupload: bool = False
    created_at: datetime | None = None  # for advancing the re-upload mark


@dataclass
class AssetScan:
    moves: list[MoveAsset] = field(default_factory=list)
    blocked: list[Blocked] = field(default_factory=list)
    dropbox: AlbumSummary | None = None
    scanned_at: datetime | None = None  # when the re-upload scan looked; None if it didn't
    skipped_reuploads: list[str] = field(default_factory=list)  # family copy in the trash

    def reuploads_by_owner(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for m in self.moves:
            if m.reupload:
                out[m.owner.name] = out.get(m.owner.name, 0) + 1
        return out


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
            if album.has_shared_link:
                scan.blocked.append(
                    Blocked(
                        f"album {album.name!r} owned by {member.name}",
                        "has a public shared link that deleting the album would break; "
                        "remove the link to convert it",
                        album_id=album.id,
                    )
                )
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
    scan.partner_with = frozenset(members - await list_partner_ids(accounts.family.client))
    return scan


REUPLOAD_MARGIN = timedelta(minutes=10)  # re-read a little before the mark, uploads can land late


async def scan_assets(
    accounts: Accounts,
    dropbox_album: str,
    *,
    ledger: Ledger | None = None,
    now: datetime | None = None,
) -> AssetScan:
    """Member-owned assets in family-owned albums, plus, when a ledger is given,
    re-uploads: members' recent assets whose checksum the family already owns."""
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
                        asset_id=asset.id,
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
    if ledger is not None:
        await _scan_reuploads(accounts, ledger, scan, seen, now or datetime.now(UTC))
    return scan


async def _scan_reuploads(
    accounts: Accounts, ledger: Ledger, scan: AssetScan, seen: set[UUID], now: datetime
) -> None:
    """Members' assets uploaded since the last look that the family already owns."""
    scan.scanned_at = now
    family = accounts.family
    for member in accounts.members.values():
        mark = ledger.get_mark(member.user_id)
        since = mark - REUPLOAD_MARGIN if mark else None
        recent = {}
        async for a in iter_assets_created_after(member.client, since):
            if a.owner_id == member.user_id and not a.is_trashed and a.id not in seen:
                recent[a.checksum] = a
        if not recent:
            continue
        owned = await owned_checksums(family.client, recent)
        for checksum, copy in owned.items():
            a = recent[checksum]
            if copy.is_trashed:
                scan.skipped_reuploads.append(
                    f"{a.original_file_name} from {member.name}: the family's copy is in the trash"
                )
                continue
            scan.moves.append(
                MoveAsset(
                    asset_id=a.id,
                    checksum=checksum,
                    owner=member,
                    album=None,
                    file_name=a.original_file_name,
                    reupload=True,
                    created_at=a.created_at,
                )
            )


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
    if scan.partner_with:
        who = ", ".join(sorted(names[u] for u in scan.partner_with))
        lines.append(f"PARTNER-SHARE the family library with {who}")
    return lines or ["family albums consistent"]


def describe_assets(scan: AssetScan) -> list[str]:
    lines = []
    for m in scan.moves:
        if m.reupload:
            lines.append(f"REUPLOAD {m.file_name} from {m.owner.name}: family already has it")
            continue
        tag = " [dropbox]" if scan.dropbox and m.album and m.album.id == scan.dropbox.id else ""
        name = m.album.name if m.album else "?"
        lines.append(f"MOVE {m.file_name} from {m.owner.name}, in {name!r}{tag}")
    lines += [f"SKIP re-upload {s}" for s in scan.skipped_reuploads]
    lines += [f"BLOCKED {b.what}: {b.reason}" for b in scan.blocked]
    return lines or ["no assets to move"]
