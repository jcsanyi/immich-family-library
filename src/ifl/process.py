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
   each member's re-upload mark advances as far as what was finished, and the
   trashed re-uploads join their member's tally.
5. The daily report, once `report_at` has passed since the last one: one
   warning per member with a non-zero tally.

Nothing here is a loop. The long-running service (stage 7) calls this once per
interval; `ifl process` calls it once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from ifl.accounts import Accounts
from ifl.config import Config
from ifl.convert import ConvertManyResult, convert_albums
from ifl.move import (
    MoveManyResult,
    MoveOptions,
    advance_marks,
    move_assets,
    pass_backlog,
)
from ifl.observer import (
    describe_albums,
    describe_reconcile,
    scan_albums,
    scan_assets,
    scan_reconcile,
)
from ifl.reconcile import ReconcileResult, reconcile
from ifl.state import State

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
    accounts: Accounts,
    cfg: Config,
    state: State,
    *,
    in_flight: set[UUID] | None = None,
    now: datetime | None = None,
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
    moved = None
    if backlog:
        log.warning("not moving anything this pass: Immich has jobs waiting: %s", backlog)
    else:
        assets = await scan_assets(accounts, cfg.dropbox_album, state=state)
        for b in assets.blocked:
            log.warning("not moving %s: %s", b.what, b.reason)
        for s in assets.skipped_reuploads:
            log.info("skipping re-upload %s", s)
        opts = MoveOptions.from_config(cfg)
        moved = await move_assets(
            accounts, opts, assets.moves, limit=cfg.assets_per_pass, in_flight=in_flight
        )
        advance_marks(state, assets, moved, accounts)
        tally_reuploads(state, moved)
    report_reuploads(state, cfg, accounts, now)
    return PassResult(converted, reconciled, backlog, moved)


def tally_reuploads(state: State, moved: MoveManyResult) -> None:
    for uid, n in moved.reuploads_trashed().items():
        state.add_reuploads(uid, n)


STALE_REPORT = timedelta(hours=36)  # past this, the window is named rather than called a day


def report_reuploads(
    state: State, cfg: Config, accounts: Accounts, now: datetime | None = None
) -> None:
    """Once a day, when `report_at` has passed since the last report: one warning
    per member whose re-uploads were trashed since then. The first call starts
    the clock and says nothing, so the first report comes at the next
    `report_at`."""
    now = now or datetime.now().astimezone()
    if state.last_report is None:
        state.start_clock(now)
        return
    if not state.report_due(now, cfg.report_at):
        return
    since = state.last_report
    window = "in the last 24 hours"
    if now - since > STALE_REPORT:
        window = f"since {since.astimezone(now.tzinfo).isoformat(timespec='minutes')}"
    names = {uid: a.name for uid, a in accounts.by_user_id().items()}
    for uid, n in state.take_report(now).items():
        log.warning(
            "%s re-uploaded %d photo%s the family already has %s; trashed",
            names.get(uid, str(uid)),
            n,
            "" if n == 1 else "s",
            window,
        )
