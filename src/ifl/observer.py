"""Look at the instance and say what would happen. Never writes.

Each pass is three scans, in this order:

1. Albums. Member-owned albums shared with the family user become family-owned
   albums (conversion). Running this first means the later scans in the same
   pass already see the converted album as family-owned.
2. Family albums. The invariant: the dropbox exists, every configured member
   is an editor on every family-owned album, and the family partner-shares
   with every member (reconcile).
3. Assets. A member-owned asset sitting in a family-owned album gets moved to
   the family account. Plus re-uploads: a member-owned asset,
   uploaded since the last look, whose checksum the family already owns. The
   same move handles both; a re-upload just has nothing to upload.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from immichpy.client.generated.models.album_user_role import AlbumUserRole
from immichpy.client.generated.models.asset_response_dto import AssetResponseDto
from immichpy.client.generated.models.asset_visibility import AssetVisibility

from ifl.accounts import Account, Accounts
from ifl.config import WebhookConfig
from ifl.immich import (
    PROVENANCE_KEY,
    AlbumSummary,
    UserNames,
    find_workflow,
    get_asset_if_readable,
    get_asset_metadata_value,
    iter_album_assets,
    iter_assets_created_after,
    list_albums,
    list_partner_ids,
    owned_checksums,
    step_matches,
)

WORKFLOW_NAME = "immich-family-library"

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


@dataclass(frozen=True)
class WorkflowFix:
    member: Account
    existing: UUID | None  # None: create; else update this workflow's steps


@dataclass
class ReconcileScan:
    create_dropbox: str | None = None  # name of the dropbox album to create
    fixes: list[ShareFix] = field(default_factory=list)
    partner_with: frozenset[UUID] = frozenset()  # members the family doesn't share with yet
    workflows: list[WorkflowFix] = field(default_factory=list)  # webhook workflows to set up

    def __bool__(self) -> bool:
        return bool(self.create_dropbox or self.fixes or self.partner_with or self.workflows)


@dataclass(frozen=True)
class MoveAsset:
    asset_id: UUID
    owner: Account
    album: AlbumSummary | None  # the family album it was found in; None for a re-upload
    file_name: str
    reupload: bool = False


@dataclass(frozen=True)
class DropboxRemoval:
    """A photo the dropbox can do nothing for, to be taken back out of it."""

    asset_id: UUID
    owner: Account
    file_name: str


@dataclass
class AssetScan:
    moves: list[MoveAsset] = field(default_factory=list)
    blocked: list[Blocked] = field(default_factory=list)
    removals: list[DropboxRemoval] = field(default_factory=list)
    dropbox: AlbumSummary | None = None
    scanned_at: datetime | None = None  # when the re-upload scan looked
    skipped_reuploads: list[str] = field(default_factory=list)  # matches left alone, with why

    def reuploads_by_owner(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for m in self.moves:
            if m.reupload:
                out[m.owner.name] = out.get(m.owner.name, 0) + 1
        return out


def unmovable(asset: AssetResponseDto) -> str | None:
    """Why the move can't take this asset until someone changes it, or None.
    The search response carries both fields, so the scan asks before it lists
    the asset as a move, and the move asks again of the asset it reads."""
    if asset.visibility in (AssetVisibility.LOCKED, AssetVisibility.HIDDEN):
        return f"visibility is {asset.visibility.value}"
    if asset.live_photo_video_id is not None:
        return "live photo; not handled yet"
    return None


EXTERNAL = "in an external library, which the service doesn't support moving"


def external(asset: AssetResponseDto) -> bool:
    """Whether the file lives in a member's own folder, outside Immich's storage.
    The service doesn't support moving these."""
    return asset.library_id is not None


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
            # The walk is the outsider check: conversion re-reads the album itself.
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
            scan.conversions.append(ConvertAlbum(album=album, owner=member))
    return scan


async def family_albums(accounts: Accounts) -> list[AlbumSummary]:
    family = accounts.family
    return [a for a in await list_albums(family.client) if a.owner_id == family.user_id]


async def scan_reconcile(
    accounts: Accounts, dropbox_album: str, webhook: WebhookConfig | None = None
) -> ReconcileScan:
    """What it takes to make the family albums consistent. Never removes anyone.
    With a webhook configured, also which members lack the workflow that posts
    their uploads to it."""
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
    if webhook is not None:
        for member in accounts.members.values():
            existing = await find_workflow(member.client, WORKFLOW_NAME)
            if existing is None:
                scan.workflows.append(WorkflowFix(member, None))
            elif not (
                existing.enabled
                and len(existing.steps) == 1
                and step_matches(existing.steps[0], webhook.url, webhook.token)
            ):
                scan.workflows.append(WorkflowFix(member, existing.id))
    return scan


REUPLOAD_MARGIN = timedelta(minutes=10)  # re-read a little before the mark, uploads can land late
REUPLOAD_WINDOW = timedelta(days=3)  # how far back the re-upload scan ever reads


async def scan_assets(
    accounts: Accounts,
    dropbox_album: str,
    *,
    mark: datetime | None = None,
    reuploads: bool = True,
    now: datetime | None = None,
) -> AssetScan:
    """Member-owned assets in family-owned albums, plus re-uploads: members'
    recent assets whose checksum the family already owns. `mark` is when the
    caller's last finished re-upload scan looked, if it has one. A caller with
    its own way to find a re-upload turns that half off."""
    scan = AssetScan()
    by_id = accounts.by_user_id()
    family = accounts.family
    names = _names(accounts)

    albums = await family_albums(accounts)
    scan.dropbox = next((a for a in albums if a.name == dropbox_album), None)

    seen: set[UUID] = set()
    for album in albums:
        async for asset in iter_album_assets(family.client, album.id):
            if asset.owner_id == family.user_id or asset.is_trashed:
                continue
            owner = by_id.get(asset.owner_id)
            if owner is not None and external(asset):
                # Not a block: nothing the member does will change it. In an
                # album it is shared with the family where it sits. The dropbox
                # is for photos that end up in no album, so there it only
                # looks as if it worked, and it comes back out.
                if scan.dropbox is not None and album.id == scan.dropbox.id:
                    scan.removals.append(DropboxRemoval(asset.id, owner, asset.original_file_name))
                elif asset.id not in seen:
                    log.debug(
                        "leaving %s from %s in %r: %s",
                        asset.original_file_name,
                        owner.name,
                        album.name,
                        EXTERNAL,
                    )
                seen.add(asset.id)
                continue
            if asset.id in seen:
                continue
            seen.add(asset.id)
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
            if why := unmovable(asset):
                scan.blocked.append(
                    Blocked(
                        f"photo {asset.original_file_name} from {owner.name}, in {album.name!r}",
                        why,
                        asset_id=asset.id,
                    )
                )
                continue
            scan.moves.append(
                MoveAsset(
                    asset_id=asset.id,
                    owner=owner,
                    album=album,
                    file_name=asset.original_file_name,
                )
            )
    if reuploads:
        await _scan_reuploads(accounts, mark, scan, seen, now or datetime.now(UTC))
        # Re-uploads go first: each takes a moment, and behind the album moves
        # they would wait on every download and upload of the pass.
        scan.moves.sort(key=lambda m: not m.reupload)
    return scan


async def _scan_reuploads(
    accounts: Accounts, mark: datetime | None, scan: AssetScan, seen: set[UUID], now: datetime
) -> None:
    """Members' assets uploaded since the mark that the family already owns.

    The read never reaches back past the window, with a mark or without one. So
    a process that has just started reads the window once, and a mark that is
    held back cannot make the read grow. A re-upload older than the window is
    never looked for: it stays in the member's library as a duplicate."""
    scan.scanned_at = now
    since = now - REUPLOAD_WINDOW
    if mark is not None:
        since = max(since, mark - REUPLOAD_MARGIN)
    candidates: list[tuple[Account, list[AssetResponseDto]]] = []
    for member in accounts.members.values():
        recent = [
            a
            async for a in iter_assets_created_after(member.client, since)
            if a.owner_id == member.user_id and not a.is_trashed and a.id not in seen
        ]
        candidates.append((member, recent))
    await reupload_items(accounts, candidates, scan)


async def member_asset(
    accounts: Accounts, asset_id: UUID, owner_id: UUID | None = None
) -> tuple[Account, AssetResponseDto] | None:
    """The member who owns this asset, and the asset read with their key, or None
    when no member holds it live. `owner_id` is a hint, never trusted: that
    member is only asked first, which usually saves the other members' reads."""
    members = list(accounts.members.values())
    members.sort(key=lambda m: m.user_id != owner_id)
    for member in members:
        asset = await get_asset_if_readable(member.client, asset_id)
        if asset is not None and asset.owner_id == member.user_id and not asset.is_trashed:
            return member, asset
    return None


async def reuploads_among(
    accounts: Accounts, found: list[tuple[Account, AssetResponseDto]]
) -> AssetScan:
    """A scan of just these assets, each with the member who owns it: the
    re-uploads among them as move items, and the matches left alone as skipped.
    For a caller that has asset ids in hand, not a window to read."""
    by_member: dict[UUID, tuple[Account, list[AssetResponseDto]]] = {}
    for member, asset in found:
        by_member.setdefault(member.user_id, (member, []))[1].append(asset)
    scan = AssetScan()
    await reupload_items(accounts, list(by_member.values()), scan)
    return scan


async def reupload_items(
    accounts: Accounts, candidates: list[tuple[Account, list[AssetResponseDto]]], scan: AssetScan
) -> None:
    """Of these member-owned assets, the re-uploads become move items on `scan`.
    Shared by the pass and, through `reuploads_among`, the webhook receiver and `move`.

    A re-upload is a member's asset whose checksum the family holds in a copy
    that came from that same member: the copy's provenance entry names them.
    A copy the family got some other way, by a direct upload or from another
    member, makes no claim on this member's photo. Only a family album does.
    Nor is the photo the copy was moved from a re-upload: that is the member's
    original, back from the trash. Those, and the ones whose family copy is in
    the trash, are listed as skipped. An external-library photo is never a
    re-upload."""
    family = accounts.family
    for member, assets in candidates:
        recent = {a.checksum: a for a in assets if a.owner_id == member.user_id and not external(a)}
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
            provenance = await get_asset_metadata_value(
                family.client, copy.asset_id, PROVENANCE_KEY
            )
            if not provenance or provenance.get("origin_user_id") != str(member.user_id):
                scan.skipped_reuploads.append(
                    f"{a.original_file_name} from {member.name}: "
                    "the family's copy did not come from them"
                )
                continue
            if provenance.get("origin_asset_id") == str(a.id):
                scan.skipped_reuploads.append(
                    f"{a.original_file_name} from {member.name}: "
                    "it is the original the family's copy was moved from"
                )
                continue
            scan.moves.append(
                MoveAsset(
                    asset_id=a.id,
                    owner=member,
                    album=None,
                    file_name=a.original_file_name,
                    reupload=True,
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
    for w in scan.workflows:
        verb = "CREATE" if w.existing is None else "FIX"
        lines.append(f"{verb} webhook workflow for {w.member.name}")
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
    lines += [
        f"REMOVE {r.file_name} from {r.owner.name}, out of {scan.dropbox.name!r}: {EXTERNAL}"
        for r in scan.removals
        if scan.dropbox
    ]
    lines += [f"SKIP re-upload {s}" for s in scan.skipped_reuploads]
    lines += [f"BLOCKED {b.what}: {b.reason}" for b in scan.blocked]
    return lines or ["no assets to move"]
