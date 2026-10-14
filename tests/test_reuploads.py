"""The re-upload source of the asset scan. No network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fakes import ALICE, BOB, EDITOR, FAMILY, FakeAsset, make_accounts, patch_world

from ifl.observer import REUPLOAD_MARGIN, describe_assets, reuploads_among, scan_assets

T0 = datetime(2026, 10, 1, tzinfo=UTC)  # older than the window
T1 = datetime(2026, 10, 9, tzinfo=UTC)  # inside it
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def world(monkeypatch):
    w = patch_world(monkeypatch, "ifl.observer")
    w.partners[FAMILY] = {ALICE, BOB}
    return w


def family_has(world, checksum, *, origin=None):
    """A family copy of the file. With `origin`, one that the service moved from
    that member; without, one the family got some other way."""
    if not world.family_albums_named("Drop"):
        world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    copy = world.add_asset(
        FakeAsset(uuid4(), FAMILY, checksum=checksum, original_file_name=f"{checksum}.jpg")
    )
    if origin:
        world.metadata[copy.id] = {
            "ifl": {"origin_user_id": str(origin), "origin_asset_id": str(uuid4())}
        }
    return copy


def upload(world, who, checksum, when):
    return world.add_asset(
        FakeAsset(
            uuid4(), who, checksum=checksum, original_file_name=f"{checksum}.jpg", created_at=when
        )
    )


async def test_without_a_mark_the_scan_reads_the_window(world):
    accounts = make_accounts()
    family_has(world, "beach", origin=ALICE)
    family_has(world, "old", origin=BOB)
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
    family_has(world, "beach", origin=ALICE)
    upload(world, ALICE, "beach", T1)

    late = await scan_assets(accounts, "Drop", mark=T1 + REUPLOAD_MARGIN / 2, now=NOW)
    past = await scan_assets(accounts, "Drop", mark=T1 + REUPLOAD_MARGIN * 2, now=NOW)

    assert [m.file_name for m in late.moves] == ["beach.jpg"]  # an upload that landed late
    assert not past.moves


async def test_a_mark_older_than_the_window_reads_only_the_window(world):
    accounts = make_accounts()
    family_has(world, "old", origin=BOB)
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
    family_has(world, "beach", origin=BOB)
    trip = upload(world, ALICE, "trip", T1)
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[trip])
    upload(world, BOB, "beach", T1)

    scan = await scan_assets(accounts, "Drop", now=NOW)

    assert [(m.file_name, m.reupload) for m in scan.moves] == [
        ("beach.jpg", True),
        ("trip.jpg", False),
    ]


async def test_a_family_copy_from_elsewhere_makes_no_claim_on_a_members_photo(world):
    accounts = make_accounts()
    # The family uploaded "fair" itself, and got "lake" from bob's move.
    family_has(world, "fair")
    family_has(world, "lake", origin=BOB)
    # Alice holds both files too. Neither is a re-upload of anything she gave.
    upload(world, ALICE, "fair", T1)
    upload(world, ALICE, "lake", T1)
    # Bob's phone re-uploads the one that came from him.
    bobs = upload(world, BOB, "lake", T1)

    scan = await scan_assets(accounts, "Drop", now=NOW)

    assert [(m.asset_id, m.owner.name) for m in scan.moves] == [(bobs.id, "bob")]
    assert sorted(scan.skipped_reuploads) == [
        "fair.jpg from alice: the family's copy did not come from them",
        "lake.jpg from alice: the family's copy did not come from them",
    ]


async def test_an_external_library_photo_is_never_a_reupload(world):
    accounts = make_accounts()
    family_has(world, "nas", origin=ALICE)
    nas = world.add_asset(
        FakeAsset(
            uuid4(),
            ALICE,
            checksum="nas",
            original_file_name="nas.jpg",
            created_at=T1,
            library_id=uuid4(),
        )
    )

    scan = await scan_assets(accounts, "Drop", now=NOW)
    one = await reuploads_among(
        accounts, [(accounts.by_user_id()[ALICE], world.find_asset(nas.id))]
    )

    assert not scan.moves and not scan.skipped_reuploads
    assert not one.moves and not one.skipped_reuploads
