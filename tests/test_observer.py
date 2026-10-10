"""Observer logic against fake clients. No network."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from ifl.accounts import Account, Accounts
from ifl.immich import AlbumSummary
from ifl.observer import (
    describe_albums,
    describe_reconcile,
    scan_albums,
    scan_assets,
    scan_reconcile,
)

FAMILY, ALICE, BOB, STRANGER = (uuid4() for _ in range(4))
EMAILS = {FAMILY: "family@x", ALICE: "alice@x", BOB: "bob@x", STRANGER: "stranger@x"}


@dataclass
class FakeAsset:
    id: UUID
    owner_id: UUID
    checksum: str = "c"
    original_file_name: str = "img.jpg"
    is_trashed: bool = False


class FakeUsers:
    async def get_user(self, id):
        if id == STRANGER:
            return type("U", (), {"email": "stranger@x"})()
        raise RuntimeError("unknown user")


class FakeClient:
    users = FakeUsers()

    def __init__(self, user_id):
        self.user_id = user_id


class FakeWorld:
    """Albums and their assets, as each account would see them."""

    def __init__(self):
        self.albums: dict[UUID, AlbumSummary] = {}
        self.assets: dict[UUID, list[FakeAsset]] = {}

    def album(self, name, owner, users=(), assets=()):
        aid = uuid4()
        members = {owner, *users}
        self.albums[aid] = AlbumSummary(
            id=aid,
            name=name,
            owner_id=owner,
            user_ids=frozenset(members),
            asset_count=len(assets),
            emails={u: EMAILS.get(u, str(u)) for u in members},
        )
        self.assets[aid] = list(assets)
        return self.albums[aid]

    def visible_to(self, user_id):
        return [a for a in self.albums.values() if user_id in a.user_ids]


def make_accounts(world: FakeWorld) -> Accounts:
    def acct(name, uid, fam=False):
        return Account(
            name=name, email=f"{name}@x", user_id=uid, client=FakeClient(uid), is_family=fam
        )

    return Accounts(
        family=acct("family", FAMILY, True),
        members={"alice": acct("alice", ALICE), "bob": acct("bob", BOB)},
    )


@pytest.fixture
def world(monkeypatch):
    w = FakeWorld()

    async def list_albums(client):
        return w.visible_to(client.user_id)

    async def iter_album_assets(client, album_id):
        for a in w.assets[album_id]:
            yield a

    monkeypatch.setattr("ifl.observer.list_albums", list_albums)
    monkeypatch.setattr("ifl.observer.iter_album_assets", iter_album_assets)
    return w


async def test_member_asset_in_family_album_is_a_move(world):
    world.album("Drop", FAMILY, users={ALICE, BOB})
    fam = world.album(
        "Trip",
        FAMILY,
        users={ALICE},
        assets=[
            FakeAsset(uuid4(), ALICE, "aa", "a.jpg"),
            FakeAsset(uuid4(), FAMILY, "ff", "f.jpg"),
        ],
    )
    scan = await scan_assets(make_accounts(world), "Drop")
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
    scan = await scan_assets(make_accounts(world), "Drop")
    assert len(scan.moves) == 1


async def test_non_participant_asset_is_blocked(world):
    world.album("Drop", FAMILY)
    world.album("Trip", FAMILY, assets=[FakeAsset(uuid4(), STRANGER, "ss", "s.jpg")])
    scan = await scan_assets(make_accounts(world), "Drop")
    assert not scan.moves
    assert len(scan.blocked) == 1 and "not a participant" in scan.blocked[0].reason
    assert "owner stranger@x" in scan.blocked[0].reason


async def test_member_album_shared_with_family_is_a_conversion(world):
    world.album("Drop", FAMILY)
    world.album("Private", ALICE, users={BOB}, assets=[FakeAsset(uuid4(), ALICE)])
    shared = world.album(
        "Shared",
        ALICE,
        users={FAMILY, BOB},
        assets=[
            FakeAsset(uuid4(), ALICE),
            FakeAsset(uuid4(), BOB),
        ],
    )
    scan = await scan_albums(make_accounts(world))
    assert [c.album.id for c in scan.conversions] == [shared.id]
    assert scan.conversions[0].contributor_ids == {ALICE, BOB}
    assert describe_albums(scan)[0].startswith("CONVERT album 'Shared'")


async def test_shared_album_with_outsider_assets_is_blocked(world):
    world.album("Drop", FAMILY)
    world.album("Shared", ALICE, users={FAMILY}, assets=[FakeAsset(uuid4(), STRANGER)])
    scan = await scan_albums(make_accounts(world))
    assert not scan.conversions
    assert "non-participants: stranger@x" in scan.blocked[0].reason


async def test_missing_dropbox_is_a_reconcile_action(world):
    accounts = make_accounts(world)
    scan = await scan_assets(accounts, "Drop")
    assert scan.dropbox is None and not scan.blocked
    fixes = await scan_reconcile(accounts, "Drop")
    assert fixes.create_dropbox == "Drop"
    assert describe_reconcile(fixes, accounts) == ["CREATE dropbox 'Drop'"]
