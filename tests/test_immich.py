"""The adapter layer over immichpy, against stub clients that return real models.

The fake in fakes.py replaces these helpers wholesale, so nothing else checks
that each one reads the right fields and sends the right arguments. These tests
do, for every helper, pass-throughs included.

Each stub method wears the signature of the installed client's real method, and
pydantic checks every call against it. So a Dependabot bump of immichpy that
renames a parameter, changes its type, or drops a method turns these tests red,
instead of failing on the server.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from fakes import (
    ALICE,
    BOB,
    EDITOR,
    FAMILY,
    OWNER,
    T0,
    VIEWER,
    FakeAsset,
    make_tag,
    make_user,
    make_workflow,
)
from immichpy.client.generated.exceptions import ApiException
from immichpy.client.generated.models.album_response_dto import AlbumResponseDto
from immichpy.client.generated.models.album_user_response_dto import AlbumUserResponseDto
from immichpy.client.generated.models.asset_bulk_upload_check_response_dto import (
    AssetBulkUploadCheckResponseDto,
)
from immichpy.client.generated.models.asset_bulk_upload_check_result import (
    AssetBulkUploadCheckResult,
)
from immichpy.client.generated.models.asset_edit_action import AssetEditAction
from immichpy.client.generated.models.asset_edit_action_item_dto import AssetEditActionItemDto
from immichpy.client.generated.models.asset_edits_response_dto import AssetEditsResponseDto
from immichpy.client.generated.models.asset_file_response_dto import AssetFileResponseDto
from immichpy.client.generated.models.asset_file_type import AssetFileType
from immichpy.client.generated.models.asset_media_status import AssetMediaStatus
from immichpy.client.generated.models.asset_metadata_response_dto import AssetMetadataResponseDto
from immichpy.client.generated.models.asset_reject_reason import AssetRejectReason
from immichpy.client.generated.models.asset_upload_action import AssetUploadAction
from immichpy.client.generated.models.asset_visibility import AssetVisibility
from immichpy.client.generated.models.bulk_id_error_reason import BulkIdErrorReason
from immichpy.client.generated.models.bulk_id_response_dto import BulkIdResponseDto
from immichpy.client.generated.models.partner_direction import PartnerDirection
from immichpy.client.generated.models.partner_response_dto import PartnerResponseDto
from immichpy.client.generated.models.queue_name import QueueName
from immichpy.client.generated.models.queue_response_dto import QueueResponseDto
from immichpy.client.generated.models.queue_statistics_dto import QueueStatisticsDto
from immichpy.client.generated.models.search_album_response_dto import SearchAlbumResponseDto
from immichpy.client.generated.models.search_asset_response_dto import SearchAssetResponseDto
from immichpy.client.generated.models.search_response_dto import SearchResponseDto
from immichpy.client.generated.models.shared_link_response_dto import SharedLinkResponseDto
from immichpy.client.generated.models.shared_link_type import SharedLinkType
from immichpy.client.generated.models.user_avatar_color import UserAvatarColor
from immichpy.client.generated.models.workflow_trigger import WorkflowTrigger
from immichpy.client.main import AsyncClient
from pydantic import ValidationError, validate_call

from ifl import immich
from ifl.immich import Owned, step_matches, summarize_album, webhook_step

# Never connects. Its API objects lend the stubs their method signatures.
PROBE = AsyncClient(base_url="https://immich.example", api_key="unused")


def bound(api: str, method: str, impl):
    """`impl` behind the real signature of `client.<api>.<method>`.

    Each call is first validated as the generated method would validate it, and
    then handed to `impl` exactly as the adapter made it, so tests see what was
    sent and not what pydantic filled in.
    """
    real = getattr(getattr(PROBE, api), method)

    async def check(*args, **kwargs):
        return None

    check.__signature__ = inspect.signature(real)
    check.__annotations__ = real.__annotations__
    checked = validate_call(check)

    async def stub(*args, **kwargs):
        await checked(*args, **kwargs)
        return await impl(*args, **kwargs)

    return stub


def canned(calls: list, api: str, method: str, *values):
    """A bound stub that records each call as (method, args, kwargs) and returns
    the canned values in turn, the last one repeatedly."""
    queue = list(values)

    async def impl(*args, **kwargs):
        calls.append((method, args, kwargs))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return bound(api, method, impl)


def stub_client(calls: list, **apis: dict[str, list]):
    """`stub_client(calls, albums={"delete_album": [None]})`: a client with one
    namespace per API, every method canned and bound."""
    return SimpleNamespace(
        **{
            api: SimpleNamespace(
                **{m: canned(calls, api, m, *values) for m, values in methods.items()}
            )
            for api, methods in apis.items()
        }
    )


def search_page(items, next_page: str | None) -> SearchResponseDto:
    return SearchResponseDto(
        albums=SearchAlbumResponseDto(count=0, facets=[], items=[], total=0),
        assets=SearchAssetResponseDto(
            count=len(items),
            facets=[],
            items=items,
            next_cursor=None,
            next_page=next_page,
            total=len(items),
        ),
    )


def album_dto(name: str, roles: dict, *, asset_count: int = 0) -> AlbumResponseDto:
    return AlbumResponseDto(
        id=uuid4(),
        album_name=name,
        album_thumbnail_asset_id=None,
        album_users=[AlbumUserResponseDto(role=r, user=make_user(u)) for u, r in roles.items()],
        asset_count=asset_count,
        created_at=T0,
        updated_at=T0,
        description="",
        has_shared_link=True,
        is_activity_enabled=False,
        shared=True,
    )


def test_summarize_album_takes_the_owner_from_the_roles():
    dto = album_dto("Trip", {ALICE: EDITOR, FAMILY: OWNER, BOB: VIEWER}, asset_count=3)
    s = summarize_album(dto)
    assert s.id == dto.id and s.name == "Trip" and s.owner_id == FAMILY
    assert s.user_ids == {FAMILY, ALICE, BOB} and s.asset_count == 3
    assert s.roles == {ALICE: EDITOR, FAMILY: OWNER, BOB: VIEWER}
    assert s.emails[ALICE] == "alice@x" and s.has_shared_link and s.description == ""


async def test_album_assets_are_read_page_by_page():
    calls = []
    album_id = uuid4()
    a, b, c = (FakeAsset(uuid4(), ALICE) for _ in range(3))
    client = stub_client(
        calls, search={"search_assets": [search_page([a, b], "2"), search_page([c], None)]}
    )
    got = [x async for x in immich.iter_album_assets(client, album_id, page_size=2)]
    assert [x.id for x in got] == [a.id, b.id, c.id]
    dtos = [
        kw["metadata_search_dto"] if "metadata_search_dto" in kw else args[0]
        for _, args, kw in calls
    ]
    assert [d.page for d in dtos] == [1, 2] and all(d.album_ids == [album_id] for d in dtos)
    assert all(d.size == 2 and d.with_exif is False for d in dtos)


async def test_recent_assets_search_passes_the_mark():
    calls = []
    since = datetime(2026, 10, 1, tzinfo=UTC)
    client = stub_client(calls, search={"search_assets": [search_page([], None)]})
    assert [x async for x in immich.iter_assets_created_after(client, since)] == []
    (dto,) = [args[0] for _, args, _ in calls]
    assert dto.created_after == since and dto.page == 1


async def test_owned_checksums_batches_and_keeps_only_duplicate_rejects():
    calls = []
    hit, trashed = uuid4(), uuid4()

    def answer(dto):
        results = []
        for item in dto.assets:
            if item.checksum == "c1":
                results.append(
                    AssetBulkUploadCheckResult(
                        action=AssetUploadAction.REJECT,
                        id=item.id,
                        asset_id=hit,
                        is_trashed=False,
                        reason=AssetRejectReason.DUPLICATE,
                    )
                )
            elif item.checksum == "c2":
                results.append(
                    AssetBulkUploadCheckResult(
                        action=AssetUploadAction.REJECT,
                        id=item.id,
                        asset_id=trashed,
                        is_trashed=True,
                        reason=AssetRejectReason.DUPLICATE,
                    )
                )
            elif item.checksum == "bad":
                results.append(
                    AssetBulkUploadCheckResult(
                        action=AssetUploadAction.REJECT,
                        id=item.id,
                        reason=AssetRejectReason.UNSUPPORTED_MINUS_FORMAT,
                    )
                )
            else:
                results.append(
                    AssetBulkUploadCheckResult(action=AssetUploadAction.ACCEPT, id=item.id)
                )
        return AssetBulkUploadCheckResponseDto(results=results)

    async def check(dto):
        calls.append(len(dto.assets))
        return answer(dto)

    client = SimpleNamespace(
        assets=SimpleNamespace(check_bulk_upload=bound("assets", "check_bulk_upload", check))
    )
    checksums = ["c1", "c2", "bad", "c1"] + [f"new{i}" for i in range(300)]

    owned = await immich.owned_checksums(client, checksums, batch=250)

    assert owned == {"c1": Owned(hit, False), "c2": Owned(trashed, True)}
    assert calls == [250, 53]  # 303 distinct checksums, the repeated one sent once


@pytest.fixture
async def server():
    """A stand-in for the two Immich calls that carry an original, on a real
    socket: these helpers go under the generated methods to the request
    builders, so only a request that is really sent proves they still work."""
    seen = SimpleNamespace(uploads=[], body=b"", status=AssetMediaStatus.CREATED)

    async def original(request):
        if request.match_info["id"] == str(MISSING):
            return web.json_response({"message": "Not found"}, status=404)
        res = web.StreamResponse()
        await res.prepare(request)
        for i in range(0, len(seen.body), 70_000):
            await res.write(seen.body[i : i + 70_000])
        return res

    async def upload(request):
        parts = {}
        async for part in await request.multipart():
            parts[part.name] = (part.filename, bytes(await part.read()))
        seen.uploads.append((dict(request.headers), parts))
        return web.json_response({"id": str(NEW_ID), "status": seen.status.value}, status=201)

    app = web.Application(client_max_size=1 << 26)
    app.router.add_get("/api/assets/{id}/original", original)
    app.router.add_post("/api/assets", upload)
    async with TestServer(app) as srv:
        client = AsyncClient(base_url=str(srv.make_url("")), api_key="the-key")
        try:
            yield client, seen
        finally:
            await client.close()


MISSING, NEW_ID = uuid4(), uuid4()
BODY = bytes(range(256)) * 12_000  # 3 MB, so it arrives and leaves in several chunks
BODY_SHA1 = base64.b64encode(hashlib.sha1(BODY).digest()).decode()


async def test_download_streams_the_original_to_a_file_and_checks_it(server, tmp_path):
    client, seen = server
    seen.body = BODY
    dest = tmp_path / "original"

    await immich.download_original(client, uuid4(), dest, checksum=BODY_SHA1)
    assert dest.read_bytes() == BODY

    # Bytes that are not the asset's stop the move before anything is uploaded.
    seen.body = BODY[:-1]
    with pytest.raises(immich.DownloadMismatch, match=f"expected {BODY_SHA1}"):
        await immich.download_original(client, uuid4(), dest, checksum=BODY_SHA1)

    with pytest.raises(ApiException) as refused:
        await immich.download_original(client, MISSING, dest, checksum=BODY_SHA1)
    assert refused.value.status == 404


@pytest.mark.parametrize("sidecar", [b"<xmp/>", None])
async def test_upload_streams_the_file_and_names_each_part_itself(server, tmp_path, sidecar):
    client, seen = server
    seen.status = AssetMediaStatus.CREATED if sidecar else AssetMediaStatus.DUPLICATE
    path = tmp_path / "ifl-original"
    path.write_bytes(BODY)
    taken = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)

    asset_id, created = await immich.upload_asset(
        client,
        path=path,
        filename="a.jpg",
        checksum="sha",
        file_created_at=taken,
        file_modified_at=taken,
        is_favorite=True,
        visibility=AssetVisibility.ARCHIVE,
        sidecar=sidecar,
        duration=2000,
    )

    assert asset_id == NEW_ID and created is bool(sidecar)
    ((headers, parts),) = seen.uploads
    assert headers["x-api-key"] == "the-key" and headers["x-immich-checksum"] == "sha"
    assert parts["assetData"] == ("a.jpg", BODY)
    # The server applies a `filename` form field to every part, so the sidecar
    # would be checked as a .jpg. Each part carries its own name instead.
    assert "filename" not in parts
    assert parts.get("sidecarData") == (("a.jpg.xmp", sidecar) if sidecar else None)
    fields = {k: v.decode() for k, (_, v) in parts.items() if not k.endswith("Data")}
    assert fields == {
        "duration": "2000",
        "fileCreatedAt": "2025-06-01T12:00:00+00:00",
        "fileModifiedAt": "2025-06-01T12:00:00+00:00",
        "isFavorite": "true",
        "visibility": "archive",
    }


async def test_tag_asset_accepts_a_duplicate_and_raises_on_anything_else():
    asset_id, tag_id = uuid4(), uuid4()
    dup = [BulkIdResponseDto(id=asset_id, success=False, error=BulkIdErrorReason.DUPLICATE)]
    denied = [
        BulkIdResponseDto(
            id=asset_id, success=False, error=BulkIdErrorReason.NO_PERMISSION, error_message="nope"
        )
    ]
    calls = []
    client = stub_client(calls, tags={"tag_assets": [dup, denied]})
    await immich.tag_asset(client, tag_id, asset_id)  # duplicate: already tagged, fine
    with pytest.raises(immich.Failure, match="tagging failed: no_permission: nope"):
        await immich.tag_asset(client, tag_id, asset_id)
    assert all(kw["id"] == tag_id and kw["bulk_ids_dto"].ids == [asset_id] for _, _, kw in calls)


def test_bulk_failure_reads_an_entry_whatever_immich_left_out():
    def entry(**kw):
        return BulkIdResponseDto(id=uuid4(), **kw)

    assert immich.bulk_failure(entry(success=True)) is None
    dup = entry(success=False, error=BulkIdErrorReason.DUPLICATE)
    assert immich.bulk_failure(dup) is None  # already there
    denied = entry(success=False, error=BulkIdErrorReason.NO_PERMISSION, error_message="nope")
    assert immich.bulk_failure(denied) == "no_permission: nope"
    # A failure with no reason, and one with no message, still read as failures.
    assert immich.bulk_failure(entry(success=False)) == "unknown: no detail"
    assert immich.bulk_failure(entry(success=False, error_message="nope")) == "unknown: nope"


async def test_find_workflow_wants_the_exact_name_and_step_matches_checks_every_field():
    url, token = "http://ifl:8080/hook", "t0k"
    other = make_workflow("immich-family-library-old", [webhook_step(url, token)])
    ours = make_workflow("immich-family-library", [webhook_step(url, token)])
    client = stub_client([], workflows={"search_workflows": [[other, ours]]})
    assert await immich.find_workflow(client, "immich-family-library") is ours
    assert await immich.find_workflow(client, "nothing") is None

    good = webhook_step(url, token)
    assert step_matches(good, url, token)
    assert not step_matches(webhook_step("http://old/hook", token), url, token)
    assert not step_matches(webhook_step(url, "other"), url, token)
    assert not step_matches(good.model_copy(update={"enabled": False}), url, token)
    assert not step_matches(
        good.model_copy(update={"method": "immich-plugin-core#other"}), url, token
    )


async def test_asset_edits_come_back_in_the_shape_the_edits_endpoint_takes():
    asset_id = uuid4()
    # from_dict is what the generated client runs on a response body.
    res = AssetEditsResponseDto.from_dict(
        {
            "assetId": str(asset_id),
            "edits": [
                {
                    "id": str(uuid4()),
                    "action": "crop",
                    "parameters": {"x": 1, "y": 2, "width": 30, "height": 40},
                },
                {"id": str(uuid4()), "action": "rotate", "parameters": {"angle": 90}},
            ],
        }
    )
    client = stub_client([], assets={"get_asset_edits": [res]})
    edits = await immich.get_asset_edits(client, asset_id)
    assert [e.action for e in edits] == [AssetEditAction.CROP, AssetEditAction.ROTATE]
    assert edits[0].parameters.actual_instance.width == 30
    assert edits[1].parameters.actual_instance.angle == 90
    assert not hasattr(edits[0], "id")  # the create shape, not the response shape


async def test_shared_link_assets_come_from_individual_links_only():
    a, b = FakeAsset(uuid4(), ALICE), FakeAsset(uuid4(), ALICE)

    def link(kind, assets):
        return SharedLinkResponseDto(
            allow_download=True,
            allow_upload=False,
            assets=assets,
            created_at=T0,
            description=None,
            expires_at=None,
            id=uuid4(),
            key="k",
            password=None,
            show_metadata=True,
            slug=None,
            type=kind,
            user_id=ALICE,
        )

    links = [link(SharedLinkType.INDIVIDUAL, [a]), link(SharedLinkType.ALBUM, [b])]
    client = stub_client([], shared_links={"get_all_shared_links": [links]})
    assert await immich.shared_link_asset_ids(client) == {a.id}


async def test_queue_backlog_maps_queue_names_to_waiting_counts():
    def queue(name, waiting):
        stats = QueueStatisticsDto(
            active=1, completed=9, delayed=0, failed=0, paused=0, waiting=waiting
        )
        return QueueResponseDto(is_paused=False, name=name, statistics=stats)

    client = stub_client(
        [],
        queues={
            "get_queues": [[queue(QueueName.SIDECAR, 3), queue(QueueName.METADATAEXTRACTION, 0)]]
        },
    )
    assert await immich.queue_backlog(client) == {"sidecar": 3, "metadataExtraction": 0}


async def test_partner_ids_are_the_accounts_this_one_shares_with():
    calls = []
    partner = PartnerResponseDto(
        avatar_color=UserAvatarColor.PRIMARY,
        email="bob@x",
        id=BOB,
        name="bob",
        profile_changed_at=T0,
        profile_image_path="",
    )
    client = stub_client(calls, partners={"get_partners": [[partner]]})
    assert await immich.list_partner_ids(client) == {BOB}
    assert calls[0][2] == {"direction": PartnerDirection.SHARED_MINUS_BY}


async def test_sidecar_lookup_and_metadata_value():
    asset_id, file_id = uuid4(), uuid4()
    sidecar = AssetFileResponseDto(
        created_at=T0,
        id=file_id,
        is_edited=False,
        is_progressive=False,
        is_transparent=False,
        path="upload/a.xmp",
        type=AssetFileType.SIDECAR,
        updated_at=T0,
    )
    calls = []
    client = stub_client(
        calls,
        asset_files={"search_asset_files": [[sidecar], []]},
        assets={
            "get_asset_metadata": [
                [AssetMetadataResponseDto(key="ifl", updated_at=T0, value={"origin_asset_id": "x"})]
            ]
        },
    )
    assert await immich.find_sidecar(client, asset_id) == file_id
    assert await immich.find_sidecar(client, asset_id) is None
    assert calls[0][2] == {"asset_id": asset_id, "type": AssetFileType.SIDECAR}
    assert await immich.get_asset_metadata_value(client, asset_id, "ifl") == {
        "origin_asset_id": "x"
    }
    assert await immich.get_asset_metadata_value(client, asset_id, "other") is None


async def test_album_user_role_update_sends_the_user_id_as_a_string():
    calls = []
    album_id, user_id = uuid4(), uuid4()
    client = stub_client(calls, albums={"update_album_user": [None]})
    await immich.set_album_user_role(client, album_id, user_id, EDITOR)
    (_, _, kw) = calls[0]
    assert kw["id"] == album_id and kw["user_id"] == str(user_id)
    assert kw["update_album_user_dto"].role == EDITOR


async def test_the_stubs_refuse_what_the_installed_client_would():
    """The guard behind every other test here: a stub turns down the calls the
    real client would turn down, so a green run means the adapter still fits."""
    delete = canned([], "albums", "delete_album", None)
    await delete(id=uuid4())
    with pytest.raises(ValidationError):
        await delete(album_id=uuid4())
    with pytest.raises(ValidationError):
        await delete(id="not a uuid")
    with pytest.raises(AttributeError):
        canned([], "albums", "no_such_method", None)


async def test_album_writes_send_what_the_client_takes():
    calls = []
    album_id, a1 = uuid4(), uuid4()
    fam = album_dto("Trip", {FAMILY: OWNER})
    client = stub_client(
        calls,
        albums={
            "create_album": [fam],
            "add_users_to_album": [None],
            "add_assets_to_album": [[BulkIdResponseDto(id=a1, success=True)]],
            "delete_album": [None],
            "get_all_albums": [[fam]],
        },
    )

    assert (await immich.create_album(client, "Trip")).id == fam.id
    await immich.add_album_users(client, album_id, {})  # nothing to add, nothing sent
    await immich.add_album_users(client, album_id, {ALICE: EDITOR, BOB: VIEWER})
    assert await immich.add_album_assets(client, album_id, []) == []
    (res,) = await immich.add_album_assets(client, album_id, [a1])
    assert res.id == a1 and res.success
    await immich.delete_album(client, album_id)
    assert [s.id for s in await immich.list_albums(client)] == [fam.id]
    assert [s.id for s in await immich.albums_containing(client, a1)] == [fam.id]

    names = [name for name, _, _ in calls]
    assert names == [
        "create_album",
        "add_users_to_album",
        "add_assets_to_album",
        "delete_album",
        "get_all_albums",
        "get_all_albums",
    ]
    (create,) = calls[0][1]
    assert create.album_name == "Trip" and create.description is None
    users = calls[1][2]
    assert users["id"] == album_id
    assert [(u.user_id, u.role) for u in users["add_users_dto"].album_users] == [
        (ALICE, EDITOR),
        (BOB, VIEWER),
    ]
    assert calls[2][2]["id"] == album_id and calls[2][2]["bulk_ids_dto"].ids == [a1]
    assert calls[3][2] == {"id": album_id}
    assert calls[4][2] == {} and calls[5][2] == {"asset_id": a1}


async def test_asset_writes_send_what_the_client_takes():
    calls = []
    asset_id, file_id, other = uuid4(), uuid4(), uuid4()
    asset, tag = FakeAsset(asset_id, ALICE), make_tag("family")
    client = stub_client(
        calls,
        assets={
            "get_asset_info": [asset],
            "delete_assets": [None],
            "edit_asset": [None],
            "update_asset_metadata": [None],
        },
        asset_files={"download_asset_file": [b"<xmp/>"]},
        trash={"restore_assets": [None]},
        tags={"upsert_tags": [[tag]]},
        partners={"create_partner": [None]},
    )
    edits = [AssetEditActionItemDto.from_dict({"action": "rotate", "parameters": {"angle": 90}})]

    assert await immich.get_asset(client, asset_id) is asset
    assert await immich.download_asset_file(client, file_id) == b"<xmp/>"
    await immich.trash_assets(client, [asset_id, other])
    await immich.set_asset_edits(client, asset_id, edits)
    await immich.set_asset_metadata(client, asset_id, "ifl", {"origin_asset_id": str(other)})
    await immich.restore_assets(client, [asset_id])
    assert await immich.upsert_tag(client, "family") == tag.id
    await immich.create_partner(client, BOB)

    by_name = {name: (args, kw) for name, args, kw in calls}
    assert by_name["get_asset_info"] == ((), {"id": asset_id})
    assert by_name["download_asset_file"] == ((), {"id": file_id})
    (delete,) = by_name["delete_assets"][0]
    assert delete.ids == [asset_id, other] and delete.force is False  # trash, never delete
    _, kw = by_name["edit_asset"]
    assert kw["id"] == asset_id and kw["asset_edits_create_dto"].edits == edits
    _, kw = by_name["update_asset_metadata"]
    (item,) = kw["asset_metadata_upsert_dto"].items
    assert kw["id"] == asset_id and (item.key, item.value) == (
        "ifl",
        {"origin_asset_id": str(other)},
    )
    (restore,) = by_name["restore_assets"][0]
    assert restore.ids == [asset_id]
    (upsert,) = by_name["upsert_tags"][0]
    assert upsert.tags == ["family"]
    (partner,) = by_name["create_partner"][0]
    assert partner.shared_with_id == BOB


async def test_existing_asset_by_checksum_is_one_bulk_check():
    calls = []
    hit = uuid4()

    async def check(dto):
        calls.append([a.checksum for a in dto.assets])
        return AssetBulkUploadCheckResponseDto(
            results=[
                AssetBulkUploadCheckResult(
                    action=AssetUploadAction.REJECT,
                    id=a.id,
                    asset_id=hit,
                    is_trashed=False,
                    reason=AssetRejectReason.DUPLICATE,
                )
                for a in dto.assets
            ]
        )

    client = SimpleNamespace(
        assets=SimpleNamespace(check_bulk_upload=bound("assets", "check_bulk_upload", check))
    )
    assert await immich.existing_asset_by_checksum(client, "c1") == Owned(hit, False)
    assert calls == [["c1"]]


async def test_workflow_writes_send_what_the_client_takes():
    calls = []
    url, token = "http://ifl:8080/hook", "t0k"
    wf = make_workflow("immich-family-library", [webhook_step(url, token)])
    client = stub_client(calls, workflows={"create_workflow": [wf], "update_workflow": [wf]})

    assert await immich.create_workflow(client, "immich-family-library", url, token) == wf.id
    await immich.update_workflow(client, wf.id, "http://new/hook", token)

    (create,) = calls[0][1]
    assert create.name == "immich-family-library" and create.enabled
    assert create.trigger == WorkflowTrigger.ASSETCREATE
    assert [step_matches(st, url, token) for st in create.steps] == [True]
    kw = calls[1][2]
    update = kw["workflow_update_dto"]
    assert kw["id"] == wf.id and update.enabled and update.trigger == WorkflowTrigger.ASSETCREATE
    assert [step_matches(st, "http://new/hook", token) for st in update.steps] == [True]


async def test_user_names_fall_back_from_config_to_album_to_lookup_to_the_id():
    calls = []
    carol, dave = uuid4(), uuid4()
    client = stub_client(calls, users={"get_user": [make_user(carol, "carol@x")]})
    names = immich.UserNames(client, {ALICE: "alice"})
    album = summarize_album(album_dto("Trip", {FAMILY: OWNER, BOB: EDITOR}))

    assert await names.describe(ALICE, album) == "alice"
    assert await names.describe(BOB, album) == "bob@x"
    assert await names.describe(carol) == "carol@x"
    assert await names.describe(carol) == "carol@x"  # cached, no second lookup
    assert calls == [("get_user", (), {"id": carol})]

    # A user Immich will not show: the id stands in. A fault is not hidden that way.
    from immichpy.client.generated.exceptions import BadRequestException

    async def refused(**kwargs):
        raise immich.ImmichError("users.get_user", BadRequestException(status=400))

    failing = SimpleNamespace(users=SimpleNamespace(get_user=refused))
    assert await immich.UserNames(failing, {}).describe(dave) == str(dave)
    broken = stub_client([], users={"get_user": [None]})
    with pytest.raises(AttributeError):
        await immich.UserNames(broken, {}).describe(dave)


async def test_get_asset_if_readable_takes_only_a_400_to_mean_no_access(monkeypatch):
    from immichpy.client.generated.exceptions import BadRequestException, ServiceException

    def answered(status):
        kind = BadRequestException if status == 400 else ServiceException
        return immich.ImmichError("assets.get_asset_info", kind(status=status))

    asset, asset_id = object(), uuid4()
    answers = [asset, answered(400), answered(502)]

    async def get_asset(client, asset_id):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(immich, "get_asset", get_asset)

    assert await immich.get_asset_if_readable(None, asset_id) is asset
    assert await immich.get_asset_if_readable(None, asset_id) is None
    with pytest.raises(immich.ImmichError, match="Immich answered 502"):
        await immich.get_asset_if_readable(None, asset_id)


def test_an_immich_error_is_one_line_that_names_the_call():
    from immichpy.client.generated.exceptions import BadRequestException, ServiceException

    def said(cause):
        return str(immich.ImmichError("albums.get_all_albums", cause))

    refused = BadRequestException(
        status=400,
        reason="Bad Request",
        body='{"message":"Not found or no asset.read access","error":"Bad Request"}',
    )
    refused.headers = {"Content-Type": "application/json", "X-Powered-By": "Express"}
    assert said(refused) == (
        "Immich answered 400 to albums.get_all_albums: Not found or no asset.read access"
    )
    assert immich.ImmichError("x", refused).status == 400
    # A validation error lists its messages. A body that is not JSON falls back to the reason.
    many = BadRequestException(status=400, body='{"message":["id must be a UUID","size too big"]}')
    assert said(many).endswith(": id must be a UUID; size too big")
    gateway = ServiceException(status=502, reason="Bad Gateway", body="<html>nginx</html>")
    assert said(gateway) == "Immich answered 502 to albums.get_all_albums: Bad Gateway"
    assert said(ServiceException(status=500)).endswith(": no detail")
    # Whatever Immich puts in the message, the result is one line.
    odd = BadRequestException(status=None, body='{"message":{"id":"must be\\na UUID"}}')
    assert "\n" not in said(odd) and said(odd).startswith("Immich answered with no status to ")

    down = immich.ImmichError("albums.get_all_albums", TimeoutError())
    assert str(down) == "albums.get_all_albums got no usable answer from Immich: TimeoutError"
    assert down.status is None

    assert immich.describe_error(down) == str(down)  # a Failure is its own text
    assert immich.describe_error(RuntimeError("went\naway")) == "RuntimeError: went away"


def test_log_failure_keeps_the_traceback_for_faults_in_the_service(caplog):
    import logging

    log = logging.getLogger("ifl.test")
    down = immich.ImmichError("albums.get_all_albums", TimeoutError())
    cases = [
        (immich.Failure("the copy is in the trash"), "the copy is in the trash", False),
        (immich.DownloadMismatch("got 3 bytes"), "got 3 bytes", False),
        (down, str(down), False),
        # Above the guarded client, an error's type says nothing about where it
        # came from. All of these are the service's own until proved otherwise.
        (TimeoutError(), "TimeoutError", True),
        (ConnectionResetError("reset by peer"), "ConnectionResetError: reset by peer", True),
        (FileNotFoundError("no such file"), "FileNotFoundError: no such file", True),
        (KeyError("owner"), "KeyError: 'owner'", True),
    ]
    for error, why, fault in cases:
        for level in (logging.ERROR, logging.WARNING):
            caplog.clear()
            with caplog.at_level(logging.DEBUG, logger="ifl.test"):
                assert immich.log_failure(log, "a.jpg not moved", error, level=level) == why
            said, *rest = caplog.records
            assert said.getMessage() == f"a.jpg not moved: {why}"
            assert said.funcName == "test_log_failure_keeps_the_traceback_for_faults_in_the_service"
            if fault:
                # A fault is an error with its traceback, whatever level was asked for.
                assert said.levelno == logging.ERROR and said.exc_info and not rest
            else:
                # A failure is one line at that level; the traceback is for debug.
                assert said.levelno == level and not said.exc_info
                assert [r.levelno for r in rest] == [logging.DEBUG] and rest[0].exc_info


async def test_the_guarded_client_turns_whatever_a_call_raises_into_an_immich_error():
    """Against a real socket and the real client, so that the wrapper is shown
    to find the client's methods by itself."""
    readable, refused, garbled = uuid4(), uuid4(), uuid4()

    async def asset(request):
        asset_id = request.match_info["id"]
        if asset_id == str(refused):
            return web.json_response({"message": "Not found or no asset.read access"}, status=400)
        if asset_id == str(garbled):
            return web.json_response({"id": "this is no asset"})
        body = FakeAsset(readable, ALICE).to_json()
        return web.Response(text=body, content_type="application/json")

    app = web.Application()
    app.router.add_get("/api/assets/{id}", asset)
    async with TestServer(app) as srv:
        url = str(srv.make_url(""))
        client = immich.guard(AsyncClient(base_url=url, api_key="the-key"))
        try:
            # A call that works is untouched.
            assert (await immich.get_asset(client, readable)).id == readable

            with pytest.raises(immich.ImmichError) as failed:
                await immich.get_asset(client, refused)
            assert failed.value.status == 400 and str(failed.value) == (
                "Immich answered 400 to assets.get_asset_info: Not found or no asset.read access"
            )
            assert await immich.get_asset_if_readable(client, refused) is None

            # A reply the client cannot read: no status, and the model named.
            with pytest.raises(immich.ImmichError) as failed:
                await immich.get_asset(client, garbled)
            assert failed.value.status is None and str(failed.value).startswith(
                "assets.get_asset_info got no usable answer from Immich: ValidationError"
            )
            assert "\n" not in str(failed.value)

            # A name that does not exist is the service's own mistake, and stays one.
            assert not hasattr(client.assets, "no_such_call")
        finally:
            await client.close()

    # Nothing listening any more.
    gone = immich.guard(AsyncClient(base_url=url, api_key="the-key"))
    try:
        with pytest.raises(immich.ImmichError) as failed:
            await immich.get_asset(gone, readable)
        assert failed.value.status is None
        assert "assets.get_asset_info got no usable answer from Immich: Client" in str(failed.value)
    finally:
        await gone.close()


async def test_the_transfers_work_through_the_guarded_client(server, tmp_path):
    raw, seen = server
    client = immich.guard(raw)
    seen.body = BODY
    dest = tmp_path / "original"

    await immich.download_original(client, uuid4(), dest, checksum=BODY_SHA1)
    assert dest.read_bytes() == BODY

    with pytest.raises(immich.ImmichError) as failed:
        await immich.download_original(client, MISSING, dest, checksum=BODY_SHA1)
    assert failed.value.status == 404

    # A checksum that does not match is the service's own finding, not Immich's error.
    with pytest.raises(immich.DownloadMismatch):
        await immich.download_original(client, uuid4(), dest, checksum="not-it")


def test_immich_call_leaves_the_services_own_errors_alone():
    for own in (immich.Failure("no copy"), TypeError("bad argument"), AttributeError("x")):
        with pytest.raises(type(own)) as raised, immich.immich_call("albums.get_all_albums"):
            raise own
        assert raised.value is own
    with pytest.raises(immich.ImmichError) as raised, immich.immich_call("albums.get_all_albums"):
        raise OSError("connection reset")
    assert isinstance(raised.value.__cause__, OSError)


async def test_an_immich_error_names_the_line_of_the_service_that_wanted_the_call():
    import re

    from ifl.accounts import Account, Accounts
    from ifl.observer import member_asset

    async def down(**kwargs):
        raise TimeoutError

    # One client method, `get_asset_info`, serves the move, the webhook and the
    # scan. The place says which of them was at work.
    client = immich.guard(SimpleNamespace(assets=SimpleNamespace(get_asset_info=down)))
    alice = Account(name="alice", email="a@x", user_id=uuid4(), client=client)
    accounts = Accounts(family=alice, members={"alice": alice}, admin=None)

    with pytest.raises(immich.ImmichError) as failed:
        await member_asset(accounts, uuid4())

    # The helpers in immich.py are skipped: the caller of the helper is the news.
    assert re.fullmatch(r"observer\.py:\d+ in member_asset", failed.value.place)
    assert str(failed.value) == (
        "assets.get_asset_info got no usable answer from Immich: TimeoutError "
        f"({failed.value.place})"
    )
    # Built outside the service, as a test does, it has no place to name.
    assert immich.ImmichError("x", TimeoutError()).place is None
