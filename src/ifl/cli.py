"""Command line entry point: `ifl observe`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from ifl.accounts import AccountError, open_accounts
from ifl.config import ConfigError, load_config
from ifl.ledger import Ledger
from ifl.observer import describe_albums, describe_assets, scan_albums, scan_assets

log = logging.getLogger("ifl")


async def run_observe(config_path: Path, once: bool) -> int:
    cfg = load_config(config_path)
    accounts = await open_accounts(cfg)
    ledger: Ledger | None = None
    try:
        ledger = Ledger(cfg.ledger_path)
        while True:
            albums = await scan_albums(accounts)
            log.info("albums: %s", "; ".join(describe_albums(albums)))
            # Stage 2 converts `albums` here, before the asset scan.
            # Stage 3 reconciles family albums here: dropbox exists, all members are editors.
            assets = await scan_assets(accounts, cfg.dropbox_album)
            log.info("assets: %s", "; ".join(describe_assets(assets)))
            # Stage 4 moves `assets` here.
            log.info("ledger: %s", ledger.counts())
            if once:
                return 0
            await asyncio.sleep(cfg.poll_interval_seconds)
    finally:
        if ledger is not None:
            ledger.close()
        await accounts.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ifl")
    parser.add_argument("-c", "--config", type=Path, default=Path("config.toml"))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    obs = sub.add_parser("observe", help="report what the one rule would do; writes nothing")
    obs.add_argument("--once", action="store_true", help="one pass, then exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        return asyncio.run(run_observe(args.config, args.once))
    except (ConfigError, AccountError) as e:
        log.error("%s", e)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
