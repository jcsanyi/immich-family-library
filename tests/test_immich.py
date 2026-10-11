"""The adapter layer over immichpy, against stub clients that return real models.

The fake in fakes.py replaces these helpers wholesale, so nothing else checks
that each one reads the right fields and sends the right arguments. These tests
do, for every helper with logic in it. Pure one-line pass-throughs are left out.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fakes import ALICE, BOB, EDITOR, FAMILY, OWNER, T0, VIEWER, FakeAsset, make_user, make_workflow
from immichpy.client.generated.models.album_response_dto import AlbumResponseDto
from immichpy.client.generated.models.album_user_response_dto import AlbumUserResponseDto
from immichpy.client.generated.models.asset_bulk_upload_check_response_dto import (
    AssetBulkUploadCheckResponseDto,
)
from immichpy.client.generated.models.asset_bulk_upload_check_result import (
    AssetBulkUploadCheckResult,
)
from immichpy.client.generated.models.asset_edit_action import AssetEditAction
from immichpy.client.generated.models.asset_edits_response_dto import AssetEditsResponseDto
from immichpy.client.generated.models.asset_file_response_dto import AssetFileResponseDto
from immichpy.client.generated.models.asset_file_type import AssetFileType
from immichpy.client.generated.models.asset_media_response_dto import AssetMediaResponseDto
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

from ifl import immich
from ifl.immich import Owned, step_matches, summarize_album, webhook_step


def canned(calls: list, name: str, *values):
    """An async stand-in for one generated-client method. Records each call and
    returns the canned values in turn, the last one repeatedly."""
    queue = list(values)

    async def method(*args, **kwargs):
        calls.append((name, args, kwargs))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return method


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


def test_summarize_album_takes_the_owner_from_the_roles():
    dto = AlbumResponseDto(
        id=uuid4(),
        album_name="Trip",
        album_thumbnail_asset_id=None,
        album_users=[
            AlbumUserResponseDto(role=EDITOR, user=make_user(ALICE)),
            AlbumUserResponseDto(role=OWNER, user=make_user(FAMILY)),
            AlbumUserResponseDto(role=VIEWER, user=make_user(BOB)),
        ],
        asset_count=3,
        created_at=T0,
        updated_at=T0,
        description="",
        has_shared_link=True,
        is_activity_enabled=False,
        shared=True,
    )
    s = summarize_album(dto)
    assert s.id == dto.id and s.name == "Trip" and s.owner_id == FAMILY
    assert s.user_ids == {FAMILY, ALICE, BOB} and s.asset_count == 3
    assert s.roles == {ALICE: EDITOR, FAMILY: OWNER, BOB: VIEWER}
    assert s.emails[ALICE] == "alice@x" and s.has_shared_link and s.description == ""


async def test_album_assets_are_read_page_by_page():
    calls = []
    album_id = uuid4()
    a, b, c = (FakeAsset(uuid4(), ALICE) for _ in range(3))
    client = SimpleNamespace(
        search=SimpleNamespace(
            search_assets=canned(calls, "search", search_page([a, b], "2"), search_page([c], None))
        )
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
    client = SimpleNamespace(
        search=SimpleNamespace(search_assets=canned(calls, "search", search_page([], None)))
    )
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

    client = SimpleNamespace(assets=SimpleNamespace(check_bulk_upload=check))
    checksums = ["c1", "c2", "bad", "c1"] + [f"new{i}" for i in range(300)]

    owned = await immich.owned_checksums(client, checksums, batch=250)

    assert owned == {"c1": Owned(hit, False), "c2": Owned(trashed, True)}
    assert calls == [250, 53]  # 303 distinct checksums, the repeated one sent once


@pytest.mark.parametrize("sidecar", [b"<xmp/>", None])
async def test_upload_names_each_part_itself_and_reports_whether_it_was_created(sidecar):
    calls = []
    new_id = uuid4()
    status = AssetMediaStatus.CREATED if sidecar else AssetMediaStatus.DUPLICATE
    client = SimpleNamespace(
        assets=SimpleNamespace(
            upload_asset=canned(calls, "upload", AssetMediaResponseDto(id=new_id, status=status))
        )
    )
    taken = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)

    asset_id, created = await immich.upload_asset(
        client,
        data=b"jpeg-bytes",
        filename="a.jpg",
        checksum="sha",
        file_created_at=taken,
        file_modified_at=taken,
        is_favorite=True,
        visibility=AssetVisibility.ARCHIVE,
        sidecar=sidecar,
        duration=None,
    )

    assert asset_id == new_id and created is bool(sidecar)
    (_, _, kw) = calls[0]
    # The server applies a `filename` form field to every part, so the sidecar
    # would be checked as a .jpg. Each part carries its own name instead.
    assert "filename" not in kw
    assert kw["asset_data"] == ("a.jpg", b"jpeg-bytes") and kw["x_immich_checksum"] == "sha"
    assert kw["sidecar_data"] == (("a.jpg.xmp", sidecar) if sidecar else None)
    assert kw["is_favorite"] is True and kw["visibility"] == AssetVisibility.ARCHIVE
    assert kw["file_created_at"] == taken and kw["duration"] is None


async def test_tag_asset_accepts_a_duplicate_and_raises_on_anything_else():
    asset_id, tag_id = uuid4(), uuid4()
    dup = [BulkIdResponseDto(id=asset_id, success=False, error=BulkIdErrorReason.DUPLICATE)]
    denied = [
        BulkIdResponseDto(
            id=asset_id, success=False, error=BulkIdErrorReason.NO_PERMISSION, error_message="nope"
        )
    ]
    calls = []
    client = SimpleNamespace(tags=SimpleNamespace(tag_assets=canned(calls, "tag", dup, denied)))
    await immich.tag_asset(client, tag_id, asset_id)  # duplicate: already tagged, fine
    with pytest.raises(RuntimeError, match="NO_PERMISSION nope"):
        await immich.tag_asset(client, tag_id, asset_id)
    assert all(kw["id"] == tag_id and kw["bulk_ids_dto"].ids == [asset_id] for _, _, kw in calls)


async def test_find_workflow_wants_the_exact_name_and_step_matches_checks_every_field():
    url, token = "http://ifl:8080/hook", "t0k"
    other = make_workflow("immich-family-library-old", [webhook_step(url, token)])
    ours = make_workflow("immich-family-library", [webhook_step(url, token)])
    client = SimpleNamespace(
        workflows=SimpleNamespace(search_workflows=canned([], "search", [other, ours]))
    )
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
    client = SimpleNamespace(assets=SimpleNamespace(get_asset_edits=canned([], "edits", res)))
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
    client = SimpleNamespace(
        shared_links=SimpleNamespace(get_all_shared_links=canned([], "links", links))
    )
    assert await immich.shared_link_asset_ids(client) == {a.id}


async def test_queue_backlog_maps_queue_names_to_waiting_counts():
    def queue(name, waiting):
        stats = QueueStatisticsDto(
            active=1, completed=9, delayed=0, failed=0, paused=0, waiting=waiting
        )
        return QueueResponseDto(is_paused=False, name=name, statistics=stats)

    client = SimpleNamespace(
        queues=SimpleNamespace(
            get_queues=canned(
                [], "queues", [queue(QueueName.SIDECAR, 3), queue(QueueName.METADATAEXTRACTION, 0)]
            )
        )
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
    client = SimpleNamespace(
        partners=SimpleNamespace(get_partners=canned(calls, "partners", [partner]))
    )
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
    client = SimpleNamespace(
        asset_files=SimpleNamespace(search_asset_files=canned(calls, "files", [sidecar], [])),
        assets=SimpleNamespace(
            get_asset_metadata=canned(
                [],
                "meta",
                [
                    AssetMetadataResponseDto(
                        key="ifl", updated_at=T0, value={"origin_asset_id": "x"}
                    )
                ],
            )
        ),
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
    client = SimpleNamespace(albums=SimpleNamespace(update_album_user=canned(calls, "role", None)))
    await immich.set_album_user_role(client, album_id, user_id, EDITOR)
    (_, _, kw) = calls[0]
    assert kw["id"] == album_id and kw["user_id"] == str(user_id)
    assert kw["update_album_user_dto"].role == EDITOR
