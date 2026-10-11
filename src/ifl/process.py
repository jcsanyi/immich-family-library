"""Stage 5: one complete pass.

The order matters and is the one docs/how-it-works.md gives under "The three
scans":

1. Album scan, then conversions, up to `albums_per_pass`. A just-converted album
   is family-owned by the time the asset scan runs, so its photos move in the
   same pass.
2. Reconcile: dropbox, editors, partner shares.
3. Queue check under the admin key, if there is one. A backlog on the sidecar or
   metadata queues means the moves wait for the next pass, and so does the
   re-upload mark.
4. Asset scan, re-uploads included, then moves, up to `assets_per_pass`. Then
   each member's re-upload mark advances as far as what was finished.

Nothing here is a loop. The long-running service (stage 7) calls this once per
interval; `ifl process` calls it once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ifl.accounts import Accounts
from ifl.config import Config
from ifl.convert import ConvertManyResult, convert_albums
from ifl.ledger import Ledger
from ifl.move import (
    MoveManyResult,
    MoveOptions,
    advance_marks,
    move_assets,
    pass_backlog,
    reupload_warnings,
)
from ifl.observer import (
    describe_albums,
    describe_reconcile,
    scan_albums,
    scan_assets,
    scan_reconcile,
)
from ifl.reconcile import ReconcileResult, reconcile

log = logging.getLogger(__name__)


@dataclass
class PassResult:
    converted: ConvertManyResult
    reconciled: ReconcileResult | None  # None when nothing needed doing
    backlog: dict[str, int] | None  # queues that stopped the moves, if any
    moved: MoveManyResult | None  # None when the backlog stopped them

    @property
    def failed(self) -> bool:
        return bool(
            self.converted.failed
            or (self.reconciled and self.reconciled.failed)
            or (self.moved and self.moved.failed)
        )

    def __str__(self) -> str:
        c = self.converted
        parts = [
            f"albums: converted {len(c.done)}, failed {len(c.failed)}, remaining {c.remaining}"
        ]
        parts.append(f"family albums: {self.reconciled or 'consistent'}")
        if self.moved is None:
            parts.append(f"assets: skipped, Immich has jobs waiting: {self.backlog}")
        else:
            parts.append(f"assets: {self.moved}")
        return "; ".join(parts)


async def run_pass(
    accounts: Accounts, cfg: Config, ledger: Ledger, *, in_flight: set | None = None
) -> PassResult:
    albums = await scan_albums(accounts)
    for b in albums.blocked:
        log.warning("not converting %s: %s", b.what, b.reason)
    log.info("albums: %s", "; ".join(describe_albums(albums)))
    converted = await convert_albums(accounts, albums.conversions, limit=cfg.albums_per_pass)

    fixes = await scan_reconcile(accounts, cfg.dropbox_album, cfg.webhook)
    log.info("family albums: %s", "; ".join(describe_reconcile(fixes, accounts)))
    reconciled = await reconcile(accounts, fixes, cfg.webhook) if fixes else None

    backlog = await pass_backlog(accounts)
    if backlog:
        log.warning("not moving anything this pass: Immich has jobs waiting: %s", backlog)
        return PassResult(converted, reconciled, backlog, None)

    assets = await scan_assets(accounts, cfg.dropbox_album, ledger=ledger)
    for b in assets.blocked:
        log.warning("not moving %s: %s", b.what, b.reason)
    for s in assets.skipped_reuploads:
        log.info("skipping re-upload %s", s)
    reupload_warnings(assets, cfg.reuploads_per_pass)
    opts = MoveOptions.from_config(cfg)
    moved = await move_assets(
        accounts, ledger, opts, assets.moves, limit=cfg.assets_per_pass, in_flight=in_flight
    )
    advance_marks(ledger, assets, moved, accounts)
    return PassResult(converted, reconciled, backlog, moved)
