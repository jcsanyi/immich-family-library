"""Set up test situations on the dev instance, acting as the users.

Each subcommand arranges one situation the service should then handle, the way
a member or the family would arrange it in the UI. They were used to verify
stages 2 to 7 and are kept so the same checks can be re-run after a change.
Most assume the seed has run (`scripts/seed_dev.py`) and refer to its photos by
file name. Like the seed, they log in with the shared dev password:

    IFL_DEV_PASSWORD=... uv run --group seed python scripts/dev_scenarios.py <subcommand>

Subcommands:

  keys            Bring the member API keys up to MEMBER_PERMISSIONS and make sure an
                  admin key with ADMIN_PERMISSIONS exists (IFL_ADMIN_EMAIL; the new
                  secret is written to --admin-secret-out, never printed). A key can't
                  change its own permissions, so this uses the password sessions.
  convert-cases   alice's "Garden" with a description and the admin as viewer (carry-over
                  on conversion); bob's "Family Trip" with alice's trip photo (merge into
                  the family album of the same name).
  more-albums     bob's "Hikes" and "Kitchen", carol's "Cat", shared with the family, for
                  `convert-album --all` and the album limit.
  break-reconcile Delete the (empty) dropbox, create an unshared family album, demote bob
                  to viewer on "Hikes": three things `reconcile-albums` must fix.
  bobs-picks      bob's album with alice as editor holding alice-garden.jpg: the move
                  re-links it with bob's key (album owner first).
  edit            Give alice-beach-1.jpg every kind of edit the move carries: description,
                  rating, favourite, GPS, date, a crop, a rotate, a tag.
  reupload        Empty the named members' trash and re-upload their seed photos with no
                  album, as a phone does after the purge. --family-trash FILE first trashes
                  the family's copy of FILE, for the "family deleted it" case.
  reshare         As carol, add her re-uploaded cat to the family's "Cat" album: the
                  re-share that restores the family's trashed copy.

Dev instance only. Nothing here is safe against a real library.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from immichpy.client.generated.models.album_user_role import AlbumUserRole
from immichpy.client.generated.models.api_key_create_dto import ApiKeyCreateDto
from immichpy.client.generated.models.api_key_update_dto import ApiKeyUpdateDto
from immichpy.client.generated.models.asset_bulk_delete_dto import AssetBulkDeleteDto
from immichpy.client.generated.models.asset_edit_action import AssetEditAction
from immichpy.client.generated.models.asset_edit_action_item_dto import AssetEditActionItemDto
from immichpy.client.generated.models.asset_edit_action_item_dto_parameters import (
    AssetEditActionItemDtoParameters,
)
from immichpy.client.generated.models.asset_edits_create_dto import AssetEditsCreateDto
from immichpy.client.generated.models.bulk_ids_dto import BulkIdsDto
from immichpy.client.generated.models.create_album_dto import CreateAlbumDto
from immichpy.client.generated.models.crop_parameters import CropParameters
from immichpy.client.generated.models.login_credential_dto import LoginCredentialDto
from immichpy.client.generated.models.metadata_search_dto import MetadataSearchDto
from immichpy.client.generated.models.permission import Permission
from immichpy.client.generated.models.rotate_parameters import RotateParameters
from immichpy.client.generated.models.tag_upsert_dto import TagUpsertDto
from immichpy.client.generated.models.update_album_user_dto import UpdateAlbumUserDto
from immichpy.client.generated.models.update_asset_dto import UpdateAssetDto
from immichpy.client.main import AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seed_dev import BASE, PHOTOS, add_to_album, ensure_album, login_all, make_jpeg, upload  # noqa: E402

from ifl.accounts import ADMIN_PERMISSIONS, MEMBER_PERMISSIONS, Account, Accounts  # noqa: E402
from ifl.immich import list_albums  # noqa: E402

log = logging.getLogger("scenarios")
KEY_NAME = "immich-family-library"
E, V = AlbumUserRole.EDITOR, AlbumUserRole.VIEWER


async def own_asset(acct: Account, filename: str) -> UUID:
    """The member's own live asset with this file name. Fails loudly if moved already."""
    res = await acct.client.search.search_assets(MetadataSearchDto(size=500, with_exif=False))
    for a in res.assets.items:
        if a.original_file_name == filename and a.owner_id == acct.user_id:
            return a.id
    raise SystemExit(f"{acct.name} has no live {filename}; re-run the seed or `reupload`")


async def family_album(fam: Account, name: str):
    for a in await list_albums(fam.client):
        if a.owner_id == fam.user_id and a.name == name:
            return a
    raise SystemExit(f"the family has no album named {name!r}")


async def non_participant(accounts: Accounts) -> UUID:
    ours = {a.email for a in [accounts.family, *accounts.members.values()]}
    users = await accounts.family.client.users.search_users()
    return next(u.id for u in users if u.email not in ours)


# ---- subcommands


async def keys(accounts: Accounts, args, cfg_url: str, password: str) -> None:
    for name, a in accounts.members.items():
        (key,) = [k for k in await a.client.api_keys.get_api_keys() if k.name == KEY_NAME]
        have = {str(p.value) for p in key.permissions}
        want = have | MEMBER_PERMISSIONS
        if want != have:
            dto = ApiKeyUpdateDto(name=KEY_NAME, permissions=[Permission(p) for p in sorted(want)])
            await a.client.api_keys.update_api_key(id=key.id, api_key_update_dto=dto)
        log.info("%s: +%s", name, sorted(want - have))
    email = os.environ.get("IFL_ADMIN_EMAIL")
    if not email:
        log.info("admin: IFL_ADMIN_EMAIL not set, skipping the admin key")
        return
    anon = AsyncClient(base_url=cfg_url)
    try:
        res = await anon.auth.login(LoginCredentialDto(email=email, password=password))
    finally:
        await anon.close()
    admin = AsyncClient(base_url=cfg_url, access_token=res.access_token)
    try:
        existing = [k for k in await admin.api_keys.get_api_keys() if k.name == KEY_NAME]
        if existing:
            have = {str(p.value) for p in existing[0].permissions}
            if not have >= ADMIN_PERMISSIONS:
                dto = ApiKeyUpdateDto(
                    name=KEY_NAME,
                    permissions=[Permission(p) for p in sorted(have | ADMIN_PERMISSIONS)],
                )
                await admin.api_keys.update_api_key(id=existing[0].id, api_key_update_dto=dto)
            log.info("admin: key exists, permissions ok")
            return
        created = await admin.api_keys.create_api_key(
            ApiKeyCreateDto(
                name=KEY_NAME, permissions=[Permission(p) for p in sorted(ADMIN_PERMISSIONS)]
            )
        )
        if args.admin_secret_out:
            Path(args.admin_secret_out).write_text(created.secret)
            log.info("admin: key created, secret written to %s", args.admin_secret_out)
        else:
            log.info("admin: key created; pass --admin-secret-out to keep the secret")
    finally:
        await admin.close()


async def convert_cases(accounts: Accounts, args, *_) -> None:
    fam, mem = accounts.family, accounts.members
    alice, bob = mem["alice"], mem["bob"]
    admin = await non_participant(accounts)
    if not any(
        a.name == "Garden" and a.owner_id == alice.user_id for a in await list_albums(alice.client)
    ):
        await alice.client.albums.create_album(
            CreateAlbumDto(album_name="Garden", description="Tomato season, mostly failures")
        )
    garden = await ensure_album(alice, "Garden", share={fam.user_id: E, admin: V})
    await add_to_album(alice, garden, [await own_asset(alice, "alice-garden.jpg")])
    trip = await ensure_album(bob, "Family Trip", share={fam.user_id: E, alice.user_id: E})
    await add_to_album(
        bob, trip, [await own_asset(bob, f) for f in ("bob-hike-1.jpg", "bob-hike-2.jpg")]
    )
    await add_to_album(alice, trip, [await own_asset(alice, "alice-trip-1.jpg")])
    log.info("Garden %s; bob's Family Trip %s", garden, trip)


async def more_albums(accounts: Accounts, args, *_) -> None:
    fam, mem = accounts.family, accounts.members
    for owner, name, files in [
        ("bob", "Hikes", ["bob-hike-1.jpg", "bob-hike-2.jpg"]),
        ("carol", "Cat", ["carol-cat.jpg"]),
        ("bob", "Kitchen", ["bob-kitchen.jpg"]),
    ]:
        aid = await ensure_album(mem[owner], name, share={fam.user_id: E})
        await add_to_album(mem[owner], aid, [await own_asset(mem[owner], f) for f in files])
        log.info("%s's %s %s", owner, name, aid)


async def break_reconcile(accounts: Accounts, args, dropbox: str, *_) -> None:
    fam, bob = accounts.family, accounts.members["bob"]
    mine = {a.name: a for a in await list_albums(fam.client) if a.owner_id == fam.user_id}
    if dropbox in mine:
        if mine[dropbox].asset_count:
            raise SystemExit("the dropbox isn't empty; not deleting it")
        await fam.client.albums.delete_album(id=mine[dropbox].id)
        log.info("deleted the dropbox")
    if "Handmade" not in mine:
        await fam.client.albums.create_album(CreateAlbumDto(album_name="Handmade"))
        log.info("created Handmade, unshared")
    hikes = await family_album(fam, "Hikes")
    await fam.client.albums.update_album_user(
        id=hikes.id, user_id=str(bob.user_id), update_album_user_dto=UpdateAlbumUserDto(role=V)
    )
    log.info("bob is a viewer on Hikes")


async def bobs_picks(accounts: Accounts, args, *_) -> None:
    alice, bob = accounts.members["alice"], accounts.members["bob"]
    picks = await ensure_album(bob, "Bob's Picks", share={alice.user_id: E})
    await add_to_album(alice, picks, [await own_asset(alice, "alice-garden.jpg")])
    log.info("Bob's Picks %s holds alice-garden.jpg", picks)


async def edit(accounts: Accounts, args, *_) -> None:
    alice = accounts.members["alice"]
    aid = await own_asset(alice, "alice-beach-1.jpg")
    await alice.client.assets.update_asset(
        id=aid,
        update_asset_dto=UpdateAssetDto(
            description="Edited in Immich: low tide, seals on the rocks",
            rating=4,
            is_favorite=True,
            latitude=44.70,
            longitude=-63.60,
            date_time_original="2026-05-04T15:30:00+02:00",
        ),
    )
    edits = AssetEditsCreateDto(
        edits=[
            AssetEditActionItemDto(
                action=AssetEditAction.CROP,
                parameters=AssetEditActionItemDtoParameters(
                    CropParameters(x=40, y=30, width=560, height=420)
                ),
            ),
            AssetEditActionItemDto(
                action=AssetEditAction.ROTATE,
                parameters=AssetEditActionItemDtoParameters(RotateParameters(angle=90)),
            ),
        ]
    )
    await alice.client.assets.edit_asset(id=aid, asset_edits_create_dto=edits)
    (tag,) = await alice.client.tags.upsert_tags(TagUpsertDto(tags=["Holidays/Beach"]))
    await alice.client.tags.tag_assets(id=tag.id, bulk_ids_dto=BulkIdsDto(ids=[aid]))
    log.info(
        "alice-beach-1.jpg %s edited: description, rating, favourite, GPS, date, crop, rotate, tag",
        aid,
    )


async def reupload(accounts: Accounts, args, *_) -> None:
    fam, mem = accounts.family, accounts.members
    if args.family_trash:
        res = await fam.client.search.search_assets(MetadataSearchDto(size=500, with_exif=False))
        copy = next(a for a in res.assets.items if a.original_file_name == args.family_trash)
        await fam.client.assets.delete_assets(AssetBulkDeleteDto(ids=[copy.id], force=False))
        log.info("family trashed its copy of %s", args.family_trash)
    for name in args.members:
        await mem[name].client.trash.empty_trash()
        log.info("%s: trash emptied", name)
    await asyncio.sleep(3)
    for owner, name, days, lat, lon, desc in PHOTOS:
        if owner in args.members and (not args.only or name in args.only):
            taken = BASE - timedelta(days=days, hours=3)
            await upload(mem[owner], name, make_jpeg(name, taken, lat, lon, desc), taken)


async def reshare(accounts: Accounts, args, *_) -> None:
    fam, carol = accounts.family, accounts.members["carol"]
    cat = await own_asset(carol, "carol-cat.jpg")
    album = await family_album(fam, "Cat")
    await carol.client.albums.add_assets_to_album(id=album.id, bulk_ids_dto=BulkIdsDto(ids=[cat]))
    log.info("carol re-shared %s into the family's Cat album", cat)


COMMANDS = {
    "keys": keys,
    "convert-cases": convert_cases,
    "more-albums": more_albums,
    "break-reconcile": break_reconcile,
    "bobs-picks": bobs_picks,
    "edit": edit,
    "reupload": reupload,
    "reshare": reshare,
}


async def main(args) -> int:
    password = os.environ.get("IFL_DEV_PASSWORD")
    if not password:
        log.error("set IFL_DEV_PASSWORD (DEV_PASSWORD from accounts.env on the dev server)")
        return 2
    accounts, dropbox = await login_all(args.config, password)
    try:
        import tomllib

        url = tomllib.loads(args.config.read_text())["immich"]["url"]
        if args.command == "keys":
            await keys(accounts, args, url, password)
        elif args.command == "break-reconcile":
            await break_reconcile(accounts, args, dropbox)
        else:
            await COMMANDS[args.command](accounts, args)
        return 0
    finally:
        await accounts.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("-c", "--config", type=Path, default=Path("config.toml"))
    sub = p.add_subparsers(dest="command", required=True)
    k = sub.add_parser("keys")
    k.add_argument("--admin-secret-out", help="file to write a newly created admin key's secret to")
    for name in (
        "convert-cases",
        "more-albums",
        "break-reconcile",
        "bobs-picks",
        "edit",
        "reshare",
    ):
        sub.add_parser(name)
    r = sub.add_parser("reupload")
    r.add_argument(
        "members",
        nargs="*",
        default=["alice"],
        help="members whose trash to empty and photos to re-upload",
    )
    r.add_argument("--only", nargs="*", help="only these file names")
    r.add_argument("--family-trash", help="first trash the family's copy of this file name")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(main(p.parse_args())))
