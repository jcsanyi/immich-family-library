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
from ifl.convert import ConvertError, convert_album, convert_albums
from ifl.daemon import Daemon
from ifl.move import (
    MoveBlocked,
    MoveError,
    MoveOptions,
    advance_marks,
    move_asset,
    move_assets,
    outside_partners,
    pass_backlog,
    tally_reupload,
)
from ifl.observer import (
    AssetScan,
    describe_albums,
    describe_assets,
    describe_reconcile,
    scan_albums,
    scan_assets,
    scan_reconcile,
)
from ifl.process import run_pass
from ifl.reconcile import reconcile
from ifl.state import Locked, State, StateError, hold_lock

log = logging.getLogger("ifl")


@asynccontextmanager
async def opened(config_path: Path, *, writes: bool) -> AsyncIterator[tuple[Config, Accounts]]:
    """The config and the opened accounts. A command that writes to Immich also
    holds the run lock for its whole run, so two of them never create the same
    album twice or work on the same photos. As a side effect the holder is the
    state file's one writer."""
    cfg = load_config(config_path)
    if writes and cfg.readonly:
        raise ConfigError(
            "config is readonly; set readonly = false under [service] to allow this command"
        )
    with hold_lock(cfg.state_path) if writes else contextlib.nullcontext():
        accounts = await open_accounts(cfg)
        try:
            yield cfg, accounts
        finally:
            await accounts.close()


def own_state(cfg: Config, accounts: Accounts) -> State:
    """The state for a command that holds the run lock, cut down to the configured members."""
    state = State.load(cfg.state_path, owner=True)
    state.keep_only({m.user_id for m in accounts.members.values()})
    return state


async def run_observe(config_path: Path) -> int:
    """One pass: report what the rules would do. Never writes to Immich, and
    reads the state file without creating it."""
    async with opened(config_path, writes=False) as (cfg, accounts):
        state = State.load(cfg.state_path)
        # The same three scans `process` acts on, in the same order, reported only.
        albums = await scan_albums(accounts)
        log.info("albums: %s", "; ".join(describe_albums(albums)))
        fixes = await scan_reconcile(accounts, cfg.dropbox_album, cfg.webhook)
        log.info("family albums: %s", "; ".join(describe_reconcile(fixes, accounts)))
        assets = await scan_assets(accounts, cfg.dropbox_album, state=state)
        log.info("assets: %s", "; ".join(describe_assets(assets)))
        for who, n in assets.reuploads_by_owner().items():
            log.info("%s has %d re-uploads waiting", who, n)
        return 0


async def run_convert_album(config_path: Path, album_id: UUID | None) -> int:
    """Convert one album by id, or every convertible album up to the configured
    limit. Either way only albums the scan would convert; same decision as `observe`."""
    async with opened(config_path, writes=True) as (cfg, accounts):
        scan = await scan_albums(accounts)
        if album_id is None:
            for b in scan.blocked:
                log.warning("skipping %s: %s", b.what, b.reason)
            many = await convert_albums(accounts, scan.conversions, limit=cfg.albums_per_pass)
            for r in many.done:
                log.info("%s", r)
            log.info(
                "converted %d, failed %d, remaining %d",
                len(many.done),
                len(many.failed),
                many.remaining,
            )
            return 1 if many.failed else 0
        conv = next((c for c in scan.conversions if c.album.id == album_id), None)
        if conv is None:
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
        result = await convert_album(accounts, conv)
        log.info("%s", result)
        return 0


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
    Either way only what the scan would move; same decision as `observe`."""
    async with opened(config_path, writes=True) as (cfg, accounts):
        backlog = await pass_backlog(accounts)
        if backlog:
            log.error("not moving anything: Immich has jobs waiting: %s", backlog)
            return 1
        state = own_state(cfg, accounts)
        scan = await scan_assets(accounts, cfg.dropbox_album, state=state)
        opts = MoveOptions.from_config(cfg)
        if asset_id is None:
            for b in scan.blocked:
                log.warning("skipping %s: %s", b.what, b.reason)
            for s in scan.skipped_reuploads:
                log.info("skipping re-upload %s", s)
            many = await move_assets(
                accounts, opts, scan.moves, state=state, limit=cfg.assets_per_pass
            )
            advance_marks(state, scan, many, accounts)
            log.info("%s", many)
            return 1 if many.failed or state.unsaved else 0
        return await _move_one(accounts, state, opts, scan, asset_id)


async def _move_one(
    accounts: Accounts, state: State, opts: MoveOptions, scan: AssetScan, asset_id: UUID
) -> int:
    target = next((m for m in scan.moves if m.asset_id == asset_id), None)
    if target is None:
        blocked = next((b for b in scan.blocked if b.asset_id == asset_id), None)
        if blocked:
            log.error("not moving %s: %s", blocked.what, blocked.reason)
        else:
            log.error("asset %s is not a member-owned asset in a family-owned album", asset_id)
        return 1
    for uid in await outside_partners(accounts, target.owner):
        log.warning(
            "%s partner-shares their library with non-participant %s", target.owner.name, uid
        )
    try:
        result = await move_asset(accounts, opts, asset_id, target.owner, reupload=target.reupload)
    except MoveBlocked as e:
        log.warning("blocked %s: %s", target.file_name, e)
        return 1
    tally_reupload(state, result)
    for w in result.warnings:
        log.warning("%s: %s", result.file_name, w)
    log.info("%s", result)
    return 1 if state.unsaved else 0


async def run_process(config_path: Path) -> int:
    """One complete pass: conversions, reconcile, moves, under the limits."""
    async with opened(config_path, writes=True) as (cfg, accounts):
        state = own_state(cfg, accounts)
        result = await run_pass(accounts, cfg, state)
        log.info("pass done: %s", result)
        return 1 if result.failed or state.unsaved else 0


async def run_daemon(config_path: Path) -> int:
    """The long-running service: passes on an interval, plus the webhook receiver."""
    async with opened(config_path, writes=True) as (cfg, accounts):
        log.info(
            "daemon: a pass every %ds%s",
            cfg.interval_seconds,
            "" if cfg.webhook else "; no webhook configured",
        )
        await Daemon(accounts, cfg, own_state(cfg, accounts)).run()
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
    except (ConfigError, AccountError, StateError) as e:
        log.error("%s", e)
        return 2
    except (ConvertError, MoveError) as e:
        log.error("%s", e)
        return 1
    except Locked as e:
        log.error("%s", e)
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
