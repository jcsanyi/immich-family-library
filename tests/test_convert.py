"""Album conversion against a fake Immich that records writes. No network."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fakes import (
    ALICE,
    BOB,
    EDITOR,
    FAMILY,
    OWNER,
    STRANGER,
    VIEWER,
    FakeAsset,
    make_accounts,
    patch_world,
)
from immichpy.client.generated.models.bulk_id_error_reason import BulkIdErrorReason

from ifl.convert import ConvertError, convert_album, wanted_roles
from ifl.immich import AlbumSummary
from ifl.observer import ConvertAlbum


@pytest.fixture
def world(monkeypatch):
    return patch_world(monkeypatch, "ifl.convert")


def conversion(accounts, album) -> ConvertAlbum:
    owner = next(a for a in accounts.members.values() if a.user_id == album.owner_id)
    return ConvertAlbum(album=album, owner=owner, contributor_ids=frozenset())


async def test_convert_creates_family_album_and_deletes_original(world):
    accounts = make_accounts()
    a1, a2, b1 = FakeAsset(uuid4(), ALICE), FakeAsset(uuid4(), ALICE), FakeAsset(uuid4(), BOB)
    orig = world.album(
        "Beach",
        ALICE,
        roles={FAMILY: EDITOR, BOB: VIEWER, STRANGER: VIEWER},
        assets=[a1, a2, b1],
        description="sand",
    )

    result = await convert_album(accounts, conversion(accounts, orig))

    (fam,) = world.family_albums_named("Beach")
    assert result.created and result.family_album.id == fam.id
    assert fam.description == "sand"
    assert fam.roles == {FAMILY: OWNER, ALICE: EDITOR, BOB: EDITOR, STRANGER: VIEWER}
    assert {a.id for a in world.assets[fam.id]} == {a1.id, a2.id, b1.id}
    assert orig.id not in world.albums
    assert result.assets_added == 3 and result.assets_already_there == 0

    # Each owner adds their own, with their own key; alice deletes her own album.
    adds = {(w[1], frozenset(w[3])) for w in world.writes if w[0] == "add"}
    assert adds == {(ALICE, frozenset({a1.id, a2.id})), (BOB, frozenset({b1.id}))}
    assert ("delete", ALICE, orig.id) in world.writes
    assert world.writes[-1][0] == "delete"


async def test_convert_reuses_family_album_and_skips_what_is_there(world):
    accounts = make_accounts()
    a1, a2 = FakeAsset(uuid4(), ALICE), FakeAsset(uuid4(), ALICE)
    fam = world.album("Beach", FAMILY, roles={ALICE: EDITOR}, assets=[a1])
    orig = world.album("Beach", ALICE, roles={FAMILY: EDITOR}, assets=[a1, a2])

    result = await convert_album(accounts, conversion(accounts, orig))

    assert not result.created and result.family_album.id == fam.id
    assert not any(w[0] == "create" for w in world.writes)
    assert result.users_added == {BOB: EDITOR}
    assert result.assets_added == 1 and result.assets_already_there == 1
    assert {a.id for a in world.assets[fam.id]} == {a1.id, a2.id}
    assert orig.id not in world.albums


async def test_failed_add_leaves_original_in_place(world):
    accounts = make_accounts()
    a1, b1 = FakeAsset(uuid4(), ALICE), FakeAsset(uuid4(), BOB)
    orig = world.album("Beach", ALICE, roles={FAMILY: EDITOR}, assets=[a1, b1])
    world.fail_adds[b1.id] = BulkIdErrorReason.NO_PERMISSION

    with pytest.raises(ConvertError, match="bob could not add 1 of 1"):
        await convert_album(accounts, conversion(accounts, orig))

    assert orig.id in world.albums
    assert not any(w[0] == "delete" for w in world.writes)
    # Running again after the problem is fixed finishes the job.
    world.fail_adds.clear()
    await convert_album(accounts, conversion(accounts, orig))
    assert orig.id not in world.albums
    assert len(world.family_albums_named("Beach")) == 1


async def test_asset_added_during_conversion_is_picked_up(world):
    accounts = make_accounts()
    a1, late = FakeAsset(uuid4(), ALICE), FakeAsset(uuid4(), BOB)
    orig = world.album("Beach", ALICE, roles={FAMILY: EDITOR}, assets=[a1])
    # Appears after the first read of the original.
    world.on_read = [lambda: None, lambda: world.assets[orig.id].append(late)]

    result = await convert_album(accounts, conversion(accounts, orig))

    (fam,) = world.family_albums_named("Beach")
    assert {a.id for a in world.assets[fam.id]} == {a1.id, late.id}
    assert result.assets_added == 2


async def test_two_family_albums_with_the_name_is_an_error(world):
    accounts = make_accounts()
    world.album("Beach", FAMILY)
    world.album("Beach", FAMILY)
    orig = world.album("Beach", ALICE, roles={FAMILY: EDITOR})
    with pytest.raises(ConvertError, match="2 albums named 'Beach'"):
        await convert_album(accounts, conversion(accounts, orig))
    assert not world.writes


def test_wanted_roles_carries_others_and_makes_members_editors():
    accounts = make_accounts()
    roles = {ALICE: OWNER, FAMILY: VIEWER, BOB: VIEWER, STRANGER: EDITOR}
    original = AlbumSummary(uuid4(), "x", ALICE, frozenset(roles), 0, roles=roles)
    assert wanted_roles(accounts, original) == {ALICE: EDITOR, BOB: EDITOR, STRANGER: EDITOR}


async def test_convert_albums_honours_the_limit_and_continues_past_failures(world):
    from ifl.convert import convert_albums

    accounts = make_accounts()
    albums = []
    for i in range(4):
        asset = FakeAsset(uuid4(), ALICE)
        albums.append(world.album(f"A{i}", ALICE, roles={FAMILY: EDITOR}, assets=[asset]))
    world.fail_adds[world.assets[albums[1].id][0].id] = BulkIdErrorReason.UNKNOWN
    convs = [conversion(accounts, a) for a in albums]

    result = await convert_albums(accounts, convs, limit=3)

    assert [r.family_album.name for r in result.done] == ["A0", "A2"]
    assert [c.album.name for c, _ in result.failed] == ["A1"]
    assert result.remaining == 1
    assert albums[1].id in world.albums and albums[3].id in world.albums
    assert albums[0].id not in world.albums and albums[2].id not in world.albums

    unlimited = await convert_albums(accounts, convs[3:], limit=0)
    assert len(unlimited.done) == 1 and unlimited.remaining == 0
