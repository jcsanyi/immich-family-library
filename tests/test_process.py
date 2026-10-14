"""One pass end to end against the fake Immich. No network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fakes import (
    ALICE,
    BOB,
    EDITOR,
    FAMILY,
    T0,
    TEMP_DIR,
    FakeAsset,
    FakeClient,
    make_accounts,
    patch_world,
)
from immichpy.client.generated.models.bulk_id_error_reason import BulkIdErrorReason
from immichpy.client.generated.models.bulk_id_response_dto import BulkIdResponseDto

from ifl.accounts import Account
from ifl.config import parse_config
from ifl.immich import ImmichError
from ifl.process import run_pass

RAW = {
    "immich": {"url": "https://x.example"},
    "service": {"readonly": False, "settle_seconds": 0, "temp_dir": str(TEMP_DIR)},
    "family": {"email": "f@x", "api_key": "k", "dropbox_album": "Drop"},
    "members": {
        "alice": {"email": "alice@x", "api_key": "k"},
        "bob": {"email": "bob@x", "api_key": "k"},
    },
}


@pytest.fixture
def world(monkeypatch):
    w = patch_world(monkeypatch, "ifl.observer", "ifl.convert", "ifl.reconcile", "ifl.move")
    w.partners[FAMILY] = {ALICE, BOB}
    return w


async def test_a_pass_converts_reconciles_and_moves_in_order(world):
    accounts = make_accounts()
    cfg = parse_config(RAW)
    # A member album shared with the family: converted, then its photo moved, same pass.
    beach = world.add_asset(
        FakeAsset(uuid4(), ALICE, checksum="beach", original_file_name="beach.jpg")
    )
    world.album("Beach", ALICE, roles={FAMILY: EDITOR}, assets=[beach])
    # A family album missing bob: reconciled. Its photo moves too.
    trip_photo = world.add_asset(
        FakeAsset(uuid4(), ALICE, checksum="trip", original_file_name="trip.jpg")
    )
    trip = world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[trip_photo])
    # No dropbox yet: created.

    result = await run_pass(accounts, cfg)

    assert len(result.converted.done) == 1 and not result.converted.failed
    assert result.reconciled and result.reconciled.created_dropbox
    assert result.reconciled.shared == 2 + 1  # dropbox gets both members; bob added to Trip
    assert result.backlog is None and result.moved is not None
    assert sorted(r.file_name for r in result.moved.done) == ["beach.jpg", "trip.jpg"]
    assert beach.is_trashed and trip_photo.is_trashed
    # Trashed originals stay in the album rows, as in Immich; the live contents are the copies.
    (fam_beach,) = world.family_albums_named("Beach")
    for album in (fam_beach, trip):
        live = [a for a in world.assets[album.id] if not a.is_trashed]
        assert len(live) == 1 and live[0].owner_id == FAMILY
    assert result.mark is not None  # nothing was left unfinished
    assert not result.failed
    assert str(result).startswith(
        "albums: converted 1, failed 0, remaining 0; family albums: created dropbox"
    )

    # A second pass finds nothing to do.
    again = await run_pass(accounts, cfg)
    assert not again.converted.done and again.reconciled is None
    assert again.moved is not None and not again.moved.done
    assert str(again).endswith("assets: moved 0, blocked 0, failed 0, remaining 0")


async def test_a_pass_takes_external_library_photos_out_of_the_dropbox(world, caplog):
    accounts = make_accounts()
    cfg = parse_config({**RAW, "limits": {"assets_per_pass": 1}})

    def nas(owner, name):
        return world.add_asset(FakeAsset(uuid4(), owner, name, f"{name}.jpg", library_id=uuid4()))

    a1, a2, b1 = nas(ALICE, "a1"), nas(ALICE, "a2"), nas(BOB, "b1")
    phone = world.add_asset(FakeAsset(uuid4(), ALICE, "phone", "phone.jpg"))
    drop = world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR}, assets=[a1, a2, b1])
    world.assets[drop.id].append(phone)
    trip = world.album("Trip", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR}, assets=[a1])

    result = await run_pass(accounts, cfg)

    # Each member's key took out that member's own, and the limit didn't apply.
    assert [w for w in world.writes if w[0] == "remove"] == [
        ("remove", ALICE, drop.id, {a1.id, a2.id}),
        ("remove", BOB, drop.id, {b1.id}),
    ]
    assert result.dropbox.removed == {"alice": 2, "bob": 1} and not result.failed
    # Out of the dropbox and nothing else: still theirs, untagged, and where it was.
    assert all(not a.is_trashed and not a.tags for a in (a1, a2, b1))
    assert world.assets[trip.id] == [a1] and len(world.uploads) == 1
    assert [a for a in world.assets[drop.id] if not a.is_trashed] == []
    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == [
        "alice: 2 photos from an external library taken out of 'Drop'; "
        "the service doesn't support moving these",
        "bob: 1 photo from an external library taken out of 'Drop'; "
        "the service doesn't support moving these",
    ]

    before = len(world.writes)
    again = await run_pass(accounts, cfg)
    assert again.dropbox is None and not [w for w in world.writes[before:] if w[0] == "remove"]


async def test_a_photo_already_out_of_the_dropbox_is_neither_taken_out_nor_failed(world, caplog):
    accounts = make_accounts()
    cfg = parse_config(RAW)
    a = world.add_asset(FakeAsset(uuid4(), ALICE, "a", "a.jpg", library_id=uuid4()))
    b = world.add_asset(FakeAsset(uuid4(), ALICE, "b", "b.jpg", library_id=uuid4()))
    drop = world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR}, assets=[a, b])
    real = world.remove_album_assets

    # Alice takes one out by hand between the scan and the removal.
    async def alice_first(client, album_id, ids):
        world.assets[drop.id].remove(a)
        return await real(client, album_id, ids)

    with pytest.MonkeyPatch.context() as mp, caplog.at_level("INFO", logger="ifl"):
        mp.setattr("ifl.move.remove_album_assets", alice_first)
        result = await run_pass(accounts, cfg)

    assert result.dropbox.removed == {"alice": 1} and not result.failed
    assert "a.jpg was already out of 'Drop'" in caplog.text
    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == [
        "alice: 1 photo from an external library taken out of 'Drop'; "
        "the service doesn't support moving these",
    ]
    assert str(result).endswith("; dropbox: took out 1, failed 0")


async def test_a_photo_the_dropbox_wont_release_fails_the_pass_and_is_tried_again(world, caplog):
    accounts = make_accounts()
    cfg = parse_config(RAW)
    a = world.add_asset(FakeAsset(uuid4(), ALICE, "a", "a.jpg", library_id=uuid4()))
    b = world.add_asset(FakeAsset(uuid4(), BOB, "b", "b.jpg", library_id=uuid4()))
    drop = world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR}, assets=[a, b])
    real = world.remove_album_assets

    async def down_for_alice(client, album_id, ids):
        if client.user_id == ALICE:
            raise ImmichError("albums.remove_asset_from_album", TimeoutError())
        return await real(client, album_id, ids)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("ifl.move.remove_album_assets", down_for_alice)
        result = await run_pass(accounts, cfg)

    assert result.failed and result.dropbox.removed == {"bob": 1}
    assert [name for name, _ in result.dropbox.failed] == ["a.jpg"]
    assert "photos of alice not taken out of 'Drop'" in caplog.text
    assert world.assets[drop.id] == [a]

    assert str(result).endswith("; dropbox: took out 1, failed 1")

    # Immich answers, and says no to that one photo.
    async def refuse(client, album_id, ids):
        return [
            BulkIdResponseDto(id=i, success=False, error=BulkIdErrorReason.NO_PERMISSION)
            for i in ids
        ]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("ifl.move.remove_album_assets", refuse)
        refused = await run_pass(accounts, cfg)
    assert refused.failed and refused.dropbox.failed == [("a.jpg", "no_permission: no detail")]
    assert "a.jpg not taken out of 'Drop': no_permission: no detail" in caplog.text

    again = await run_pass(accounts, cfg)
    assert again.dropbox.removed == {"alice": 1} and not again.failed


async def test_limits_apply_per_pass(world):
    accounts = make_accounts()
    cfg = parse_config({**RAW, "limits": {"albums_per_pass": 1, "assets_per_pass": 1}})
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    for name in ("A", "B"):
        a = world.add_asset(
            FakeAsset(uuid4(), ALICE, checksum=name, original_file_name=f"{name}.jpg")
        )
        world.album(name, ALICE, roles={FAMILY: EDITOR}, assets=[a])

    result = await run_pass(accounts, cfg)

    assert len(result.converted.done) == 1 and result.converted.remaining == 1
    assert result.moved and len(result.moved.done) == 1 and result.moved.remaining == 0
    # Only the converted album's photo was movable this pass; the other is still in alice's album.


async def test_a_queue_backlog_skips_the_moves_but_not_the_albums(world):
    accounts = make_accounts()
    accounts.admin = Account(
        name="admin", email="root@x", user_id=uuid4(), client=FakeClient(uuid4())
    )
    cfg = parse_config(RAW)
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    a = world.add_asset(FakeAsset(uuid4(), ALICE, checksum="x", original_file_name="x.jpg"))
    world.album("X", ALICE, roles={FAMILY: EDITOR}, assets=[a])
    world.queues = {"metadataExtraction": 40}

    result = await run_pass(accounts, cfg, mark=T0)

    assert len(result.converted.done) == 1
    assert result.backlog == {"metadataExtraction": 40} and result.moved is None
    assert result.mark == T0  # no scan ran, so the mark stays
    assert not a.is_trashed and not result.failed
    assert "skipped, Immich has jobs waiting" in str(result)


async def test_reuploads_are_handled_and_the_mark_is_held_while_one_is_unfinished(world):
    accounts = make_accounts()
    cfg = parse_config(RAW)
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    start = datetime.now(UTC) - timedelta(days=2)
    old = start + timedelta(hours=1)
    fresh = datetime.now(UTC) - timedelta(seconds=5)
    copy = world.add_asset(
        FakeAsset(uuid4(), FAMILY, checksum="beach", original_file_name="beach.jpg")
    )
    world.metadata[copy.id] = {
        "ifl": {"origin_user_id": str(ALICE), "origin_asset_id": str(uuid4())}
    }
    # A re-upload needs no settling: seconds old and not yet extracted, it still goes.
    alices = world.add_asset(
        FakeAsset(
            uuid4(),
            ALICE,
            checksum="beach",
            original_file_name="beach.jpg",
            created_at=fresh,
            updated_at=fresh,
            exif_info=None,
        )
    )
    # Bob's is in one of his shared links, so it's blocked this pass.
    hike = world.add_asset(
        FakeAsset(uuid4(), FAMILY, checksum="hike", original_file_name="hike.jpg")
    )
    world.metadata[hike.id] = {"ifl": {"origin_user_id": str(BOB), "origin_asset_id": str(uuid4())}}
    bobs = world.add_asset(
        FakeAsset(uuid4(), BOB, checksum="hike", original_file_name="hike.jpg", created_at=old)
    )
    world.shared_links[BOB] = {bobs.id}

    result = await run_pass(accounts, cfg, mark=start)

    assert [r.file_name for r in result.moved.done] == ["beach.jpg"] and result.moved.done[
        0
    ].duplicate
    assert alices.is_trashed and not bobs.is_trashed
    assert [b[1] for b in result.moved.blocked] == ["in one of the owner's shared links"]
    assert result.mark == start  # held: past it, bob's would not be read again
    assert result.moved.reuploads_trashed() == {ALICE: 1}
    # Bob removes it from the link: found again from the held mark, handled, mark released.
    world.shared_links[BOB] = set()
    again = await run_pass(accounts, cfg, mark=result.mark)
    assert [r.file_name for r in again.moved.done] == ["hike.jpg"] and bobs.is_trashed
    assert again.mark > fresh and again.moved.reuploads_trashed() == {BOB: 1}


async def test_in_flight_assets_are_left_to_the_other_path(world):
    accounts = make_accounts()
    cfg = parse_config(RAW)
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    copy = world.add_asset(
        FakeAsset(uuid4(), FAMILY, checksum="beach", original_file_name="beach.jpg")
    )
    world.metadata[copy.id] = {
        "ifl": {"origin_user_id": str(ALICE), "origin_asset_id": str(uuid4())}
    }
    re = world.add_asset(
        FakeAsset(
            uuid4(),
            ALICE,
            checksum="beach",
            original_file_name="beach.jpg",
            created_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    busy = {re.id}  # the receiver has it

    result = await run_pass(accounts, cfg, in_flight=busy, mark=T0)

    assert result.moved.skipped_in_flight == 1 and not result.moved.done and not re.is_trashed
    assert result.mark == T0  # held back, in case the other path fails
    assert busy == {re.id}  # not ours to remove


async def test_reconcile_sets_up_each_members_webhook_workflow(world):
    from fakes import make_workflow

    from ifl.immich import webhook_step
    from ifl.observer import WORKFLOW_NAME

    accounts = make_accounts()
    cfg = parse_config({**RAW, "webhook": {"url": "http://ifl:8080/hook", "token": "t0k"}})
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    # bob has one pointing at an old URL; alice has none.
    stale = make_workflow(WORKFLOW_NAME, [webhook_step("http://old/hook", "t0k")])
    world.workflows[BOB] = [stale]

    result = await run_pass(accounts, cfg)

    assert result.reconciled and result.reconciled.workflows == 2
    assert ("workflow", ALICE, "create") in world.writes and (
        "workflow",
        BOB,
        "update",
    ) in world.writes
    assert stale.steps[0].config["url"] == "http://ifl:8080/hook"
    again = await run_pass(accounts, cfg)
    assert again.reconciled is None
