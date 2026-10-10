"""Command line entry point: `ifl observe`, `ifl convert-album`, `ifl show-config`."""

from __future__ import annotations

import argparse
import asyncio
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
from ifl.ledger import Ledger
from ifl.observer import describe_albums, describe_assets, scan_albums, scan_assets

log = logging.getLogger("ifl")


@asynccontextmanager
async def opened(config_path: Path, *, writes: bool) -> AsyncIterator[tuple[Config, Accounts]]:
    cfg = load_config(config_path)
    if writes and cfg.readonly:
        raise ConfigError(
            "config is readonly; set readonly = false under [service] to allow this command"
        )
    accounts = await open_accounts(cfg)
    try:
        yield cfg, accounts
    finally:
        await accounts.close()


async def run_observe(config_path: Path) -> int:
    """One pass: report what the rules would do. Never writes."""
    async with opened(config_path, writes=False) as (cfg, accounts):
        ledger = Ledger(cfg.ledger_path)
        try:
            albums = await scan_albums(accounts)
            log.info("albums: %s", "; ".join(describe_albums(albums)))
            # Stage 5's `process` converts `albums` and reconciles family albums here,
            # before the asset scan.
            assets = await scan_assets(accounts, cfg.dropbox_album)
            log.info("assets: %s", "; ".join(describe_assets(assets)))
            # Stage 5's `process` moves `assets` here.
            log.info("ledger: %s", ledger.counts())
            return 0
        finally:
            ledger.close()


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


def run_show_config(config_path: Path, minimal: bool) -> int:
    """Print the effective config as TOML, keys redacted. No network."""
    sys.stdout.write(tomli_w.dumps(to_raw(load_config(config_path), minimal=minimal)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ifl")
    parser.add_argument("-c", "--config", type=Path, default=Path("config.toml"))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("observe", help="one pass: report what the rules would do; writes nothing")
    conv = sub.add_parser(
        "convert-album", help="turn a member album shared with the family into a family album"
    )
    which = conv.add_mutually_exclusive_group(required=True)
    which.add_argument("album_id", type=UUID, nargs="?")
    which.add_argument(
        "--all", action="store_true", help="every convertible album, up to [limits] albums_per_pass"
    )
    show = sub.add_parser("show-config", help="print the effective config, secrets redacted")
    show.add_argument("--minimal", action="store_true", help="only what differs from the defaults")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        if args.command == "show-config":
            return run_show_config(args.config, args.minimal)
        if args.command == "observe":
            return asyncio.run(run_observe(args.config))
        return asyncio.run(run_convert_album(args.config, None if args.all else args.album_id))
    except (ConfigError, AccountError) as e:
        log.error("%s", e)
        return 2
    except ConvertError as e:
        log.error("%s", e)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
