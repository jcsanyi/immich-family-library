"""A fake Immich for the write paths: albums, users, assets, and a log of every write."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from immichpy.client.generated.models.album_user_role import AlbumUserRole
from immichpy.client.generated.models.asset_visibility import AssetVisibility
from immichpy.client.generated.models.bulk_id_error_reason import BulkIdErrorReason

from ifl.accounts import Account, Accounts
from ifl.immich import AlbumSummary
from ifl.sidecar import with_only_tag

FAMILY, ALICE, BOB, STRANGER = (uuid4() for _ in range(4))
EDITOR, VIEWER, OWNER = AlbumUserRole.EDITOR, AlbumUserRole.VIEWER, AlbumUserRole.OWNER


@dataclass
class FakeTag:
    value: str


@dataclass
class FakeAsset:
    id: UUID
    owner_id: UUID
    checksum: str = "c"
    original_file_name: str = "img.jpg"
    is_trashed: bool = False
    visibility: AssetVisibility = AssetVisibility.TIMELINE
    exif_info: object | None = field(default_factory=lambda: SimpleNamespace(description=""))
    updated_at: datetime = datetime(2026, 1, 1, tzinfo=UTC)
    file_created_at: datetime = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    file_modified_at: datetime = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    is_favorite: bool = False
    duration: str | None = "0:00:00.00000"
    live_photo_video_id: UUID | None = None
    stack: object | None = None
    tags: list[FakeTag] = field(default_factory=list)
    description: str = ""
    width: int | None = 640  # None until "extraction" has run
    height: int | None = 480
    created_at: datetime = datetime(2026, 1, 1, tzinfo=UTC)


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
        self.partners: dict[UUID, set[UUID]] = {}  # sharer -> users shared with
        # Per-asset state for the move path.
        self.library: dict[UUID, FakeAsset] = {}  # every asset, in an album or not
        self.sidecars: dict[UUID, bytes] = {}
        self.edits: dict[UUID, list] = {}
        self.metadata: dict[UUID, dict] = {}
        self.shared_links: dict[UUID, set[UUID]] = {}  # user -> asset ids in their links
        self.uploads: list[dict] = []
        self.queues: dict[str, int] = {}
        self.sidecar_writes: bool = True  # False: tagging never produces a sidecar
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

    def _may_add(self, client, album, asset) -> bool:
        """Immich's rules: editor or owner on the album, and owner or partner on the asset."""
        on_album = album.owner_id == client.user_id or album.roles.get(client.user_id) == EDITOR
        on_asset = asset.owner_id == client.user_id or client.user_id in self.partners.get(
            asset.owner_id, set()
        )
        return on_album and on_asset

    async def add_album_assets(self, client, album_id, asset_ids):
        ids = list(asset_ids)
        if not ids:
            return []
        self.writes.append(("add", client.user_id, album_id, set(ids)))
        out = []
        album = self.albums[album_id]
        present = {a.id for a in self.assets[album_id]}
        for i in ids:
            if i in self.fail_adds:
                out.append(BulkResult(i, False, self.fail_adds[i], "nope"))
            elif not self._may_add(client, album, self.find_asset(i)):
                out.append(BulkResult(i, False, BulkIdErrorReason.NO_PERMISSION, "no access"))
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

    async def list_partner_ids(self, client):
        return set(self.partners.get(client.user_id, set()))

    async def create_partner(self, client, user_id):
        assert user_id not in self.partners.get(client.user_id, set()), "already a partner"
        self.writes.append(("partner", client.user_id, user_id))
        self.partners.setdefault(client.user_id, set()).add(user_id)

    async def set_album_user_role(self, client, album_id, user_id, role):
        a = self.albums[album_id]
        assert user_id in a.user_ids
        self.writes.append(("role", client.user_id, album_id, user_id, role))
        self.albums[album_id] = replace(a, roles={**a.roles, user_id: role})

    def find_asset(self, asset_id):
        if asset_id in self.library:
            return self.library[asset_id]
        return next(a for lst in self.assets.values() for a in lst if a.id == asset_id)

    def add_asset(self, asset: FakeAsset) -> FakeAsset:
        self.library[asset.id] = asset
        return asset

    def _write_sidecar(self, asset: FakeAsset) -> None:
        """What Immich's sidecar job would write: the asset's tags (plus whatever
        edited fields; the fake only tracks description)."""
        xmp = with_only_tag(None, "placeholder")
        xmp = xmp.replace(
            b"<rdf:li>placeholder</rdf:li>",
            b"".join(f"<rdf:li>{t.value}</rdf:li>".encode() for t in asset.tags),
        )
        if asset.description:
            desc = f'<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:description><rdf:Alt><rdf:li xml:lang="x-default">{asset.description}</rdf:li></rdf:Alt></dc:description></rdf:Description>'
            xmp = xmp.replace(b"<rdf:Description", desc.encode() + b"<rdf:Description", 1)
        self.sidecars[asset.id] = xmp

    # Move-path reads.

    async def get_asset(self, client, asset_id):
        a = self.find_asset(asset_id)
        assert client.user_id == a.owner_id or any(
            client.user_id in al.user_ids
            for al, lst in zip(self.albums.values(), self.assets.values(), strict=True)
            if a in lst
        ), "asset not visible to this account"
        return a

    async def albums_containing(self, client, asset_id):
        return [
            al
            for al, lst in zip(self.albums.values(), self.assets.values(), strict=True)
            if any(x.id == asset_id for x in lst) and client.user_id in al.user_ids
        ]

    async def get_asset_edits(self, client, asset_id):
        assert client.user_id == self.find_asset(asset_id).owner_id
        return list(self.edits.get(asset_id, []))

    async def find_sidecar(self, client, asset_id):
        assert client.user_id == self.find_asset(asset_id).owner_id
        return asset_id if asset_id in self.sidecars else None

    async def download_asset_file(self, client, file_id):
        return self.sidecars[file_id]

    async def download_original(self, client, asset_id):
        self.writes.append(("download", client.user_id, asset_id))
        return b"bytes-of-" + self.find_asset(asset_id).checksum.encode()

    async def shared_link_asset_ids(self, client):
        return set(self.shared_links.get(client.user_id, set()))

    async def existing_asset_by_checksum(self, client, checksum):
        return (await self.owned_checksums(client, [checksum])).get(checksum)

    async def get_asset_metadata_value(self, client, asset_id, key):
        return self.metadata.get(asset_id, {}).get(key)

    async def restore_assets(self, client, asset_ids):
        for i in asset_ids:
            a = self.find_asset(i)
            assert client.user_id == a.owner_id
            a.is_trashed = False
            self.writes.append(("restore", client.user_id, i))

    async def queue_backlog(self, client):
        return dict(self.queues)

    async def iter_assets_created_after(self, client, since):
        for a in self.library.values():
            visible = a.owner_id == client.user_id or client.user_id in self.partners.get(
                a.owner_id, set()
            )
            if visible and (since is None or a.created_at > since):
                yield a

    async def owned_checksums(self, client, checksums):
        from ifl.immich import Owned

        out = {}
        for c in checksums:
            for a in self.library.values():
                if a.owner_id == client.user_id and a.checksum == c:
                    out[c] = Owned(a.id, a.is_trashed)
        return out

    # Move-path writes.

    async def upsert_tag(self, client, name):
        self._pending_tag_name = name
        return uuid4()

    async def tag_asset(self, client, tag_id, asset_id):
        a = self.find_asset(asset_id)
        assert client.user_id == a.owner_id
        self.writes.append(("tag", client.user_id, asset_id))
        a.tags.append(FakeTag(self._pending_tag_name))
        if self.sidecar_writes:
            self._write_sidecar(a)

    async def upload_asset(
        self,
        client,
        *,
        data,
        filename,
        checksum,
        file_created_at,
        file_modified_at,
        is_favorite,
        visibility,
        sidecar,
        duration=None,
    ):
        existing = await self.existing_asset_by_checksum(client, checksum)
        if existing:
            return existing.asset_id, False
        new = self.add_asset(
            FakeAsset(
                uuid4(),
                client.user_id,
                checksum,
                filename,
                visibility=visibility,
                is_favorite=is_favorite,
                file_created_at=file_created_at,
                file_modified_at=file_modified_at,
            )
        )
        self.uploads.append(
            {
                "id": new.id,
                "data": data,
                "sidecar": sidecar,
                "visibility": visibility,
                "is_favorite": is_favorite,
                "duration": duration,
            }
        )
        self.writes.append(("upload", client.user_id, new.id))
        return new.id, True

    async def set_asset_edits(self, client, asset_id, edits):
        assert client.user_id == self.find_asset(asset_id).owner_id
        self.edits[asset_id] = list(edits)

    async def set_asset_metadata(self, client, asset_id, key, value):
        assert client.user_id == self.find_asset(asset_id).owner_id
        self.metadata.setdefault(asset_id, {})[key] = value

    async def trash_assets(self, client, asset_ids):
        for i in asset_ids:
            a = self.find_asset(i)
            assert client.user_id == a.owner_id
            a.is_trashed = True
            self.writes.append(("trash", client.user_id, i))

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
    "list_partner_ids",
    "create_partner",
    "get_asset",
    "albums_containing",
    "get_asset_edits",
    "find_sidecar",
    "download_asset_file",
    "download_original",
    "shared_link_asset_ids",
    "existing_asset_by_checksum",
    "queue_backlog",
    "upsert_tag",
    "tag_asset",
    "upload_asset",
    "set_asset_edits",
    "set_asset_metadata",
    "trash_assets",
    "iter_assets_created_after",
    "owned_checksums",
    "get_asset_metadata_value",
    "restore_assets",
)


def patch_world(monkeypatch, *modules: str, world: FakeWorld | None = None) -> FakeWorld:
    """Point every immich helper that each module imported at one FakeWorld."""
    w = world or FakeWorld()
    for module in modules:
        mod = __import__(module, fromlist=["_"])
        for name in WRITE_HELPERS:
            if hasattr(mod, name):
                monkeypatch.setattr(f"{module}.{name}", getattr(w, name))
    return w
