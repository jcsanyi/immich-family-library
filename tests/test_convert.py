"""Album conversion against a fake Immich that records writes. No network."""

from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import UUID, uuid4

import pytest
from immichpy.client.generated.models.album_user_role import AlbumUserRole
from immichpy.client.generated.models.bulk_id_error_reason import BulkIdErrorReason

from ifl.accounts import Account, Accounts
from ifl.convert import ConvertError, convert_album, wanted_roles
from ifl.immich import AlbumSummary
from ifl.observer import ConvertAlbum

FAMILY, ALICE, BOB, STRANGER = (uuid4() for _ in range(4))
EDITOR, VIEWER, OWNER = AlbumUserRole.EDITOR, AlbumUserRole.VIEWER, AlbumUserRole.OWNER


@dataclass
class FakeAsset:
    id: UUID
    owner_id: UUID


@dataclass
class BulkResult:
    id: UUID
    success: bool
    error: BulkIdErrorReason | None = None
    error_message: str | None = None


class FakeClient:
    def __init__(self, user_id):
        self.user_id = user_id


class FakeWorld:
    """Albums, their users and their assets, plus a log of every write."""

    def __init__(self):
        self.albums: dict[UUID, AlbumSummary] = {}
        self.assets: dict[UUID, list[FakeAsset]] = {}
        self.writes: list[tuple] = []
        self.fail_adds: dict[UUID, BulkIdErrorReason] = {}
        self.on_read: list = []  # callbacks run once each, in order, on asset reads

    def album(self, name, owner, roles=None, assets=(), description=""):
        aid = uuid4()
        roles = {owner: OWNER, **(roles or {})}
        self.albums[aid] = AlbumSummary(
            id=aid,
            name=name,
            owner_id=owner,
            user_ids=frozenset(roles),
            asset_count=len(assets),
            description=description,
            roles=roles,
        )
        self.assets[aid] = list(assets)
        return self.albums[aid]

    # The helpers convert.py imports, as this account would see them.

    async def list_albums(self, client):
        return [a for a in self.albums.values() if client.user_id in a.user_ids]

    async def iter_album_assets(self, client, album_id):
        if self.on_read:
            self.on_read.pop(0)()
        for a in list(self.assets[album_id]):
            yield a

    async def create_album(self, client, name, description=""):
        self.writes.append(("create", client.user_id, name))
        return self.album(name, client.user_id, description=description)

    async def add_album_users(self, client, album_id, roles):
        if not roles:
            return
        a = self.albums[album_id]
        assert not set(roles) & a.user_ids, "Immich rejects users already on the album"
        self.writes.append(("share", client.user_id, album_id, dict(roles)))
        merged = {**a.roles, **roles}
        self.albums[album_id] = replace(a, user_ids=frozenset(merged), roles=merged)

    async def add_album_assets(self, client, album_id, asset_ids):
        ids = list(asset_ids)
        if not ids:
            return []
        self.writes.append(("add", client.user_id, album_id, set(ids)))
        out = []
        present = {a.id for a in self.assets[album_id]}
        for i in ids:
            if i in self.fail_adds:
                out.append(BulkResult(i, False, self.fail_adds[i], "nope"))
            elif i in present:
                out.append(BulkResult(i, False, BulkIdErrorReason.DUPLICATE))
            else:
                self.assets[album_id].append(self.find_asset(i))
                out.append(BulkResult(i, True))
        return out

    async def delete_album(self, client, album_id):
        self.writes.append(("delete", client.user_id, album_id))
        del self.albums[album_id]
        del self.assets[album_id]

    def find_asset(self, asset_id):
        return next(a for lst in self.assets.values() for a in lst if a.id == asset_id)

    def family_albums_named(self, name):
        return [a for a in self.albums.values() if a.owner_id == FAMILY and a.name == name]


def make_accounts() -> Accounts:
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
    for name in (
        "list_albums",
        "iter_album_assets",
        "create_album",
        "add_album_users",
        "add_album_assets",
        "delete_album",
    ):
        monkeypatch.setattr(f"ifl.convert.{name}", getattr(w, name))
    return w


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
