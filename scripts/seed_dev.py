"""Seed the dev instance with sample photos and albums for testing.

Generates small JPEGs with distinct EXIF (date, GPS, description), uploads them
as the test members, and sets up the album situations the observer and the
move path need to see:

  - family-owned "Family Dropbox", shared with all members as editors
  - family-owned "Family Trip", shared with all members, with one of alice's
    photos added by alice (a move)
  - alice-owned "Beach 2025" shared with the family user (a conversion)
  - alice-owned "Alice & Bob" shared with bob only (left alone)
  - bob's photos uploaded, in no album

Acts as the users, not as the service: it logs in with each account's email
from config.toml and the shared dev password, because sharing an album is a
thing a member does in the UI and the service keys deliberately can't. The
password comes from the IFL_DEV_PASSWORD environment variable.

Deterministic: the same photos get the same bytes every run, so Immich
deduplicates re-uploads and albums are found by name. `--reset` first trashes
every asset and deletes every album for all configured accounts, then empties
the trash. Dev instance only.

    IFL_DEV_PASSWORD=... uv run --group seed python scripts/seed_dev.py [--reset] [-c config.toml]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import piexif
from immichpy.client.generated.models.add_users_dto import AddUsersDto
from immichpy.client.generated.models.album_user_add_dto import AlbumUserAddDto
from immichpy.client.generated.models.album_user_role import AlbumUserRole
from immichpy.client.generated.models.asset_bulk_delete_dto import AssetBulkDeleteDto
from immichpy.client.generated.models.bulk_ids_dto import BulkIdsDto
from immichpy.client.generated.models.create_album_dto import CreateAlbumDto
from immichpy.client.generated.models.login_credential_dto import LoginCredentialDto
from immichpy.client.generated.models.metadata_search_dto import MetadataSearchDto
from immichpy.client.main import AsyncClient
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ifl.accounts import Account, Accounts  # noqa: E402
from ifl.config import AccountConfig, load_config  # noqa: E402
from ifl.immich import list_albums  # noqa: E402

log = logging.getLogger("seed")

# Fixed so re-runs produce identical bytes.
BASE = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)

# (owner, filename, days-before-BASE, lat, lon, description)
PHOTOS = [
    ("alice", "alice-beach-1.jpg", 120, 44.6488, -63.5752, "Beach day, low tide"),
    ("alice", "alice-beach-2.jpg", 119, 44.6490, -63.5760, "Beach day, high tide"),
    ("alice", "alice-trip-1.jpg", 60, 46.2382, -63.1311, "Ferry crossing"),
    ("alice", "alice-garden.jpg", 10, 44.6500, -63.5800, "Tomatoes finally"),
    ("bob", "bob-hike-1.jpg", 90, 45.3080, -65.0000, "Fundy trail"),
    ("bob", "bob-hike-2.jpg", 89, 45.3100, -65.0100, ""),
    ("bob", "bob-kitchen.jpg", 5, 44.6500, -63.5800, "Bread attempt #4"),
    ("carol", "carol-cat.jpg", 30, 44.6500, -63.5800, "Cat"),
]


def _dms(deg: float) -> tuple[tuple[int, int], ...]:
    d = abs(deg)
    m, s = divmod(d * 3600, 60)
    deg_i, m = divmod(m, 60)
    return ((int(deg_i), 1), (int(m), 1), (int(s * 100), 100))


def make_jpeg(name: str, taken: datetime, lat: float, lon: float, desc: str) -> bytes:
    seed = int(hashlib.sha1(name.encode()).hexdigest()[:6], 16)
    img = Image.new("RGB", (640, 480), ((seed >> 16) & 255, (seed >> 8) & 255, seed & 255))
    d = ImageDraw.Draw(img)
    d.rectangle([40, 40, 600, 440], outline=(255, 255, 255), width=6)
    d.text((60, 60), name, fill=(255, 255, 255))
    d.text((60, 90), taken.strftime("%Y-%m-%d %H:%M"), fill=(255, 255, 255))
    stamp = taken.strftime("%Y:%m:%d %H:%M:%S").encode()
    exif = {
        "0th": {
            piexif.ImageIFD.Make: b"Seed",
            piexif.ImageIFD.Model: b"seed_dev.py",
            piexif.ImageIFD.ImageDescription: desc.encode(),
        },
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: stamp,
            piexif.ExifIFD.DateTimeDigitized: stamp,
            piexif.ExifIFD.OffsetTimeOriginal: b"-03:00",
        },
        "GPS": {
            piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S",
            piexif.GPSIFD.GPSLatitude: _dms(lat),
            piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
            piexif.GPSIFD.GPSLongitude: _dms(lon),
        },
    }
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85, exif=piexif.dump(exif))
    return buf.getvalue()


async def upload(acct: Account, name: str, data: bytes, taken: datetime) -> UUID:
    res = await acct.client.assets.upload_asset(
        asset_data=(name, data),
        file_created_at=taken,
        file_modified_at=taken,
        filename=name,
    )
    log.info("%s: %s -> %s %s", acct.name, name, res.status.value, res.id)
    return res.id


async def ensure_album(acct: Account, name: str, *, share: dict[UUID, AlbumUserRole]) -> UUID:
    for a in await list_albums(acct.client):
        if a.owner_id == acct.user_id and a.name == name:
            log.info("%s: album %r exists", acct.name, name)
            album_id = a.id
            break
    else:
        created = await acct.client.albums.create_album(CreateAlbumDto(album_name=name))
        album_id = created.id
        log.info("%s: created album %r", acct.name, name)
    info = await acct.client.albums.get_album_info(id=album_id)
    present = {au.user.id for au in info.album_users or []}
    to_add = [
        AlbumUserAddDto(user_id=uid, role=role) for uid, role in share.items() if uid not in present
    ]
    if to_add:
        await acct.client.albums.add_users_to_album(
            id=album_id, add_users_dto=AddUsersDto(album_users=to_add)
        )
        log.info("%s: shared %r with %d user(s)", acct.name, name, len(to_add))
    return album_id


async def add_to_album(acct: Account, album_id: UUID, asset_ids: list[UUID]) -> None:
    await acct.client.albums.add_assets_to_album(
        id=album_id, bulk_ids_dto=BulkIdsDto(ids=asset_ids)
    )


async def login(url: str, acct: AccountConfig, password: str, *, is_family: bool) -> Account:
    anon = AsyncClient(base_url=url)
    try:
        res = await anon.auth.login(LoginCredentialDto(email=acct.email, password=password))
    finally:
        await anon.close()
    client = AsyncClient(base_url=url, access_token=res.access_token)
    return Account(
        name=acct.name, email=acct.email, user_id=res.user_id, client=client, is_family=is_family
    )


async def login_all(config_path: Path, password: str) -> tuple[Accounts, str]:
    cfg = load_config(config_path)
    family = await login(cfg.immich_url, cfg.family, password, is_family=True)
    members = {
        n: await login(cfg.immich_url, m, password, is_family=False) for n, m in cfg.members.items()
    }
    return Accounts(family=family, members=members), cfg.dropbox_album


async def reset(accounts: Accounts) -> None:
    """Trash every asset and delete every owned album for all accounts, then empty trash."""
    for acct in [accounts.family, *accounts.members.values()]:
        for a in await list_albums(acct.client):
            if a.owner_id == acct.user_id:
                await acct.client.albums.delete_album(id=a.id)
        ids = []
        page = 1
        while True:
            res = await acct.client.search.search_assets(
                MetadataSearchDto(page=page, size=500, with_deleted=True)
            )
            ids += [x.id for x in res.assets.items]
            if not res.assets.next_page:
                break
            page = int(res.assets.next_page)
        if ids:
            await acct.client.assets.delete_assets(AssetBulkDeleteDto(ids=ids, force=True))
        await acct.client.trash.empty_trash()
        log.info("%s: reset (%d assets removed)", acct.name, len(ids))


async def main(config_path: Path, do_reset: bool) -> int:
    password = os.environ.get("IFL_DEV_PASSWORD")
    if not password:
        log.error("set IFL_DEV_PASSWORD (DEV_PASSWORD from accounts.env on the dev server)")
        return 2
    accounts, dropbox_album = await login_all(config_path, password)
    try:
        if do_reset:
            await reset(accounts)
        fam, mem = accounts.family, accounts.members
        ids: dict[str, UUID] = {}
        for owner, name, days, lat, lon, desc in PHOTOS:
            taken = BASE - timedelta(days=days, hours=3)
            ids[name] = await upload(
                mem[owner], name, make_jpeg(name, taken, lat, lon, desc), taken
            )

        editors = {m.user_id: AlbumUserRole.EDITOR for m in mem.values()}
        await ensure_album(fam, dropbox_album, share=editors)
        trip = await ensure_album(fam, "Family Trip", share=editors)
        await add_to_album(mem["alice"], trip, [ids["alice-trip-1.jpg"]])

        beach = await ensure_album(
            mem["alice"], "Beach 2025", share={fam.user_id: AlbumUserRole.EDITOR}
        )
        await add_to_album(
            mem["alice"], beach, [ids["alice-beach-1.jpg"], ids["alice-beach-2.jpg"]]
        )

        ab = await ensure_album(
            mem["alice"], "Alice & Bob", share={mem["bob"].user_id: AlbumUserRole.EDITOR}
        )
        await add_to_album(mem["alice"], ab, [ids["alice-garden.jpg"]])
        await add_to_album(mem["bob"], ab, [ids["bob-kitchen.jpg"]])
        log.info("done")
        return 0
    finally:
        await accounts.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("-c", "--config", type=Path, default=Path("config.toml"))
    p.add_argument(
        "--reset", action="store_true", help="wipe all accounts' assets and albums first"
    )
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.config, args.reset)))
