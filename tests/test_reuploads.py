"""The re-upload source of the asset scan. No network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fakes import ALICE, BOB, EDITOR, FAMILY, FakeAsset, make_accounts, patch_world

from ifl.observer import REUPLOAD_MARGIN, describe_assets, scan_assets

T0 = datetime(2026, 10, 1, tzinfo=UTC)  # older than the window
T1 = datetime(2026, 10, 9, tzinfo=UTC)  # inside it
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def world(monkeypatch):
    w = patch_world(monkeypatch, "ifl.observer")
    w.partners[FAMILY] = {ALICE, BOB}
    return w


def family_has(world, checksum):
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    world.add_asset(
        FakeAsset(uuid4(), FAMILY, checksum=checksum, original_file_name=f"{checksum}.jpg")
    )


def upload(world, who, checksum, when):
    return world.add_asset(
        FakeAsset(
            uuid4(), who, checksum=checksum, original_file_name=f"{checksum}.jpg", created_at=when
        )
    )


async def test_without_a_mark_the_scan_reads_the_window(world):
    accounts = make_accounts()
    family_has(world, "beach")
    family_has(world, "old")
    # Alice's phone re-uploads beach after the purge; she also uploads something new.
    re = upload(world, ALICE, "beach", T1)
    upload(world, ALICE, "fresh", T1)
    # Bob's re-upload is older than the window: never looked for.
    upload(world, BOB, "old", T0)

    scan = await scan_assets(accounts, "Drop", now=NOW)

    assert [(m.file_name, m.owner.name, m.reupload) for m in scan.moves] == [
        ("beach.jpg", "alice", True)
    ]
    assert scan.moves[0].asset_id == re.id and scan.moves[0].album is None
    assert scan.scanned_at == NOW and scan.reuploads_by_owner() == {"alice": 1}
    assert describe_assets(scan) == ["REUPLOAD beach.jpg from alice: family already has it"]


async def test_with_a_mark_the_scan_reads_from_a_little_before_it(world):
    accounts = make_accounts()
    family_has(world, "beach")
    upload(world, ALICE, "beach", T1)

    late = await scan_assets(accounts, "Drop", mark=T1 + REUPLOAD_MARGIN / 2, now=NOW)
    past = await scan_assets(accounts, "Drop", mark=T1 + REUPLOAD_MARGIN * 2, now=NOW)

    assert [m.file_name for m in late.moves] == ["beach.jpg"]  # an upload that landed late
    assert not past.moves


async def test_a_mark_older_than_the_window_reads_only_the_window(world):
    accounts = make_accounts()
    family_has(world, "old")
    upload(world, BOB, "old", T0)

    scan = await scan_assets(accounts, "Drop", mark=T0 - timedelta(days=1), now=NOW)

    assert not scan.moves


async def test_a_trashed_family_copy_is_skipped_and_a_family_album_asset_is_not_doubled(world):
    accounts = make_accounts()
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    world.add_asset(
        FakeAsset(uuid4(), FAMILY, checksum="cat", original_file_name="cat.jpg", is_trashed=True)
    )
    upload(world, ALICE, "cat", T1)
    # An asset already found in a family album is a plain move, not also a re-upload.
    world.add_asset(FakeAsset(uuid4(), FAMILY, checksum="trip", original_file_name="trip.jpg"))
    trip = upload(world, ALICE, "trip", T1)
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[trip])

    scan = await scan_assets(accounts, "Drop", now=NOW)

    assert [(m.file_name, m.reupload) for m in scan.moves] == [("trip.jpg", False)]
    assert scan.skipped_reuploads == ["cat.jpg from alice: the family's copy is in the trash"]
    assert (
        "SKIP re-upload cat.jpg from alice: the family's copy is in the trash"
        in describe_assets(scan)
    )


async def test_reuploads_are_listed_before_the_album_moves(world):
    accounts = make_accounts()
    family_has(world, "beach")
    trip = upload(world, ALICE, "trip", T1)
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[trip])
    upload(world, BOB, "beach", T1)

    scan = await scan_assets(accounts, "Drop", now=NOW)

    assert [(m.file_name, m.reupload) for m in scan.moves] == [
        ("beach.jpg", True),
        ("trip.jpg", False),
    ]
