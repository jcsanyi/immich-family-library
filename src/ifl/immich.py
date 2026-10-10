"""Thin helpers over immichpy for the handful of shapes the service cares about."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from uuid import UUID

from immichpy.client.generated.models.add_users_dto import AddUsersDto
from immichpy.client.generated.models.album_response_dto import AlbumResponseDto
from immichpy.client.generated.models.album_user_add_dto import AlbumUserAddDto
from immichpy.client.generated.models.album_user_role import AlbumUserRole
from immichpy.client.generated.models.asset_response_dto import AssetResponseDto
from immichpy.client.generated.models.bulk_id_response_dto import BulkIdResponseDto
from immichpy.client.generated.models.bulk_ids_dto import BulkIdsDto
from immichpy.client.generated.models.create_album_dto import CreateAlbumDto
from immichpy.client.generated.models.metadata_search_dto import MetadataSearchDto
from immichpy.client.main import AsyncClient


@dataclass(frozen=True)
class AlbumSummary:
    id: UUID
    name: str
    owner_id: UUID | None
    user_ids: frozenset[UUID]  # everyone on the album, owner included
    asset_count: int
    description: str = ""
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


# Writes. Each is one Immich call under the given account's key.


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
