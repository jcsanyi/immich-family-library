"""Thin helpers over immichpy for the handful of shapes the service cares about."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from immichpy.client.generated.models.add_users_dto import AddUsersDto
from immichpy.client.generated.models.album_response_dto import AlbumResponseDto
from immichpy.client.generated.models.album_user_add_dto import AlbumUserAddDto
from immichpy.client.generated.models.album_user_role import AlbumUserRole
from immichpy.client.generated.models.asset_bulk_delete_dto import AssetBulkDeleteDto
from immichpy.client.generated.models.asset_bulk_upload_check_dto import (
    AssetBulkUploadCheckDto,
)
from immichpy.client.generated.models.asset_bulk_upload_check_item import (
    AssetBulkUploadCheckItem,
)
from immichpy.client.generated.models.asset_edit_action_item_dto import AssetEditActionItemDto
from immichpy.client.generated.models.asset_edits_create_dto import AssetEditsCreateDto
from immichpy.client.generated.models.asset_file_type import AssetFileType
from immichpy.client.generated.models.asset_metadata_upsert_dto import AssetMetadataUpsertDto
from immichpy.client.generated.models.asset_metadata_upsert_item_dto import (
    AssetMetadataUpsertItemDto,
)
from immichpy.client.generated.models.asset_response_dto import AssetResponseDto
from immichpy.client.generated.models.asset_visibility import AssetVisibility
from immichpy.client.generated.models.bulk_id_error_reason import BulkIdErrorReason
from immichpy.client.generated.models.bulk_id_response_dto import BulkIdResponseDto
from immichpy.client.generated.models.bulk_ids_dto import BulkIdsDto
from immichpy.client.generated.models.create_album_dto import CreateAlbumDto
from immichpy.client.generated.models.metadata_search_dto import MetadataSearchDto
from immichpy.client.generated.models.partner_create_dto import PartnerCreateDto
from immichpy.client.generated.models.partner_direction import PartnerDirection
from immichpy.client.generated.models.shared_link_type import SharedLinkType
from immichpy.client.generated.models.tag_upsert_dto import TagUpsertDto
from immichpy.client.generated.models.update_album_user_dto import UpdateAlbumUserDto
from immichpy.client.main import AsyncClient


@dataclass(frozen=True)
class AlbumSummary:
    id: UUID
    name: str
    owner_id: UUID | None
    user_ids: frozenset[UUID]  # everyone on the album, owner included
    asset_count: int
    description: str = ""
    has_shared_link: bool = False
    emails: dict[UUID, str] = field(default_factory=dict, compare=False, hash=False)
    roles: dict[UUID, AlbumUserRole] = field(default_factory=dict, compare=False, hash=False)


def summarize_album(album: AlbumResponseDto) -> AlbumSummary:
    owner_id = None
    emails: dict[UUID, str] = {}
    roles: dict[UUID, AlbumUserRole] = {}
    for au in album.album_users or []:
        emails[au.user.id] = au.user.email
        roles[au.user.id] = au.role
        if au.role == AlbumUserRole.OWNER:
            owner_id = au.user.id
    return AlbumSummary(
        id=album.id,
        name=album.album_name,
        owner_id=owner_id,
        user_ids=frozenset(emails),
        asset_count=album.asset_count,
        description=album.description or "",
        has_shared_link=bool(album.has_shared_link),
        emails=emails,
        roles=roles,
    )


class UserNames:
    """Turns user ids into something readable for messages: email, else the id.

    Participants come from config. Others come from the album's user list when
    they're on it, else one cached lookup per id.
    """

    def __init__(self, client: AsyncClient, known: dict[UUID, str]):
        self.client = client
        self.cache = dict(known)

    async def describe(self, user_id: UUID, album: AlbumSummary | None = None) -> str:
        if user_id in self.cache:
            return self.cache[user_id]
        if album and user_id in album.emails:
            self.cache[user_id] = album.emails[user_id]
            return self.cache[user_id]
        try:
            user = await self.client.users.get_user(id=user_id)
            self.cache[user_id] = user.email
        except Exception:
            self.cache[user_id] = str(user_id)
        return self.cache[user_id]


async def list_albums(client: AsyncClient) -> list[AlbumSummary]:
    """All albums visible to this account: owned and shared-with."""
    albums = await client.albums.get_all_albums()
    return [summarize_album(a) for a in albums]


async def iter_album_assets(
    client: AsyncClient, album_id: UUID, *, page_size: int = 250
) -> AsyncIterator[AssetResponseDto]:
    """Every asset in an album, with owner and checksum, via metadata search."""
    page = 1
    while True:
        res = await client.search.search_assets(
            MetadataSearchDto(album_ids=[album_id], page=page, size=page_size, with_exif=False)
        )
        for a in res.assets.items:
            yield a
        if not res.assets.next_page:
            return
        page = int(res.assets.next_page)


async def list_partner_ids(client: AsyncClient) -> set[UUID]:
    """Users this account partner-shares its library with."""
    partners = await client.partners.get_partners(direction=PartnerDirection.SHARED_MINUS_BY)
    return {p.id for p in partners}


# Writes. Each is one Immich call under the given account's key.


async def create_partner(client: AsyncClient, user_id: UUID) -> None:
    """Share this account's library with a user. "Show in timeline" is theirs to set."""
    await client.partners.create_partner(PartnerCreateDto(shared_with_id=user_id))


async def create_album(client: AsyncClient, name: str, description: str = "") -> AlbumSummary:
    dto = CreateAlbumDto(album_name=name, description=description or None)
    return summarize_album(await client.albums.create_album(dto))


async def add_album_users(
    client: AsyncClient, album_id: UUID, roles: dict[UUID, AlbumUserRole]
) -> None:
    """Share with users not yet on the album. Immich rejects users already on it,
    so callers diff against the current user list first."""
    if not roles:
        return
    dto = AddUsersDto(album_users=[AlbumUserAddDto(user_id=u, role=r) for u, r in roles.items()])
    await client.albums.add_users_to_album(id=album_id, add_users_dto=dto)


async def add_album_assets(
    client: AsyncClient, album_id: UUID, asset_ids: Iterable[UUID]
) -> list[BulkIdResponseDto]:
    """One result per id. An asset already in the album comes back as a
    'duplicate' failure, which callers treat as success."""
    ids = list(asset_ids)
    if not ids:
        return []
    return await client.albums.add_assets_to_album(id=album_id, bulk_ids_dto=BulkIdsDto(ids=ids))


async def delete_album(client: AsyncClient, album_id: UUID) -> None:
    """Deletes the album only. Its assets stay where they are."""
    await client.albums.delete_album(id=album_id)


async def set_album_user_role(
    client: AsyncClient, album_id: UUID, user_id: UUID, role: AlbumUserRole
) -> None:
    await client.albums.update_album_user(
        id=album_id, user_id=str(user_id), update_album_user_dto=UpdateAlbumUserDto(role=role)
    )


# Per-asset reads for the move. Edits, asset files and the sidecar are owner-only.


async def get_asset(client: AsyncClient, asset_id: UUID) -> AssetResponseDto:
    return await client.assets.get_asset_info(id=asset_id)


async def albums_containing(client: AsyncClient, asset_id: UUID) -> list[AlbumSummary]:
    """Albums this account can see that hold the asset."""
    albums = await client.albums.get_all_albums(asset_id=asset_id)
    return [summarize_album(a) for a in albums]


async def get_asset_edits(client: AsyncClient, asset_id: UUID) -> list[AssetEditActionItemDto]:
    """Crop, rotate and mirror, in the shape the edits endpoint accepts back."""
    res = await client.assets.get_asset_edits(id=asset_id)
    return [AssetEditActionItemDto(action=e.action, parameters=e.parameters) for e in res.edits]


async def find_sidecar(client: AsyncClient, asset_id: UUID) -> UUID | None:
    files = await client.asset_files.search_asset_files(
        asset_id=asset_id, type=AssetFileType.SIDECAR
    )
    return files[0].id if files else None


async def download_asset_file(client: AsyncClient, file_id: UUID) -> bytes:
    return await client.asset_files.download_asset_file(id=file_id)


async def download_original(client: AsyncClient, asset_id: UUID) -> bytes:
    return await client.assets.download_asset(id=asset_id)


async def shared_link_asset_ids(client: AsyncClient) -> set[UUID]:
    """Assets in this account's individual shared links. Album links are checked by album."""
    links = await client.shared_links.get_all_shared_links()
    return {a.id for link in links if link.type == SharedLinkType.INDIVIDUAL for a in link.assets}


async def existing_asset_by_checksum(client: AsyncClient, checksum: str) -> Owned | None:
    """This account's asset with that checksum, if it already has one, trashed or not."""
    return (await owned_checksums(client, [checksum])).get(checksum)


async def get_asset_metadata_value(
    client: AsyncClient, asset_id: UUID, key: str
) -> dict[str, Any] | None:
    for item in await client.assets.get_asset_metadata(id=asset_id):
        if item.key == key:
            return item.value
    return None


async def restore_assets(client: AsyncClient, asset_ids: Iterable[UUID]) -> None:
    await client.trash.restore_assets(BulkIdsDto(ids=list(asset_ids)))


async def queue_backlog(client: AsyncClient) -> dict[str, int]:
    """Waiting jobs per queue. Admin only."""
    return {q.name.value: q.statistics.waiting for q in await client.queues.get_queues()}


# Per-asset writes for the move.


async def upsert_tag(client: AsyncClient, name: str) -> UUID:
    (tag,) = await client.tags.upsert_tags(TagUpsertDto(tags=[name]))
    return tag.id


async def tag_asset(client: AsyncClient, tag_id: UUID, asset_id: UUID) -> None:
    results = await client.tags.tag_assets(id=tag_id, bulk_ids_dto=BulkIdsDto(ids=[asset_id]))
    for r in results:
        if not r.success and r.error != BulkIdErrorReason.DUPLICATE:
            raise RuntimeError(f"tagging failed: {r.error} {r.error_message or ''}".strip())


async def upload_asset(
    client: AsyncClient,
    *,
    data: bytes,
    filename: str,
    checksum: str,
    file_created_at: datetime,
    file_modified_at: datetime,
    is_favorite: bool,
    visibility: AssetVisibility,
    sidecar: bytes | None,
    duration: str | None = None,
) -> tuple[UUID, bool]:
    """Upload bytes plus sidecar. Returns (asset id, created). A duplicate of
    something this account already has returns that asset's id and False."""
    # No `filename` form field: the server applies it to every part, so the
    # sidecar would be checked as a .jpg and rejected. Each part names itself.
    res = await client.assets.upload_asset(
        asset_data=(filename, data),
        x_immich_checksum=checksum,
        file_created_at=file_created_at,
        file_modified_at=file_modified_at,
        is_favorite=is_favorite,
        visibility=visibility,
        sidecar_data=(filename + ".xmp", sidecar) if sidecar else None,
        duration=duration,
    )
    return res.id, res.status.value == "created"


async def set_asset_edits(
    client: AsyncClient, asset_id: UUID, edits: list[AssetEditActionItemDto]
) -> None:
    """Replaces the asset's edit list. Needs the asset's dimensions, so only
    after metadata extraction has run on it."""
    dto = AssetEditsCreateDto(edits=edits)
    await client.assets.edit_asset(id=asset_id, asset_edits_create_dto=dto)


async def set_asset_metadata(
    client: AsyncClient, asset_id: UUID, key: str, value: dict[str, Any]
) -> None:
    dto = AssetMetadataUpsertDto(items=[AssetMetadataUpsertItemDto(key=key, value=value)])
    await client.assets.update_asset_metadata(id=asset_id, asset_metadata_upsert_dto=dto)


async def trash_assets(client: AsyncClient, asset_ids: Iterable[UUID]) -> None:
    """Trash, never permanent delete. See the re-upload section of DESIGN.md."""
    await client.assets.delete_assets(AssetBulkDeleteDto(ids=list(asset_ids), force=False))


async def iter_assets_created_after(
    client: AsyncClient, since: datetime | None, *, page_size: int = 250
) -> AsyncIterator[AssetResponseDto]:
    """This account's view of assets uploaded after `since` (all of them when None).
    Includes partners' assets; callers filter by owner."""
    page = 1
    while True:
        res = await client.search.search_assets(
            MetadataSearchDto(created_after=since, page=page, size=page_size, with_exif=False)
        )
        for a in res.assets.items:
            yield a
        if not res.assets.next_page:
            return
        page = int(res.assets.next_page)


@dataclass(frozen=True)
class Owned:
    asset_id: UUID
    is_trashed: bool


async def owned_checksums(
    client: AsyncClient, checksums: Iterable[str], *, batch: int = 250
) -> dict[str, Owned]:
    """Which of these checksums this account already has, and whether that asset is trashed."""
    out: dict[str, Owned] = {}
    todo = list(dict.fromkeys(checksums))
    for i in range(0, len(todo), batch):
        chunk = todo[i : i + batch]
        dto = AssetBulkUploadCheckDto(
            assets=[AssetBulkUploadCheckItem(id=c, checksum=c) for c in chunk]
        )
        res = await client.assets.check_bulk_upload(dto)
        for r in res.results:
            if r.action.value == "reject" and r.reason and r.reason.value == "duplicate":
                out[r.id] = Owned(r.asset_id, bool(r.is_trashed))
    return out
