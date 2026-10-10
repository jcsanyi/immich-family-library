"""A fake Immich for the write paths: albums, users, assets, and a log of every write."""

from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import UUID, uuid4

from immichpy.client.generated.models.album_user_role import AlbumUserRole
from immichpy.client.generated.models.bulk_id_error_reason import BulkIdErrorReason

from ifl.accounts import Account, Accounts
from ifl.immich import AlbumSummary

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

    async def set_album_user_role(self, client, album_id, user_id, role):
        a = self.albums[album_id]
        assert user_id in a.user_ids
        self.writes.append(("role", client.user_id, album_id, user_id, role))
        self.albums[album_id] = replace(a, roles={**a.roles, user_id: role})

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


WRITE_HELPERS = (
    "list_albums",
    "iter_album_assets",
    "create_album",
    "add_album_users",
    "add_album_assets",
    "delete_album",
    "set_album_user_role",
)


def patch_world(monkeypatch, module: str) -> FakeWorld:
    """Point every immich helper that `module` imported at one fresh FakeWorld."""
    w = FakeWorld()
    mod = __import__(module, fromlist=["_"])
    for name in WRITE_HELPERS:
        if hasattr(mod, name):
            monkeypatch.setattr(f"{module}.{name}", getattr(w, name))
    return w
