"""The re-upload source of the asset scan. No network."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fakes import ALICE, BOB, EDITOR, FAMILY, FakeAsset, make_accounts, patch_world

from ifl.ledger import Ledger
from ifl.observer import describe_assets, scan_assets

T0 = datetime(2026, 10, 1, tzinfo=UTC)
T1 = datetime(2026, 10, 9, tzinfo=UTC)
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def world(monkeypatch):
    w = patch_world(monkeypatch, "ifl.observer")
    w.partners[FAMILY] = {ALICE, BOB}
    return w


@pytest.fixture
def ledger(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    yield led
    led.close()


async def test_reuploads_are_found_and_the_mark_is_only_read(world, ledger):
    accounts = make_accounts()
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    # The family owns "beach" (moved earlier) and "old" (uploaded directly).
    world.add_asset(FakeAsset(uuid4(), FAMILY, checksum="beach", original_file_name="beach.jpg"))
    world.add_asset(FakeAsset(uuid4(), FAMILY, checksum="old", original_file_name="old.jpg"))
    # Alice's phone re-uploads beach after the purge; she also uploads something new.
    re = world.add_asset(
        FakeAsset(uuid4(), ALICE, checksum="beach", original_file_name="beach.jpg", created_at=T1)
    )
    world.add_asset(
        FakeAsset(uuid4(), ALICE, checksum="fresh", original_file_name="fresh.jpg", created_at=T1)
    )
    # Bob re-uploaded "old" long ago, before his mark: not seen this pass.
    world.add_asset(
        FakeAsset(uuid4(), BOB, checksum="old", original_file_name="old.jpg", created_at=T0)
    )
    ledger.set_mark(BOB, T1)

    scan = await scan_assets(accounts, "Drop", ledger=ledger, now=NOW)

    assert [(m.file_name, m.owner.name, m.reupload, m.created_at) for m in scan.moves] == [
        ("beach.jpg", "alice", True, T1)
    ]
    assert scan.moves[0].asset_id == re.id and scan.moves[0].album is None
    assert scan.scanned_at == NOW and scan.reuploads_by_owner() == {"alice": 1}
    assert describe_assets(scan) == ["REUPLOAD beach.jpg from alice: family already has it"]
    # The scan never advances a mark.
    assert ledger.get_mark(ALICE) is None and ledger.get_mark(BOB) == T1


async def test_a_trashed_family_copy_is_skipped_and_a_family_album_asset_is_not_doubled(
    world, ledger
):
    accounts = make_accounts()
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    world.add_asset(
        FakeAsset(uuid4(), FAMILY, checksum="cat", original_file_name="cat.jpg", is_trashed=True)
    )
    world.add_asset(
        FakeAsset(uuid4(), ALICE, checksum="cat", original_file_name="cat.jpg", created_at=T1)
    )
    # An asset already found in a family album is a plain move, not also a re-upload.
    world.add_asset(FakeAsset(uuid4(), FAMILY, checksum="trip", original_file_name="trip.jpg"))
    trip = world.add_asset(
        FakeAsset(uuid4(), ALICE, checksum="trip", original_file_name="trip.jpg", created_at=T1)
    )
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[trip])

    scan = await scan_assets(accounts, "Drop", ledger=ledger, now=NOW)

    assert [(m.file_name, m.reupload) for m in scan.moves] == [("trip.jpg", False)]
    assert scan.skipped_reuploads == ["cat.jpg from alice: the family's copy is in the trash"]
    assert (
        "SKIP re-upload cat.jpg from alice: the family's copy is in the trash"
        in describe_assets(scan)
    )


async def test_without_a_ledger_the_scan_does_not_look_for_reuploads(world):
    accounts = make_accounts()
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    world.add_asset(FakeAsset(uuid4(), FAMILY, checksum="beach"))
    world.add_asset(FakeAsset(uuid4(), ALICE, checksum="beach", created_at=T1))
    scan = await scan_assets(accounts, "Drop")
    assert not scan.moves and scan.scanned_at is None
