"""Observer logic against fake clients. No network."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fakes import ALICE, BOB, EDITOR, FAMILY, STRANGER, FakeAsset, make_accounts, patch_world
from immichpy.client.generated.models.asset_visibility import AssetVisibility

from ifl.observer import (
    describe_albums,
    describe_reconcile,
    scan_albums,
    scan_assets,
    scan_reconcile,
)


@pytest.fixture
def world(monkeypatch):
    w = patch_world(monkeypatch, "ifl.observer")
    w.partners[FAMILY] = {ALICE, BOB}
    return w


async def test_member_asset_in_family_album_is_a_move(world):
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    fam = world.album(
        "Trip",
        FAMILY,
        roles={ALICE: EDITOR},
        assets=[
            FakeAsset(uuid4(), ALICE, "aa", "a.jpg"),
            FakeAsset(uuid4(), FAMILY, "ff", "f.jpg"),
        ],
    )
    scan = await scan_assets(make_accounts(), "Drop")
    assert [m.file_name for m in scan.moves] == ["a.jpg"]
    assert scan.moves[0].owner.name == "alice"
    assert scan.moves[0].album.id == fam.id
    assert scan.dropbox and scan.dropbox.name == "Drop"
    assert not scan.blocked


async def test_asset_in_two_family_albums_is_one_move(world):
    world.album("Drop", FAMILY)
    a = FakeAsset(uuid4(), ALICE)
    world.album("One", FAMILY, assets=[a])
    world.album("Two", FAMILY, assets=[a])
    scan = await scan_assets(make_accounts(), "Drop")
    assert len(scan.moves) == 1


async def test_non_participant_asset_is_blocked(world):
    world.album("Drop", FAMILY)
    world.album("Trip", FAMILY, assets=[FakeAsset(uuid4(), STRANGER, "ss", "s.jpg")])
    scan = await scan_assets(make_accounts(), "Drop")
    assert not scan.moves
    assert len(scan.blocked) == 1 and "not a participant" in scan.blocked[0].reason
    assert "owner stranger@x" in scan.blocked[0].reason


async def test_live_and_hidden_photos_are_blocked_in_the_scan(world):
    world.album("Drop", FAMILY)
    world.album(
        "Trip",
        FAMILY,
        roles={ALICE: EDITOR},
        assets=[
            FakeAsset(uuid4(), ALICE, "l", "live.jpg", live_photo_video_id=str(uuid4())),
            FakeAsset(uuid4(), ALICE, "h", "hidden.jpg", visibility=AssetVisibility.HIDDEN),
            FakeAsset(uuid4(), ALICE, "a", "archived.jpg", visibility=AssetVisibility.ARCHIVE),
        ],
    )
    scan = await scan_assets(make_accounts(), "Drop")
    assert [m.file_name for m in scan.moves] == ["archived.jpg"]
    assert [(b.what, b.reason) for b in scan.blocked] == [
        ("photo live.jpg from alice, in 'Trip'", "live photo; not handled yet"),
        ("photo hidden.jpg from alice, in 'Trip'", "visibility is hidden"),
    ]


async def test_member_album_shared_with_family_is_a_conversion(world):
    world.album("Drop", FAMILY)
    world.album("Private", ALICE, roles={BOB: EDITOR}, assets=[FakeAsset(uuid4(), ALICE)])
    shared = world.album(
        "Shared",
        ALICE,
        roles={FAMILY: EDITOR, BOB: EDITOR},
        assets=[
            FakeAsset(uuid4(), ALICE),
            FakeAsset(uuid4(), BOB),
        ],
    )
    scan = await scan_albums(make_accounts())
    assert [c.album.id for c in scan.conversions] == [shared.id]
    assert scan.conversions[0].owner.name == "alice"
    assert describe_albums(scan)[0].startswith("CONVERT album 'Shared'")


async def test_shared_album_with_a_public_link_is_blocked(world):
    world.album("Drop", FAMILY)
    linked = world.album("Linked", ALICE, roles={FAMILY: EDITOR}, shared_link=True)
    scan = await scan_albums(make_accounts())
    assert not scan.conversions
    assert scan.blocked[0].album_id == linked.id and "shared link" in scan.blocked[0].reason


async def test_shared_album_with_outsider_assets_is_blocked(world):
    world.album("Drop", FAMILY)
    world.album("Shared", ALICE, roles={FAMILY: EDITOR}, assets=[FakeAsset(uuid4(), STRANGER)])
    scan = await scan_albums(make_accounts())
    assert not scan.conversions
    assert "non-participants: stranger@x" in scan.blocked[0].reason


async def test_missing_dropbox_is_a_reconcile_action(world):
    accounts = make_accounts()
    scan = await scan_assets(accounts, "Drop")
    assert scan.dropbox is None and not scan.blocked
    fixes = await scan_reconcile(accounts, "Drop")
    assert fixes.create_dropbox == "Drop"
    assert describe_reconcile(fixes, accounts) == ["CREATE dropbox 'Drop'"]
