"""Thin helpers over immichpy for the handful of shapes the service cares about."""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import inspect
import json
import logging
import os
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import aiohttp
from immichpy.client.generated.exceptions import ApiException
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
from immichpy.client.generated.models.workflow_create_dto import WorkflowCreateDto
from immichpy.client.generated.models.workflow_response_dto import WorkflowResponseDto
from immichpy.client.generated.models.workflow_step_dto import WorkflowStepDto
from immichpy.client.generated.models.workflow_trigger import WorkflowTrigger
from immichpy.client.generated.models.workflow_update_dto import WorkflowUpdateDto
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
        except ImmichError as e:
            if not e.refused:
                raise
            # Immich will not show this user to the family. The name is for a
            # message only, so the id will do.
            self.cache[user_id] = str(user_id)
        return self.cache[user_id]


async def list_albums(client: AsyncClient) -> list[AlbumSummary]:
    """All albums visible to this account: owned and shared-with."""
    albums = await client.albums.get_all_albums()
    return [summarize_album(a) for a in albums]


async def _search(
    client: AsyncClient, *, page_size: int, **criteria: Any
) -> AsyncIterator[AssetResponseDto]:
    """Every asset a metadata search matches, one page at a time."""
    page = 1
    while True:
        res = await client.search.search_assets(
            MetadataSearchDto(**criteria, page=page, size=page_size, with_exif=False)
        )
        for a in res.assets.items:
            yield a
        if not res.assets.next_page:
            return
        page = int(res.assets.next_page)


def iter_album_assets(
    client: AsyncClient, album_id: UUID, *, page_size: int = 250
) -> AsyncIterator[AssetResponseDto]:
    """Every asset in an album, with owner and checksum, via metadata search."""
    return _search(client, page_size=page_size, album_ids=[album_id])


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
    'duplicate' failure, which callers treat as success: see `bulk_failure`."""
    ids = list(asset_ids)
    if not ids:
        return []
    return await client.albums.add_assets_to_album(id=album_id, bulk_ids_dto=BulkIdsDto(ids=ids))


async def remove_album_assets(
    client: AsyncClient, album_id: UUID, asset_ids: Iterable[UUID]
) -> list[BulkIdResponseDto]:
    """One result per id. The assets leave the album and are otherwise untouched.
    An asset that was not in the album comes back as a 'not_found' failure."""
    ids = list(asset_ids)
    if not ids:
        return []
    return await client.albums.remove_asset_from_album(
        id=album_id, bulk_ids_dto=BulkIdsDto(ids=ids)
    )


class Failure(Exception):
    """Something the service found wrong and can say in its own words: a refusal
    from Immich that it checked for, or a condition it tested. The text is the
    whole report, so it needs no traceback. The move and the conversion have
    their own kinds."""


class ImmichError(Failure):
    """A call to Immich did not work: Immich answered with an error, sent a
    reply the client could not read, or could not be reached. The error that
    the client raised is the cause.

    `call` is the client method, `status` is Immich's answer when there was
    one, and `place` is the line of the service that wanted the call. `reason`
    is what went wrong without the call and the place, for a message that
    already says them."""

    def __init__(self, call: str, cause: Exception):
        self.call = call
        self.status = None
        self.place = _calling_place()
        if isinstance(cause, ApiException):
            self.status = cause.status
            # The client's own text runs to four lines and carries every
            # response header. The status and Immich's message are the news.
            try:
                message = json.loads(cause.body or "").get("message")
            except (ValueError, AttributeError, TypeError):
                message = None
            if isinstance(message, list):
                message = "; ".join(str(m) for m in message)
            answered = f"Immich answered {cause.status or 'with no status'}"
            detail = _one_short_line(str(message or cause.reason or "no detail"))
            self.reason, text = f"{answered}: {detail}", f"{answered} to {call}: {detail}"
        else:
            # A reply that fails validation lists every field. The start says
            # which model, and the rest is in the debug traceback.
            self.reason = f"no usable answer from Immich: {_one_short_line(describe_error(cause))}"
            text = f"{call} got {self.reason}"
        super().__init__(f"{text} ({self.place})" if self.place else text)

    @property
    def refused(self) -> bool:
        """Immich understood the request and said no. These are the three answers
        it gives to a key: 400 for no access to the thing asked for, 401 for a
        key it does not know, 403 for a permission the key lacks. An outage, a
        server error and a reply that could not be read are not refusals, and
        nor is another 4xx, which a proxy in front of Immich is as likely to
        have sent: 404 with no backend, 408 or 429 when it is busy."""
        return self.status in (400, 401, 403)


def _one_short_line(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + " ..."


_THIS_FILE = __file__
_PACKAGE = os.path.dirname(__file__)


def _calling_place() -> str | None:
    """The nearest line of the service outside this module, as the stack stands
    now. One client method serves several parts of the service, so the method
    alone does not say which part was at work. The helpers here are skipped:
    each is one call, and the line that wanted it is the news."""
    frame = inspect.currentframe()
    while frame is not None:
        file = frame.f_code.co_filename
        if file != _THIS_FILE and os.path.dirname(file) == _PACKAGE:
            return f"{os.path.basename(file)}:{frame.f_lineno} in {frame.f_code.co_name}"
        frame = frame.f_back
    return None


@contextmanager
def immich_call(name: str) -> Iterator[None]:
    """Whatever the block raises comes out as an `ImmichError` that names the
    call. What counts as Immich's doing is decided here, by where the error
    came from, and nowhere by its type: the same types are raised by the
    service's own mistakes.

    Two things raised in the block are the service's own. A `Failure` is its
    own finding. And an argument that the client refused never reached Immich."""
    try:
        yield
    except Failure:
        raise
    except Exception as e:
        if _refused_argument(e, name):
            raise
        raise ImmichError(name, e) from e


def _refused_argument(e: Exception, call: str) -> bool:
    """The client checks the arguments of a method before it sends anything, and
    reports a bad one as a validation error titled with the method. A reply it
    cannot read is the same type of error, titled with the model of the reply."""
    title = getattr(e, "title", None)
    return isinstance(title, str) and title.rsplit(".", 1)[-1] == call.rsplit(".", 1)[-1]


_PLAIN_VALUES = (str, bytes, int, float, bool, type(None), dict, list, tuple, set)


class _Guarded:
    """The client, or one of its API objects, with every method called inside
    `immich_call`. Methods are found as they are used, so a new client version
    needs nothing here. It goes two levels deep and no further, which is where
    the methods are: `client.albums.get_all_albums`. Anything else that an API
    object holds is returned as it is."""

    def __init__(self, target: Any, path: str = ""):
        self._guard_target = target
        self._guard_path = path

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_guard_"):
            raise AttributeError(name)  # asked for before __init__ ran, as a copy does
        attr = getattr(self._guard_target, name)
        path = f"{self._guard_path}.{name}" if self._guard_path else name
        if callable(attr):
            return _guarded_call(attr, path)
        if self._guard_path or isinstance(attr, _PLAIN_VALUES):
            return attr
        return _Guarded(attr, path)


def _guarded_call(fn: Callable[..., Any], path: str) -> Callable[..., Any]:
    @functools.wraps(fn)
    def call(*args: Any, **kwargs: Any) -> Any:
        with immich_call(path):
            result = fn(*args, **kwargs)
        if not inspect.isawaitable(result):
            return result

        async def answer() -> Any:
            with immich_call(path):
                return await result

        return answer()

    return call


def guard(client: AsyncClient) -> AsyncClient:
    """The client as the service uses it: see `immich_call`. A client that is
    guarded already is returned as it is."""
    return client if isinstance(client, _Guarded) else _Guarded(client)  # type: ignore[return-value]


def unguarded(client: AsyncClient) -> AsyncClient:
    """The client underneath, for the two transfers. They use parts of the
    client that it does not check, so they place `immich_call` themselves."""
    return client._guard_target if isinstance(client, _Guarded) else client


def describe_error(e: BaseException) -> str:
    """Any error as one line, for a log line or a result. A `Failure` is its
    own text. Anything else is its type and its text."""
    text = " ".join(str(e).split())
    if isinstance(e, Failure):
        return text
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def log_failure(log: logging.Logger, what: str, e: Exception, *, level: int = logging.ERROR) -> str:
    """Log one piece of work that failed, and return the reason as one line.

    A `Failure` is one line at `level`: the reason is all there is to know, and
    the traceback goes to debug. Anything else is a fault in the service, and
    is an error with its traceback whatever the level."""
    why = describe_error(e)
    if isinstance(e, Failure):
        log.log(level, "%s: %s", what, why)
        log.debug("the traceback of %s", what, exc_info=e)
    else:
        log.error("%s: %s", what, why, exc_info=e)
    return why


def bulk_failure(
    r: BulkIdResponseDto, *, done: BulkIdErrorReason = BulkIdErrorReason.DUPLICATE
) -> str | None:
    """Why one entry of a bulk response failed, or None when it did not. `done`
    is the error that says the work was already done, and is not a failure: an
    add of an id that was already there is a 'duplicate'."""
    if r.success or r.error == done:
        return None
    return f"{r.error.value if r.error else 'unknown'}: {r.error_message or 'no detail'}"


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


async def get_asset_if_readable(client: AsyncClient, asset_id: UUID) -> AssetResponseDto | None:
    """The asset, or None when this account cannot read it. Immich gives one
    answer, a 400, for an asset that does not exist and for one the key has no
    access to. Any other failure is raised."""
    try:
        return await get_asset(client, asset_id)
    except ImmichError as e:
        if e.status != 400:
            raise
        return None


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


# An original can be a video of several gigabytes, so the two calls that carry
# one never hold it in memory and never run under the client's five-minute
# total timeout. The generated `download_asset` and `upload_asset` take and
# return bytes only. The request builders beneath them don't care, so these two
# helpers call the builders and hand aiohttp a stream.
CHUNK = 1 << 20
DOWNLOAD, UPLOAD = "the download of an original", "the upload of an asset"
# A download is cut off when the server sends nothing for this long.
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)
# aiohttp's read timeout would fire while a long upload is still being sent, so
# an upload gets a total instead: five minutes, plus its size at this rate.
SLOWEST_UPLOAD = 125_000  # bytes a second


class DownloadMismatch(Failure):
    """The bytes that arrived are not the asset Immich says it has."""


async def download_original(
    client: AsyncClient, asset_id: UUID, dest: Path, *, checksum: str
) -> None:
    """Write the asset's original file to `dest`, and make sure it arrived whole:
    the upload that follows is what the original gets trashed for."""
    api = unguarded(client).assets
    # Built outside `immich_call`: an argument that the builder refuses is the
    # service's own mistake, and the client does not check these itself.
    request = api._download_asset_serialize(
        id=asset_id,
        edited=None,
        key=None,
        slug=None,
        _request_auth=None,
        _content_type=None,
        _headers=None,
        _host_index=0,
    )
    with immich_call(DOWNLOAD):
        res = await api.api_client.call_api(*request, _request_timeout=DOWNLOAD_TIMEOUT)
    try:
        if res.status != 200:
            with immich_call(DOWNLOAD):
                await res.read()
                api.api_client.response_deserialize(res, {"200": "bytes"})  # raises for the status
        digest = hashlib.sha1(usedforsecurity=False)

        def keep(chunk: bytes) -> None:
            f.write(chunk)
            digest.update(chunk)

        with dest.open("wb") as f:
            # Reading the body is Immich's part. Writing the file is the
            # service's own, so it stays outside.
            chunks = res.response.content.iter_chunked(CHUNK)
            while True:
                with immich_call(DOWNLOAD):
                    chunk = await anext(chunks, None)
                if chunk is None:
                    break
                await asyncio.to_thread(keep, chunk)
    finally:
        res.response.release()
    # Immich reports a checksum as the base64 of the SHA-1.
    got = base64.b64encode(digest.digest()).decode()
    if got != checksum:
        raise DownloadMismatch(
            f"downloaded {dest.stat().st_size} bytes of {asset_id} with checksum {got}, "
            f"expected {checksum}"
        )


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
        if why := bulk_failure(r):
            raise Failure(f"tagging failed: {why}")


async def upload_asset(
    client: AsyncClient,
    *,
    path: Path,
    filename: str,
    checksum: str,
    file_created_at: datetime,
    file_modified_at: datetime,
    is_favorite: bool,
    visibility: AssetVisibility,
    sidecar: bytes | None,
    duration: int | None = None,  # milliseconds, videos only
) -> tuple[UUID, bool]:
    """Upload the file at `path` plus sidecar. Returns (asset id, created). A
    duplicate of something this account already has returns that asset's id and
    False."""
    api = unguarded(client).assets
    timeout = aiohttp.ClientTimeout(total=300 + path.stat().st_size / SLOWEST_UPLOAD)
    with path.open("rb") as f:
        # No `filename` form field: the server applies it to every part, so the
        # sidecar would be checked as a .jpg and rejected. Each part names itself.
        request = api._upload_asset_serialize(
            asset_data=(filename, f),
            file_created_at=file_created_at,
            file_modified_at=file_modified_at,
            key=None,
            slug=None,
            x_immich_checksum=checksum,
            duration=duration,
            filename=None,
            is_favorite=is_favorite,
            live_photo_video_id=None,
            metadata=None,
            sidecar_data=(filename + ".xmp", sidecar) if sidecar else None,
            visibility=visibility,
            _request_auth=None,
            _content_type=None,
            _headers=None,
            _host_index=0,
        )
        with immich_call(UPLOAD):
            res = await api.api_client.call_api(*request, _request_timeout=timeout)
            await res.read()
    types = {"200": "AssetMediaResponseDto", "201": "AssetMediaResponseDto"}
    with immich_call(UPLOAD):
        made = api.api_client.response_deserialize(res, types).data
    return made.id, made.status.value == "created"


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
    """Trash, never permanent delete. See "Re-uploads" in docs/how-it-works.md."""
    await client.assets.delete_assets(AssetBulkDeleteDto(ids=list(asset_ids), force=False))


def iter_assets_created_after(
    client: AsyncClient, since: datetime | None, *, page_size: int = 250
) -> AsyncIterator[AssetResponseDto]:
    """This account's view of assets uploaded after `since` (all of them when None).
    Includes partners' assets; callers filter by owner."""
    return _search(client, page_size=page_size, created_after=since)


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


# Workflows: per user, firing on that user's own uploads.

WEBHOOK_METHOD = "immich-plugin-core#webhook"
TOKEN_HEADER = "X-IFL-Token"
PROVENANCE_KEY = "ifl"  # the metadata entry on a family copy that says where it came from


def webhook_step(url: str, token: str) -> WorkflowStepDto:
    return WorkflowStepDto(
        method=WEBHOOK_METHOD,
        enabled=True,
        config={"url": url, "method": "POST", "headerName": TOKEN_HEADER, "headerValue": token},
    )


def step_matches(step: WorkflowStepDto, url: str, token: str) -> bool:
    c = step.config or {}
    return (
        step.method == WEBHOOK_METHOD
        and step.enabled is not False
        and c.get("url") == url
        and c.get("headerName") == TOKEN_HEADER
        and c.get("headerValue") == token
    )


async def find_workflow(client: AsyncClient, name: str) -> WorkflowResponseDto | None:
    for w in await client.workflows.search_workflows(name=name):
        if w.name == name:
            return w
    return None


async def create_workflow(client: AsyncClient, name: str, url: str, token: str) -> UUID:
    dto = WorkflowCreateDto(
        name=name,
        description="immich-family-library: tells the service about each new upload",
        trigger=WorkflowTrigger.ASSETCREATE,
        enabled=True,
        steps=[webhook_step(url, token)],
    )
    return (await client.workflows.create_workflow(dto)).id


async def update_workflow(client: AsyncClient, workflow_id: UUID, url: str, token: str) -> None:
    dto = WorkflowUpdateDto(
        trigger=WorkflowTrigger.ASSETCREATE, enabled=True, steps=[webhook_step(url, token)]
    )
    await client.workflows.update_workflow(id=workflow_id, workflow_update_dto=dto)
