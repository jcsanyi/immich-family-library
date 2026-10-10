"""Family album consistency against the fake Immich. No network."""

from __future__ import annotations

import pytest
from fakes import ALICE, BOB, EDITOR, FAMILY, OWNER, STRANGER, VIEWER, make_accounts, patch_world

from ifl.observer import describe_reconcile, scan_reconcile
from ifl.reconcile import reconcile


@pytest.fixture
def world(monkeypatch):
    w = patch_world(monkeypatch, "ifl.reconcile")
    monkeypatch.setattr("ifl.observer.list_albums", w.list_albums)
    return w


async def test_consistent_albums_need_nothing(world):
    accounts = make_accounts()
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    world.album("Trip", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR, STRANGER: VIEWER})
    world.album("Private", ALICE, roles={BOB: VIEWER})  # not family-owned, ignored
    scan = await scan_reconcile(accounts, "Drop")
    assert not scan
    assert describe_reconcile(scan, accounts) == ["family albums consistent"]


async def test_scan_finds_missing_dropbox_missing_members_and_viewers(world):
    accounts = make_accounts()
    handmade = world.album("Handmade", FAMILY)
    hikes = world.album("Hikes", FAMILY, roles={ALICE: EDITOR, BOB: VIEWER})
    scan = await scan_reconcile(accounts, "Drop")
    assert scan.create_dropbox == "Drop"
    by_album = {f.album.id: f for f in scan.fixes}
    assert by_album[handmade.id].add == {ALICE, BOB} and not by_album[handmade.id].promote
    assert by_album[hikes.id].promote == {BOB} and not by_album[hikes.id].add
    lines = describe_reconcile(scan, accounts)
    assert "CREATE dropbox 'Drop'" in lines
    assert "SHARE 'Handmade' with alice, bob" in lines
    assert "PROMOTE bob to editor on 'Hikes'" in lines


async def test_reconcile_applies_and_is_then_a_no_op(world):
    accounts = make_accounts()
    handmade = world.album("Handmade", FAMILY, roles={STRANGER: VIEWER})
    hikes = world.album("Hikes", FAMILY, roles={ALICE: EDITOR, BOB: VIEWER})

    result = await reconcile(accounts, await scan_reconcile(accounts, "Drop"))

    (drop,) = [a for a in world.albums.values() if a.name == "Drop"]
    assert result.created_dropbox.id == drop.id
    assert drop.roles == {FAMILY: OWNER, ALICE: EDITOR, BOB: EDITOR}
    assert world.albums[handmade.id].roles == {
        FAMILY: OWNER,
        STRANGER: VIEWER,  # never removed or changed
        ALICE: EDITOR,
        BOB: EDITOR,
    }
    assert world.albums[hikes.id].roles == {FAMILY: OWNER, ALICE: EDITOR, BOB: EDITOR}
    assert (result.shared, result.promoted) == (4, 1)
    assert all(w[1] == FAMILY for w in world.writes)  # everything under the family key

    world.writes.clear()
    again = await scan_reconcile(accounts, "Drop")
    assert not again
    assert await reconcile(accounts, again) == type(result)(None, 0, 0)
    assert not world.writes
