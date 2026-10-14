"""The per-asset move.

A member-owned asset in a family-owned album is re-uploaded under the family
account with everything Immich lets us carry, re-linked into the albums it was
in, and the original is trashed. docs/how-it-works.md walks through every step
and the key each call runs under. In short:

- read everything with the owner's key: the asset, its albums, its edits;
- tag the original with the moved tag, which makes Immich write a fresh
  sidecar from its own exif row, and wait for it;
- rewrite only the sidecar's tag list, upload bytes plus sidecar as the family,
  apply the edits, record provenance, add the copy to the family albums;
- trash the original.

State comes from Immich at every step: the family already owning the checksum
means the upload is done, the original still existing means it hasn't been
trashed. So a move that died anywhere is finished by running it again.

The same path handles a duplicate, a member's asset whose bytes the family
already has. The moved tag on the original tells the two apart: the service
puts it there before it uploads, so an original wearing it is an interrupted
move and the member's asset is authoritative. An original without it is a
re-upload, or a photo the family got some other way, and the family copy is
authoritative: edits and provenance are left alone, the copy is re-linked into
any album the duplicate sits in, and the duplicate is trashed. The copy's `ifl`
entry is a veto on top: if it names a different original, the copy already has
its owner, whatever tag this asset wears. A family copy found in the trash
while a member shares the photo again is restored first.

The albums an asset is in are listed with every participant's key, since the
asset's owner can't see an album they aren't on. Re-linking into an album the
family doesn't own runs under the key of a participant who owns it or is an
editor on it, the album's owner first. That works because the family
partner-shares with every member. A re-link that fails stops the move before
the trash, so the photo never silently vanishes from an album the service can
see.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from immichpy.client.generated.models.album_user_role import AlbumUserRole

from ifl.accounts import Account, Accounts
from ifl.config import Config, ConfigError
from ifl.immich import (
    PROVENANCE_KEY,
    AlbumSummary,
    Failure,
    ImmichError,
    add_album_assets,
    albums_containing,
    bulk_failure,
    download_asset_file,
    download_original,
    existing_asset_by_checksum,
    find_sidecar,
    get_asset,
    get_asset_edits,
    get_asset_metadata_value,
    list_partner_ids,
    log_failure,
    queue_backlog,
    restore_assets,
    set_asset_edits,
    set_asset_metadata,
    shared_link_asset_ids,
    tag_asset,
    trash_assets,
    upload_asset,
    upsert_tag,
)
from ifl.observer import MoveAsset, unmovable
from ifl.sidecar import tags_in, with_only_tag

log = logging.getLogger(__name__)

WATCHED_QUEUES = ("sidecar", "metadataExtraction")
TEMP_PREFIX = "ifl-"  # the only files the service removes from the temp directory


class MoveBlocked(Exception):
    """Not now. The reason is for the log; the asset is found again next pass."""


class MoveTimedOut(MoveBlocked):
    """Blocked after a wait on Immich, so the try cost the pass real time."""


class MoveError(Failure):
    """Something failed partway. Running the move again resumes it."""


@dataclass(frozen=True)
class MoveOptions:
    dropbox_album: str
    settle_seconds: int
    moved_tag: str
    from_prefix: str
    temp_dir: Path
    sidecar_timeout: float = 15.0
    extraction_timeout: float = 30.0  # edits need the copy's dimensions, which extraction sets

    @classmethod
    def from_config(cls, cfg: Config) -> MoveOptions:
        return cls(
            dropbox_album=cfg.dropbox_album,
            settle_seconds=cfg.settle_seconds,
            moved_tag=cfg.moved_tag,
            from_prefix=cfg.from_prefix,
            temp_dir=cfg.temp_dir,
        )


def prepare_temp_dir(temp_dir: Path) -> None:
    """Make the temp directory, prove it can be written, and clear what a killed
    move left in it. Only for the holder of the run lock: nobody else is
    mid-move, so every file of ours in there is a leftover."""
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        for leftover in temp_dir.glob(f"{TEMP_PREFIX}*"):
            leftover.unlink()
            log.info("removed leftover %s", leftover)
        probe = temp_dir / f"{TEMP_PREFIX}probe"
        probe.touch()
        probe.unlink()
    except OSError as e:
        raise ConfigError(f"[service] temp_dir {temp_dir} is not usable: {e}") from e


@dataclass
class MoveResult:
    asset_id: UUID
    owner_id: UUID
    family_asset_id: UUID
    file_name: str
    uploaded: bool  # False when the family already had the checksum
    edits: int
    duplicate: bool = False  # a re-upload of something already moved; nothing rewritten
    restored: bool = False  # the family copy came back out of the trash
    reupload: bool = False  # came from the re-upload scan or the webhook, not a family album
    relinked: list[str] = field(default_factory=list)  # family albums the copy was added to
    relinked_member: list[str] = field(default_factory=list)  # member-owned albums, same
    trashed: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def counts_as_reupload(self) -> bool:
        """A phone's re-upload of a photo the family already had, now trashed. A
        scan item that finished an interrupted move is not one."""
        return self.reupload and self.duplicate and self.trashed

    def __str__(self) -> str:
        if self.restored:
            how = "family copy restored from the trash"
        elif self.duplicate:
            how = ("re-upload" if self.reupload else "duplicate") + ", family already has it"
        else:
            how = "uploaded" if self.uploaded else "family already had it"
        fate = "trashed" if self.trashed else "kept"
        s = (
            f"{self.file_name}: {how} as {self.family_asset_id}; {self.edits} edits; "
            f"in {len(self.relinked)} family albums; original {fate}"
        )
        if self.relinked_member:
            s += f"; re-linked into {', '.join(repr(a) for a in self.relinked_member)}"
        return s


async def pass_backlog(accounts: Accounts) -> dict[str, int] | None:
    """Waiting jobs on the queues the move depends on, or None without an admin key."""
    if accounts.admin is None:
        return None
    waiting = await queue_backlog(accounts.admin.client)
    return {q: waiting.get(q, 0) for q in WATCHED_QUEUES if waiting.get(q, 0)}


async def outside_partners(accounts: Accounts, owner: Account) -> set[UUID]:
    """Non-participants the owner partner-shares their library with."""
    return await list_partner_ids(owner.client) - set(accounts.by_user_id())


async def _fresh_sidecar(owner: Account, asset_id: UUID, tag: str, timeout: float) -> bytes:
    """Immich's own sidecar for the asset, written after the moved tag went on."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        file_id = await find_sidecar(owner.client, asset_id)
        if file_id is not None:
            xmp = await download_asset_file(owner.client, file_id)
            if tag in tags_in(xmp):
                return xmp
        if asyncio.get_running_loop().time() >= deadline:
            raise MoveTimedOut(f"sidecar not written within {timeout:.0f}s; Immich may be busy")
        await asyncio.sleep(0.5)


async def _add_refusal(client, album: AlbumSummary, asset_id: UUID) -> str | None:
    """Add the asset to the album. Why Immich refused, or None when it is in."""
    for r in await add_album_assets(client, album.id, [asset_id]):
        if why := bulk_failure(r):
            return why
    return None


async def _albums_holding(accounts: Accounts, asset_id: UUID) -> list[AlbumSummary]:
    """Every album with the asset in it that any participant can see. The
    owner's key alone misses an album they aren't on: a partner's private
    album, or a family album they haven't been added to yet."""
    found: dict[UUID, AlbumSummary] = {}
    for who in accounts.by_user_id().values():
        for album in await albums_containing(who.client, asset_id):
            found.setdefault(album.id, album)
    return list(found.values())


def _relink_keys(accounts: Accounts, album: AlbumSummary, asset_owner: Account) -> list[Account]:
    """The participants who can add to an album the family doesn't own, in the
    order to try them: the album's owner, the asset's owner, then anyone else,
    each only if they own it or are an editor on it."""
    participants = accounts.by_user_id()
    can_add = (AlbumUserRole.OWNER, AlbumUserRole.EDITOR)
    order = dict.fromkeys([album.owner_id, asset_owner.user_id, *participants])
    return [
        participants[uid]
        for uid in order
        if uid in participants and album.roles.get(uid) in can_add
    ]


async def _relink_member_album(
    accounts: Accounts, album: AlbumSummary, asset_owner: Account, family_id: UUID
) -> None:
    """Add the family copy to an album the family doesn't own, with the first
    key that Immich accepts.

    A key that Immich refuses is an answer, not a failure: it says to try the
    next key. So an error from outside is caught here and acted on. A fault in
    the service is no such answer, and is left to rise with its traceback."""
    failures = []
    for who in _relink_keys(accounts, album, asset_owner):
        try:
            why = await _add_refusal(who.client, album, family_id)
        except ImmichError as e:
            why = str(e)
        if why is None:
            log.info("re-linked into %r with %s's key", album.name, who.name)
            return
        failures.append(f"{who.name}: {why}")
    raise MoveError(
        f"could not re-link the copy into {album.name!r} ({'; '.join(failures) or 'no key can'});"
        " original left in place"
    )


async def _wait_for_dimensions(family: Account, asset_id: UUID, timeout: float) -> None:
    """Applying edits needs width and height, which metadata extraction fills in."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        copy = await get_asset(family.client, asset_id)
        if copy.width and copy.height:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise MoveError(
                f"family copy {asset_id} has no dimensions after {timeout:.0f}s; "
                "extraction hasn't run. Run the move again to finish."
            )
        await asyncio.sleep(0.5)


@dataclass
class MoveManyResult:
    done: list[MoveResult] = field(default_factory=list)
    blocked: list[tuple[str, str]] = field(default_factory=list)  # (file name, reason)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (file name, error)
    remaining: int = 0  # movable assets not tried because of the limit
    unfinished: set[UUID] = field(default_factory=set)  # blocked, failed or over the limit
    skipped_in_flight: int = 0  # another path was already on it

    def reuploads_trashed(self) -> dict[UUID, int]:
        """Trashed re-uploads per member, for the summary line and the daemon's tally."""
        out: dict[UUID, int] = {}
        for r in self.done:
            if r.counts_as_reupload:
                out[r.owner_id] = out.get(r.owner_id, 0) + 1
        return out

    def __str__(self) -> str:
        n = sum(self.reuploads_trashed().values())
        reuploads = f" ({n} re-uploads)" if n else ""
        return (
            f"moved {len(self.done)}{reuploads}, blocked {len(self.blocked)}, "
            f"failed {len(self.failed)}, remaining {self.remaining}"
        )


async def move_assets(
    accounts: Accounts,
    opts: MoveOptions,
    moves: list[MoveAsset],
    *,
    limit: int,
    in_flight: set[UUID] | None = None,
) -> MoveManyResult:
    """Move assets in scan order. Once `limit` of them have used a slot (0 for no
    limit), the rest that would use one are left for the next pass. A move uses
    a slot, and so does a failure or a timeout, because each may have uploaded
    bytes or kept the pass waiting. A block that the first reads settle is
    free: a photo that is blocked on every pass, a live photo say, must not
    hold a slot on every pass and starve the photos behind it.

    A re-upload never uses a slot, whatever becomes of it, and the limit never
    stops one. It has nothing to upload, and the limit is the brake on bytes.

    Each item is a MoveAsset from the scan. One asset blocking or failing doesn't
    stop the others, whatever the exception. Per-owner lookups happen once.

    `in_flight` is shared between the daemon's pass and its webhook receiver:
    an asset the other path is already on is skipped here, and ours are held in
    it while they're worked on."""
    out = MoveManyResult()
    shared_links: dict[UUID, set[UUID]] = {}
    slots_used = 0
    for m in moves:
        slot = 0 if m.reupload else 1
        if limit and slot and slots_used >= limit:
            out.remaining += 1
            out.unfinished.add(m.asset_id)
            continue
        owner = m.owner
        if in_flight is not None:
            if m.asset_id in in_flight:
                out.skipped_in_flight += 1
                out.unfinished.add(m.asset_id)
                continue
            in_flight.add(m.asset_id)
        try:
            if owner.user_id not in shared_links:
                shared_links[owner.user_id] = await shared_link_asset_ids(owner.client)
                for uid in await outside_partners(accounts, owner):
                    log.warning(
                        "%s partner-shares their library with non-participant %s", owner.name, uid
                    )
            result = await move_asset(
                accounts,
                opts,
                m.asset_id,
                owner,
                shared_links=shared_links[owner.user_id],
                reupload=m.reupload,
            )
        except MoveBlocked as e:
            log.warning("blocked %s: %s", m.file_name, e)
            out.blocked.append((m.file_name, str(e)))
            out.unfinished.add(m.asset_id)
            slots_used += slot * isinstance(e, MoveTimedOut)
            continue
        except Exception as e:  # noqa: BLE001 - the move stops before the trash; the rest go on
            out.failed.append((m.file_name, log_failure(log, f"{m.file_name} not moved", e)))
            out.unfinished.add(m.asset_id)
            slots_used += slot
            continue
        finally:
            if in_flight is not None:
                in_flight.discard(m.asset_id)
        for w in result.warnings:
            log.warning("%s: %s", result.file_name, w)
        log.info("%s", result)
        out.done.append(result)
        slots_used += slot
    if out.remaining:
        log.info("%d more assets to move; limit is %d per pass", out.remaining, limit)
    return out


async def move_asset(
    accounts: Accounts,
    opts: MoveOptions,
    asset_id: UUID,
    owner: Account,
    *,
    shared_links: set[UUID] | None = None,
    now: datetime | None = None,
    reupload: bool = False,
) -> MoveResult:
    """`reupload` says the asset came from the re-upload scan rather than a family
    album. It then needs a live family copy: a trashed one blocks instead of
    being restored, a missing one blocks instead of being uploaded, and a
    duplicate counts as a re-upload."""
    family = accounts.family
    now = now or datetime.now(UTC)

    # Read, and decide whether to touch it at all.
    asset = await get_asset(owner.client, asset_id)
    if asset.owner_id != owner.user_id:
        raise MoveError(f"{asset.original_file_name} is not owned by {owner.name}")
    if why := unmovable(asset):
        raise MoveBlocked(why)

    # The family copy, looked up first. With a copy, live or in the trash, the
    # moved tag on the original decides: without it this is a duplicate, and the
    # copy is authoritative. A duplicate needs nothing the asset's own jobs produce, so
    # it skips the extraction and settle checks, which is what lets the webhook
    # trash a re-upload seconds after it lands.
    have_tag = any(t.value == opts.moved_tag for t in asset.tags or [])

    async def copy_state() -> tuple[bool, bool]:
        """(duplicate, fixed up). The fix-ups write the edits first and the
        entry last, so an entry that names this asset says both are done. That
        is a move that stopped at the re-link or the trash, or a moved original
        its owner took back out of the trash: either way they are not repeated."""
        provenance = await get_asset_metadata_value(family.client, family_id, PROVENANCE_KEY)
        ours = bool(provenance) and provenance.get("origin_asset_id") == str(asset_id)
        foreign = bool(provenance) and not ours
        return foreign or not have_tag, ours

    owned = await existing_asset_by_checksum(family.client, asset.checksum)
    if reupload and (owned is None or owned.is_trashed):
        # Only a family album makes a photo the family's. Without a live copy
        # this is the member's own photo, whatever the scan saw a moment ago.
        raise MoveBlocked(
            "the family's copy is in the trash" if owned else "the family no longer has a copy"
        )
    family_id = owned.asset_id if owned else None
    duplicate = fixed_up = False
    if owned:
        duplicate, fixed_up = await copy_state()

    if not duplicate:
        if asset.exif_info is None:
            raise MoveBlocked("metadata extraction hasn't run yet")
        if now - asset.updated_at < timedelta(seconds=opts.settle_seconds):
            age = int((now - asset.updated_at).total_seconds())
            raise MoveBlocked(f"changed {age}s ago; settling")
    if shared_links is None:
        shared_links = await shared_link_asset_ids(owner.client)
    if asset_id in shared_links:
        raise MoveBlocked("in one of the owner's shared links")

    warnings: list[str] = []
    if asset.stack is not None:
        warnings.append("in a stack; the stack loses this member")

    albums = await _albums_holding(accounts, asset_id)
    family_albums = [a for a in albums if a.owner_id == family.user_id]
    other_albums = [a for a in albums if a.owner_id != family.user_id]
    for a in other_albums:
        if not _relink_keys(accounts, a, owner):
            # The photo's owner may not be on this album at all, so the reason
            # has to say whose it is, who can see it, and what clears the block.
            theirs = a.emails.get(a.owner_id, "someone unknown")
            seen_by = " and ".join(
                p.name for uid, p in accounts.by_user_id().items() if uid in a.user_ids
            )
            raise MoveBlocked(
                f"in {a.name!r}, owned by non-participant {theirs}, which {seen_by} can see "
                "but nobody with a key can add to; ask its owner to make a family member an "
                "editor there, or to take the photo out"
            )
    edits = [] if duplicate or fixed_up else await get_asset_edits(owner.client, asset_id)

    # A trashed original with no copy isn't moved; a trashed original with a copy
    # is a move that died late.
    uploaded = restored = False
    if owned and owned.is_trashed:
        # Shared again after the family deleted it: bring the copy back.
        await restore_assets(family.client, [owned.asset_id])
        restored = True
        log.info(
            "%s: restored the family copy %s from the trash", asset.original_file_name, family_id
        )
    if family_id is None:
        if asset.is_trashed:
            raise MoveError(f"{asset.original_file_name} is in the trash")
        # The sidecar, written by Immich after the moved tag goes on.
        # An original that already wears the tag gets no new sidecar write, so
        # an edit from the last few seconds can be missing from the one we read.
        # Accepted: see "Sidecar" in docs/immich-notes.md.
        if not have_tag:
            await tag_asset(owner.client, await upsert_tag(owner.client, opts.moved_tag), asset_id)
        xmp = await _fresh_sidecar(owner, asset_id, opts.moved_tag, opts.sidecar_timeout)
        sidecar = with_only_tag(xmp, f"{opts.from_prefix}/{owner.name}")
        # The directory is made here as well as at startup: a cleaner of /tmp
        # can take it away from under a daemon that has run for weeks.
        opts.temp_dir.mkdir(parents=True, exist_ok=True)
        original = opts.temp_dir / f"{TEMP_PREFIX}{asset_id}"
        try:
            await download_original(owner.client, asset_id, original, checksum=asset.checksum)
            family_id, uploaded = await upload_asset(
                family.client,
                path=original,
                filename=asset.original_file_name,
                checksum=asset.checksum,
                file_created_at=asset.file_created_at,
                file_modified_at=asset.file_modified_at,
                is_favorite=asset.is_favorite,
                visibility=asset.visibility,
                sidecar=sidecar,
                duration=asset.duration or None,  # milliseconds; None or 0 for a still image
            )
        finally:
            original.unlink(missing_ok=True)
        how = "uploaded" if uploaded else "duplicate of"
        log.info("%s: %s as %s", asset.original_file_name, how, family_id)
    else:
        log.info("%s: family already has it as %s", asset.original_file_name, family_id)

    # Fix-ups, edits before provenance: `copy_state` relies on that order. A
    # duplicate leaves the copy's edits and provenance alone: they describe the
    # move that already happened. So does a copy whose fix-ups are already done.
    applied = 0
    if not duplicate and not fixed_up:
        applied = len(edits)
        if edits:
            await _wait_for_dimensions(family, family_id, opts.extraction_timeout)
            await set_asset_edits(family.client, family_id, edits)
        await set_asset_metadata(
            family.client,
            family_id,
            PROVENANCE_KEY,
            {
                "origin_user_id": str(owner.user_id),
                "origin_asset_id": str(asset_id),
                "moved_at": now.isoformat(timespec="seconds"),
            },
        )
    relinked = []
    for a in family_albums:
        if a.name == opts.dropbox_album:
            continue
        if why := await _add_refusal(family.client, a, family_id):
            raise MoveError(f"could not add the copy to {a.name!r}: {why}")
        relinked.append(a.name)
    relinked_member = []
    for a in other_albums:
        await _relink_member_album(accounts, a, owner, family_id)
        relinked_member.append(a.name)

    # The original goes to the trash, once.
    current = await get_asset(owner.client, asset_id)
    trashed = False
    if not current.is_trashed:
        await trash_assets(owner.client, [asset_id])
        trashed = True

    return MoveResult(
        asset_id=asset_id,
        family_asset_id=family_id,
        file_name=asset.original_file_name,
        uploaded=uploaded,
        edits=applied,
        duplicate=duplicate,
        restored=restored,
        reupload=reupload,
        owner_id=owner.user_id,
        relinked=relinked,
        relinked_member=relinked_member,
        trashed=trashed,
        warnings=warnings,
    )
