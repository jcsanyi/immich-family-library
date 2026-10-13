"""Command line entry point. `ifl --help` lists the commands."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import tomli_w

from ifl.accounts import AccountError, Accounts, open_accounts
from ifl.config import Config, ConfigError, load_config, to_raw
from ifl.convert import convert_albums
from ifl.daemon import Daemon
from ifl.immich import IMMICH_ERRORS, Failure, describe_error
from ifl.lock import Locked, LockError, hold_lock
from ifl.move import (
    MoveOptions,
    move_assets,
    pass_backlog,
    prepare_temp_dir,
)
from ifl.observer import (
    MoveAsset,
    describe_albums,
    describe_assets,
    describe_reconcile,
    member_asset,
    reuploads_among,
    scan_albums,
    scan_assets,
    scan_reconcile,
)
from ifl.process import run_pass
from ifl.reconcile import reconcile

log = logging.getLogger("ifl")


@asynccontextmanager
async def opened(config_path: Path, *, writes: bool) -> AsyncIterator[tuple[Config, Accounts]]:
    """The config and the opened accounts. A command that writes to Immich also
    holds the run lock for its whole run, so two of them never create the same
    album twice or work on the same photos."""
    cfg = load_config(config_path)
    if writes and cfg.readonly:
        raise ConfigError(
            "config is readonly; set readonly = false under [service] to allow this command"
        )
    with hold_lock(cfg.lock_path) if writes else contextlib.nullcontext():
        if writes:
            prepare_temp_dir(cfg.temp_dir)
        accounts = await open_accounts(cfg)
        try:
            yield cfg, accounts
        finally:
            await accounts.close()


async def run_observe(config_path: Path) -> int:
    """One pass: report what the rules would do. Never writes to Immich."""
    async with opened(config_path, writes=False) as (cfg, accounts):
        # The same three scans `process` acts on, in the same order, reported only.
        albums = await scan_albums(accounts)
        log.info("albums: %s", "; ".join(describe_albums(albums)))
        fixes = await scan_reconcile(accounts, cfg.dropbox_album, cfg.webhook)
        log.info("family albums: %s", "; ".join(describe_reconcile(fixes, accounts)))
        assets = await scan_assets(accounts, cfg.dropbox_album)
        log.info("assets: %s", "; ".join(describe_assets(assets)))
        for who, n in assets.reuploads_by_owner().items():
            log.info("%s has %d re-uploads waiting", who, n)
        backlog = await pass_backlog(accounts)
        if backlog:
            log.warning("a pass would move nothing now: Immich has jobs waiting: %s", backlog)
        return 0


async def run_convert_album(config_path: Path, album_id: UUID | None) -> int:
    """Convert one album by id, or every convertible album up to the configured
    limit. Either way only albums the scan would convert; same decision as `observe`."""
    async with opened(config_path, writes=True) as (cfg, accounts):
        scan = await scan_albums(accounts)
        if album_id is None:
            for b in scan.blocked:
                log.warning("skipping %s: %s", b.what, b.reason)
            todo, limit = scan.conversions, cfg.albums_per_pass
        else:
            todo, limit = [c for c in scan.conversions if c.album.id == album_id], 0
            if not todo:
                blocked = next((b for b in scan.blocked if b.album_id == album_id), None)
                if blocked:
                    log.error("not converting %s: %s", blocked.what, blocked.reason)
                else:
                    log.error(
                        "album %s is not a member-owned album shared with the family "
                        "(or its owner has no key)",
                        album_id,
                    )
                return 1
        many = await convert_albums(accounts, todo, limit=limit)
        for r in many.done:
            log.info("%s", r)
        if album_id is None:
            log.info(
                "converted %d, failed %d, remaining %d",
                len(many.done),
                len(many.failed),
                many.remaining,
            )
        return 1 if many.failed else 0


async def run_reconcile_albums(config_path: Path) -> int:
    """Make the family albums consistent: dropbox exists, every member is an editor."""
    async with opened(config_path, writes=True) as (cfg, accounts):
        scan = await scan_reconcile(accounts, cfg.dropbox_album, cfg.webhook)
        log.info("family albums: %s", "; ".join(describe_reconcile(scan, accounts)))
        if not scan:
            return 0
        result = await reconcile(accounts, scan, cfg.webhook)
        log.info("%s", result)
        return 1 if result.failed else 0


async def run_move(config_path: Path, asset_id: UUID | None) -> int:
    """Move one asset by id, or every movable asset up to the configured limit.
    `--all` moves only what the scan would; same decision as `observe`. One id
    is looked up directly, so a re-upload older than the scan's window still
    moves."""
    async with opened(config_path, writes=True) as (cfg, accounts):
        backlog = await pass_backlog(accounts)
        if backlog:
            log.error("not moving anything: Immich has jobs waiting: %s", backlog)
            return 1
        scan = await scan_assets(accounts, cfg.dropbox_album, reuploads=asset_id is None)
        opts = MoveOptions.from_config(cfg)
        if asset_id is None:
            for b in scan.blocked:
                log.warning("skipping %s: %s", b.what, b.reason)
            for s in scan.skipped_reuploads:
                log.info("skipping re-upload %s", s)
            many = await move_assets(accounts, opts, scan.moves, limit=cfg.assets_per_pass)
            log.info("%s", many)
            return 1 if many.failed else 0
        target = [m for m in scan.moves if m.asset_id == asset_id]
        if not target:
            blocked = next((b for b in scan.blocked if b.asset_id == asset_id), None)
            if blocked:
                log.error("not moving %s: %s", blocked.what, blocked.reason)
                return 1
            target = await _reupload_target(accounts, asset_id)
            if not target:
                return 1
        many = await move_assets(accounts, opts, target, limit=0)
        return 1 if not many.done else 0


async def _reupload_target(accounts: Accounts, asset_id: UUID) -> list[MoveAsset]:
    """The move item for a member's asset that is in no family album but that
    the family already has, or nothing, with the reason logged."""
    found = await member_asset(accounts, asset_id)
    if found is None:
        log.error("asset %s is not a member's asset, or it is in the trash", asset_id)
        return []
    one = await reuploads_among(accounts, [found])
    for s in one.skipped_reuploads:
        log.error("not moving %s", s)
    if not one.moves and not one.skipped_reuploads:
        log.error(
            "not moving %s from %s: it is in no family-owned album, and the family has no copy",
            found[1].original_file_name,
            found[0].name,
        )
    return one.moves


async def run_process(config_path: Path) -> int:
    """One complete pass: conversions, reconcile, moves, under the limits."""
    async with opened(config_path, writes=True) as (cfg, accounts):
        result = await run_pass(accounts, cfg)
        log.info("pass done: %s", result)
        return 1 if result.failed else 0


async def run_daemon(config_path: Path) -> int:
    """The long-running service: passes on an interval, plus the webhook receiver."""
    async with opened(config_path, writes=True) as (cfg, accounts):
        log.info(
            "daemon: a pass every %ds%s",
            cfg.interval_seconds,
            "" if cfg.webhook else "; no webhook configured",
        )
        await Daemon(accounts, cfg).run()
        return 0


def run_show_config(config_path: Path, minimal: bool) -> int:
    """Print the effective config as TOML, keys redacted. No network."""
    sys.stdout.write(tomli_w.dumps(to_raw(load_config(config_path), minimal=minimal)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ifl")
    parser.add_argument("-c", "--config", type=Path, default=Path("config.toml"))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "observe", help="one pass: report what the rules would do; writes nothing to Immich"
    )
    conv = sub.add_parser(
        "convert-album", help="turn a member album shared with the family into a family album"
    )
    which = conv.add_mutually_exclusive_group(required=True)
    which.add_argument("album_id", type=UUID, nargs="?")
    which.add_argument(
        "--all", action="store_true", help="every convertible album, up to [limits] albums_per_pass"
    )
    mv = sub.add_parser("move", help="move member-owned photos in family albums to the family")
    which_asset = mv.add_mutually_exclusive_group(required=True)
    which_asset.add_argument("asset_id", type=UUID, nargs="?")
    which_asset.add_argument(
        "--all", action="store_true", help="every movable photo, up to [limits] assets_per_pass"
    )
    sub.add_parser(
        "reconcile-albums",
        help="create the dropbox if missing; make every member an editor on every family album",
    )
    sub.add_parser(
        "process",
        help="one complete pass: convert albums, reconcile family albums, move photos",
    )
    sub.add_parser("daemon", help="run passes on an interval and receive Immich's upload webhooks")
    show = sub.add_parser("show-config", help="print the effective config, secrets redacted")
    show.add_argument("--minimal", action="store_true", help="only what differs from the defaults")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    commands = {
        "observe": lambda: run_observe(args.config),
        "convert-album": lambda: run_convert_album(
            args.config, None if args.all else args.album_id
        ),
        "move": lambda: run_move(args.config, None if args.all else args.asset_id),
        "reconcile-albums": lambda: run_reconcile_albums(args.config),
        "process": lambda: run_process(args.config),
        "daemon": lambda: run_daemon(args.config),
    }
    try:
        if args.command == "show-config":
            return run_show_config(args.config, args.minimal)
        return asyncio.run(commands[args.command]())
    except (ConfigError, AccountError, LockError) as e:
        log.error("%s", e)
        return 2
    except Locked as e:
        log.error("%s", e)
        return 3
    except (Failure, *IMMICH_ERRORS) as e:
        # Each batch of work reports its own failures and goes on. What gets
        # here stopped the command outright: a scan that Immich did not answer.
        log.error("the command stopped: %s", describe_error(e))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
