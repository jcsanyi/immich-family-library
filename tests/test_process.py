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

from ifl.accounts import Account
from ifl.config import parse_config
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
