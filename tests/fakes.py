"""A fake Immich for the write paths: albums, users, assets, and a log of every write.

Assets and albums are real immichpy response models, built by `FakeAsset` and
`FakeWorld.album`, so the fake can't drift from the shapes the service reads:
every field is validated on construction and on assignment. Albums go through
`summarize_album`, the same way the service's own reads do.
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import aiohttp
from immichpy.client.generated.models.album_response_dto import AlbumResponseDto
from immichpy.client.generated.models.album_user_response_dto import AlbumUserResponseDto
from immichpy.client.generated.models.album_user_role import AlbumUserRole
from immichpy.client.generated.models.asset_response_dto import AssetResponseDto
from immichpy.client.generated.models.asset_type_enum import AssetTypeEnum
from immichpy.client.generated.models.asset_visibility import AssetVisibility
from immichpy.client.generated.models.bulk_id_error_reason import BulkIdErrorReason
from immichpy.client.generated.models.bulk_id_response_dto import BulkIdResponseDto
from immichpy.client.generated.models.exif_response_dto import ExifResponseDto
from immichpy.client.generated.models.tag_response_dto import TagResponseDto
from immichpy.client.generated.models.user_avatar_color import UserAvatarColor
from immichpy.client.generated.models.user_response_dto import UserResponseDto
from immichpy.client.generated.models.workflow_response_dto import WorkflowResponseDto
from immichpy.client.generated.models.workflow_step_dto import WorkflowStepDto
from immichpy.client.generated.models.workflow_trigger import WorkflowTrigger

from ifl.accounts import Account, Accounts
from ifl.immich import AlbumSummary, ImmichError, Owned, summarize_album, webhook_step

FAMILY, ALICE, BOB, STRANGER = (uuid4() for _ in range(4))
EDITOR, VIEWER, OWNER = AlbumUserRole.EDITOR, AlbumUserRole.VIEWER, AlbumUserRole.OWNER
EMAILS = {FAMILY: "family@x", ALICE: "alice@x", BOB: "bob@x", STRANGER: "stranger@x"}
T0 = datetime(2026, 1, 1, tzinfo=UTC)
TEMP_DIR = Path(tempfile.gettempdir()) / "ifl-tests"  # every move empties it again
TAKEN = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


def make_user(uid: UUID, email: str | None = None) -> UserResponseDto:
    email = email or EMAILS.get(uid, str(uid))
    return UserResponseDto(
        avatar_color=UserAvatarColor.PRIMARY,
        email=email,
        id=uid,
        name=email.split("@")[0],
        profile_changed_at=T0,
        profile_image_path="",
    )


def make_tag(value: str) -> TagResponseDto:
    return TagResponseDto(
        created_at=T0, id=uuid4(), name=value.rsplit("/", 1)[-1], updated_at=T0, value=value
    )


def make_workflow(
    name: str, steps: list[WorkflowStepDto], enabled: bool = True
) -> WorkflowResponseDto:
    return WorkflowResponseDto(
        created_at="",
        description=None,
        enabled=enabled,
        id=uuid4(),
        logging=False,
        name=name,
        steps=steps,
        trigger=WorkflowTrigger.ASSETCREATE,
        updated_at="",
    )


def FakeAsset(  # noqa: N802 - reads like the model it builds
    id: UUID,
    owner_id: UUID,
    checksum: str = "c",
    original_file_name: str = "img.jpg",
    *,
    description: str = "",
    **overrides,
) -> AssetResponseDto:
    """A real AssetResponseDto with the defaults of a settled, extracted still
    image. `description` lands in exif_info, where Immich keeps it. Any field
    can be overridden; `exif_info=None` means extraction hasn't run, and
    `width=None` means the copy has no dimensions yet."""
    fields = {
        "id": id,
        "owner_id": owner_id,
        "checksum": checksum,
        "original_file_name": original_file_name,
        "original_path": f"upload/{original_file_name}",
        "type": AssetTypeEnum.VIDEO if overrides.get("duration") else AssetTypeEnum.IMAGE,
        "created_at": T0,
        "updated_at": T0,
        "file_created_at": TAKEN,
        "file_modified_at": TAKEN,
        "local_date_time": TAKEN,
        "duration": None,  # milliseconds; None for a still image
        "width": 640,
        "height": 480,
        "has_metadata": True,
        "is_archived": False,
        "is_edited": False,
        "is_favorite": False,
        "is_offline": False,
        "is_trashed": False,
        "thumbhash": None,
        "visibility": AssetVisibility.TIMELINE,
        "exif_info": ExifResponseDto(description=description),
        "tags": [],
    }
    fields.update(overrides)
    return AssetResponseDto(**fields)


class FakeUsers:
    """The one generated-client call the observer makes directly: a user lookup
    for someone who isn't on the album, so their email can be shown."""

    async def get_user(self, id):
        if id in EMAILS:
            return make_user(id)
        raise RuntimeError("unknown user")


class FakeClient:
    users = FakeUsers()

    def __init__(self, user_id):
        self.user_id = user_id

    async def close(self):
        pass


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
        self.workflows: dict[UUID, list] = {}  # owner -> workflow response-like objects
        self.fail_adds: dict[UUID, BulkIdErrorReason] = {}

    def album(self, name, owner, roles=None, assets=(), description="", shared_link=False):
        """An album as Immich would return it, summarised the way the service does."""
        roles = {owner: OWNER, **(roles or {})}
        dto = AlbumResponseDto(
            id=uuid4(),
            album_name=name,
            album_thumbnail_asset_id=None,
            album_users=[AlbumUserResponseDto(role=r, user=make_user(u)) for u, r in roles.items()],
            asset_count=len(assets),
            created_at=T0,
            updated_at=T0,
            description=description,
            has_shared_link=shared_link,
            is_activity_enabled=False,
            shared=len(roles) > 1,
        )
        summary = summarize_album(dto)
        self.albums[summary.id] = summary
        self.assets[summary.id] = list(assets)
        return summary

    # The helpers convert.py imports, as this account would see them.

    async def list_albums(self, client):
        return [a for a in self.albums.values() if client.user_id in a.user_ids]

    async def iter_album_assets(self, client, album_id):
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
                out.append(
                    BulkIdResponseDto(
                        id=i, success=False, error=self.fail_adds[i], error_message="nope"
                    )
                )
            elif not self._may_add(client, album, self.find_asset(i)):
                out.append(
                    BulkIdResponseDto(
                        id=i,
                        success=False,
                        error=BulkIdErrorReason.NO_PERMISSION,
                        error_message="no access",
                    )
                )
            elif i in present:
                out.append(
                    BulkIdResponseDto(id=i, success=False, error=BulkIdErrorReason.DUPLICATE)
                )
            else:
                self.assets[album_id].append(self.find_asset(i))
                out.append(BulkIdResponseDto(id=i, success=True))
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

    async def find_workflow(self, client, name):
        return next((w for w in self.workflows.get(client.user_id, []) if w.name == name), None)

    async def create_workflow(self, client, name, url, token):
        w = make_workflow(name, [webhook_step(url, token)])
        self.workflows.setdefault(client.user_id, []).append(w)
        self.writes.append(("workflow", client.user_id, "create"))
        return w.id

    async def update_workflow(self, client, workflow_id, url, token):
        w = next(w for w in self.workflows[client.user_id] if w.id == workflow_id)
        w.steps, w.enabled = [webhook_step(url, token)], True
        self.writes.append(("workflow", client.user_id, "update"))

    async def set_album_user_role(self, client, album_id, user_id, role):
        a = self.albums[album_id]
        assert user_id in a.user_ids
        self.writes.append(("role", client.user_id, album_id, user_id, role))
        self.albums[album_id] = replace(a, roles={**a.roles, user_id: role})

    def find_asset(self, asset_id):
        if asset_id in self.library:
            return self.library[asset_id]
        return next(a for lst in self.assets.values() for a in lst if a.id == asset_id)

    def add_asset(self, asset: AssetResponseDto) -> AssetResponseDto:
        self.library[asset.id] = asset
        return asset

    def _write_sidecar(self, asset: AssetResponseDto) -> None:
        """What Immich's sidecar job would write: the asset's tags (plus whatever
        edited fields; the fake only tracks description)."""
        tags = "".join(f"<rdf:li>{t.value}</rdf:li>" for t in asset.tags or [])
        description = asset.exif_info.description if asset.exif_info else None
        desc = (
            '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:description><rdf:Alt><rdf:li xml:lang="x-default">{description}</rdf:li>'
            "</rdf:Alt></dc:description></rdf:Description>"
            if description
            else ""
        )
        xmp = (
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            f'{desc}<rdf:Description rdf:about="" xmlns:digiKam="http://www.digikam.org/ns/1.0/">'
            f"<digiKam:TagsList><rdf:Seq>{tags}</rdf:Seq></digiKam:TagsList>"
            "</rdf:Description></rdf:RDF></x:xmpmeta>"
        ).encode()
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

    async def get_asset_if_readable(self, client, asset_id):
        known = asset_id in self.library or any(
            a.id == asset_id for lst in self.assets.values() for a in lst
        )
        if not known:
            return None
        try:
            return await self.get_asset(client, asset_id)
        except AssertionError:
            return None

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

    async def download_original(self, client, asset_id, dest, *, checksum):
        self.writes.append(("download", client.user_id, asset_id))
        assert checksum == self.find_asset(asset_id).checksum
        dest.write_bytes(b"bytes-of-" + checksum.encode())

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
        a.tags = [*(a.tags or []), make_tag(self._pending_tag_name)]
        if self.sidecar_writes:
            self._write_sidecar(a)

    async def upload_asset(
        self,
        client,
        *,
        path,
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
                duration=duration,
            )
        )
        self.uploads.append(
            {
                "id": new.id,
                "data": path.read_bytes(),
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
    def acct(name, uid):
        return Account(name=name, email=f"{name}@x", user_id=uid, client=FakeClient(uid))

    return Accounts(
        family=acct("family", FAMILY),
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
    "get_asset_if_readable",
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
    "find_workflow",
    "create_workflow",
    "update_workflow",
)


def outage(call: str = "albums.get_all_albums") -> ImmichError:
    """What a helper raises when Immich cannot be reached: the guarded client
    turns whatever the call raised into this."""
    return ImmichError(call, aiohttp.ClientConnectionError("immich went away"))


def patch_world(monkeypatch, *modules: str, world: FakeWorld | None = None) -> FakeWorld:
    """Point every immich helper that each module imported at one FakeWorld."""
    w = world or FakeWorld()
    for module in modules:
        mod = __import__(module, fromlist=["_"])
        for name in WRITE_HELPERS:
            if hasattr(mod, name):
                monkeypatch.setattr(f"{module}.{name}", getattr(w, name))
    return w
