"""Album conversion.

A member-owned album shared with the family user becomes a family-owned album
with the same name. The family account gets an album by that name (or already
has one), everyone who should be on it is added, each contributor adds their
own photos to it with their own key, and the original is deleted. No photo
moves here: afterwards they're member-owned assets in a family-owned album,
which is exactly what the service moves.

Every step is derived from what Immich has, so a run that died halfway is
finished by running it again: the family album is found by name, users and
assets already on it are skipped, and the original is only deleted once
Immich has accepted every photo.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID

from immichpy.client.generated.models.album_user_role import AlbumUserRole
from immichpy.client.main import AsyncClient

from ifl.accounts import Account, Accounts
from ifl.immich import (
    AlbumSummary,
    add_album_assets,
    add_album_users,
    bulk_failure,
    create_album,
    delete_album,
    iter_album_assets,
    list_albums,
)
from ifl.observer import ConvertAlbum

log = logging.getLogger(__name__)


class ConvertError(Exception):
    """The conversion stopped before deleting the original. Running it again resumes."""


@dataclass
class ConvertResult:
    family_album: AlbumSummary
    created: bool
    users_added: dict[UUID, AlbumUserRole]
    assets_added: int
    assets_already_there: int

    def __str__(self) -> str:
        how = "created" if self.created else "re-used"
        return (
            f"{how} family album {self.family_album.name!r} ({self.family_album.id}); "
            f"added {len(self.users_added)} users, {self.assets_added} assets "
            f"({self.assets_already_there} already there)"
        )


async def find_family_album(family: Account, name: str) -> AlbumSummary | None:
    matches = [
        a
        for a in await list_albums(family.client)
        if a.owner_id == family.user_id and a.name == name
    ]
    if len(matches) > 1:
        raise ConvertError(
            f"the family account has {len(matches)} albums named {name!r}; "
            "merge or rename them before converting into one"
        )
    return matches[0] if matches else None


def wanted_roles(accounts: Accounts, original: AlbumSummary) -> dict[UUID, AlbumUserRole]:
    """Who belongs on the family album: every configured member as editor, plus
    anyone else the original was shared with, at their original role."""
    roles: dict[UUID, AlbumUserRole] = {}
    for uid, role in original.roles.items():
        if uid in (original.owner_id, accounts.family.user_id):
            continue
        roles[uid] = role
    for m in accounts.members.values():
        roles[m.user_id] = AlbumUserRole.EDITOR
    return roles


async def _assets_by_owner(client: AsyncClient, album_id: UUID) -> dict[UUID, list[UUID]]:
    out: dict[UUID, list[UUID]] = defaultdict(list)
    async for a in iter_album_assets(client, album_id):
        out[a.owner_id].append(a.id)
    return dict(out)


async def _add_assets(
    accounts: Accounts, album: AlbumSummary, by_owner: dict[UUID, list[UUID]]
) -> tuple[int, int]:
    """Each owner adds their own assets. Returns (added, already there)."""
    participants = accounts.by_user_id()
    added = dupes = 0
    for owner_id, ids in by_owner.items():
        owner = participants.get(owner_id)
        if owner is None:
            raise ConvertError(f"asset owner {owner_id} is not a participant")
        results = await add_album_assets(owner.client, album.id, ids)
        failed = [why for r in results if (why := bulk_failure(r))]
        if failed:
            raise ConvertError(
                f"{owner.name} could not add {len(failed)} of {len(ids)} assets to "
                f"{album.name!r} ({failed[0]})"
            )
        added += sum(1 for r in results if r.success)
        dupes += sum(1 for r in results if not r.success)
    return added, dupes


@dataclass
class ConvertManyResult:
    done: list[ConvertResult]
    failed: list[tuple[ConvertAlbum, ConvertError]]
    remaining: int  # convertible albums left untouched because of the limit


async def convert_albums(
    accounts: Accounts, conversions: list[ConvertAlbum], *, limit: int
) -> ConvertManyResult:
    """Convert albums in scan order, at most `limit` of them (0 for no limit).

    One album failing doesn't stop the others, whatever the exception; it's
    reported and will be found again by the next scan.
    """
    todo = conversions if limit == 0 else conversions[:limit]
    out = ConvertManyResult(done=[], failed=[], remaining=len(conversions) - len(todo))
    for conv in todo:
        try:
            out.done.append(await convert_album(accounts, conv))
        except ConvertError as e:
            log.error("album %r not converted: %s", conv.album.name, e)
            out.failed.append((conv, e))
        except Exception as e:  # noqa: BLE001 - the original is only deleted last; the rest go on
            log.exception("album %r not converted", conv.album.name)
            out.failed.append((conv, ConvertError(f"{type(e).__name__}: {e}")))
    if out.remaining:
        log.info("%d more albums to convert; limit is %d per pass", out.remaining, limit)
    return out


async def convert_album(accounts: Accounts, conv: ConvertAlbum) -> ConvertResult:
    """Run the conversion for an album the scan marked convertible."""
    original, owner, family = conv.album, conv.owner, accounts.family
    log.info("converting %s's album %r (%s)", owner.name, original.name, original.id)

    # The family album with the same name, found or created.
    fam_album = await find_family_album(family, original.name)
    created = fam_album is None
    if fam_album is None:
        fam_album = await create_album(family.client, original.name, original.description)
        log.info("created family album %r (%s)", fam_album.name, fam_album.id)
    else:
        log.info("re-using family album %r (%s)", fam_album.name, fam_album.id)

    # Everyone who should be on it and isn't yet.
    missing = {
        u: r for u, r in wanted_roles(accounts, original).items() if u not in fam_album.user_ids
    }
    await add_album_users(family.client, fam_album.id, missing)
    if missing:
        log.info("shared %r with %d more users", fam_album.name, len(missing))

    # Each contributor adds their own photos with their own key. Immich answers
    # for every id, and any refusal raises before the delete. A photo that
    # joins the original after this read is not carried: see "Album conversion"
    # in docs/how-it-works.md.
    added, dupes = await _add_assets(
        accounts, fam_album, await _assets_by_owner(owner.client, original.id)
    )
    await delete_album(owner.client, original.id)
    log.info("deleted %s's album %r", owner.name, original.name)

    return ConvertResult(
        family_album=fam_album,
        created=created,
        users_added=missing,
        assets_added=added,
        assets_already_there=dupes,
    )
