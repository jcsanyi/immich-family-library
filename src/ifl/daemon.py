"""The long-running service.

Two things in one process, sharing the accounts, the re-upload tally and a set
of in-flight asset ids:

- the pass, run every `interval_seconds`: conversions, reconcile, moves, under
  the limits. This is the only path that does paced work.
- the webhook receiver, when `[webhook]` is configured: Immich posts each new
  upload here through the per-member workflow that reconcile sets up. The
  receiver queues the asset id, drains the queue in small batches, and runs
  the re-upload half of the scan on those assets, then the normal move. A
  re-upload is cheap (nothing to upload) and the move's duplicate fast path
  needs no settling, so it's trashed seconds after it lands. Anything that
  isn't a re-upload is left for the pass.

The receiver never wakes the pass: the interval and the limits are the pacing
for real moves, and nothing the receiver handles is metered by them.

The daemon holds the run lock for as long as it runs, so no other command
writes to Immich beside it.

What the daemon remembers, it remembers in memory only: the re-upload scan's
mark, each member's tally of trashed re-uploads, and when it last reported
them. A clean stop reports the tally before the process exits; a crash loses
it. A restart reads the scan's whole window once and reports next at
`report_at`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import signal
from datetime import datetime, timedelta
from uuid import UUID

from aiohttp import web

from ifl.accounts import Accounts
from ifl.config import Config
from ifl.immich import TOKEN_HEADER, describe_error
from ifl.move import MoveManyResult, MoveOptions, move_assets
from ifl.observer import describe_assets, member_asset, reuploads_among
from ifl.process import run_pass

log = logging.getLogger(__name__)

BATCH_WAIT = 1.0  # seconds to collect more events after the first, so a backup burst is one check


class Daemon:
    def __init__(self, accounts: Accounts, cfg: Config, now: datetime | None = None):
        self.accounts = accounts
        self.cfg = cfg
        self.tally: dict[UUID, int] = {}  # trashed re-uploads per member since the last report
        self.reported_at = now or datetime.now().astimezone()
        self.in_flight: set[UUID] = set()
        self.mark: datetime | None = None  # the re-upload scan's; a restart reads the whole window
        self.events: asyncio.Queue[tuple[UUID, UUID | None]] = asyncio.Queue()
        self.stopping = asyncio.Event()

    # ---- lifecycle

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stopping.set)
        tasks = [asyncio.create_task(self.passes(), name="passes")]
        runner = None
        try:
            if self.cfg.webhook:
                tasks.append(asyncio.create_task(self.drain(), name="webhook-drain"))
                runner = await self.serve()
            await self.stopping.wait()
            log.info("stopping")
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if runner is not None:
                await runner.cleanup()
            # The tally dies with the process, so say it now, not at `report_at`.
            self.report(final=True)

    async def passes(self) -> None:
        while not self.stopping.is_set():
            try:
                result = await run_pass(
                    self.accounts, self.cfg, in_flight=self.in_flight, mark=self.mark
                )
                self.mark = result.mark
                if result.moved:
                    self.count(result.moved)
                log.info("pass done: %s", result)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pass failed; next one in %ds", self.cfg.interval_seconds)
            # Outside the try: the webhook keeps counting while passes fail, and
            # the report is the one signal for a looping phone.
            self.report()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), timeout=self.cfg.interval_seconds)

    # ---- the daily report

    def count(self, moved: MoveManyResult) -> None:
        for uid, n in moved.reuploads_trashed().items():
            self.tally[uid] = self.tally.get(uid, 0) + n

    def report(self, now: datetime | None = None, *, final: bool = False) -> None:
        """Once `report_at` has passed since the last report, or since the daemon
        started: one warning per member whose re-uploads were trashed since then.
        `final` reports at once, whatever the time."""
        now = now or datetime.now().astimezone()
        at = self.cfg.report_at
        scheduled = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
        if scheduled > now:
            scheduled -= timedelta(days=1)
        if self.reported_at >= scheduled and not final:
            return
        since = self.reported_at.astimezone(now.tzinfo).isoformat(timespec="minutes")
        names = {uid: a.name for uid, a in self.accounts.by_user_id().items()}
        for uid, n in self.tally.items():
            log.warning(
                "%s re-uploaded %d photo%s the family already has since %s; trashed",
                names[uid],
                n,
                "" if n == 1 else "s",
                since,
            )
        self.tally = {}
        self.reported_at = now

    # ---- webhook

    async def serve(self) -> web.AppRunner:
        assert self.cfg.webhook
        app = web.Application()
        app.router.add_post(self.cfg.webhook.path, self.handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        host, port = self.cfg.webhook.bind
        await web.TCPSite(runner, host, port).start()
        log.info("webhook listening on %s%s", self.cfg.webhook.listen, self.cfg.webhook.path)
        return runner

    async def handle(self, request: web.Request) -> web.Response:
        """Accept an Immich workflow webhook: check the token, queue the asset id.
        The payload is a hint, never trusted: the asset is re-read with a member key."""
        assert self.cfg.webhook
        given = request.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(given.encode(), self.cfg.webhook.token.encode()):
            return web.Response(status=401, text="bad token")
        try:
            body = await request.json()
            asset = body["data"]["asset"]
            asset_id = UUID(str(asset["id"]))
        except Exception:  # noqa: BLE001 - any malformed body is a 400
            return web.Response(status=400, text="expected {data: {asset: {id}}}")
        owner_id = None
        with contextlib.suppress(TypeError, ValueError, AttributeError):
            owner_id = UUID(str(asset.get("ownerId")))
        await self.events.put((asset_id, owner_id))
        return web.Response(status=202)

    async def drain(self) -> None:
        while True:
            first = await self.events.get()
            await asyncio.sleep(BATCH_WAIT)
            batch = [first]
            while not self.events.empty():
                batch.append(self.events.get_nowait())
            try:
                await self.handle_batch(batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("webhook batch failed; the next pass will catch anything missed")

    async def handle_batch(self, batch: list[tuple[UUID, UUID | None]]) -> None:
        found = []
        for asset_id, owner_id in dict.fromkeys(batch):
            try:
                one = await member_asset(self.accounts, asset_id, owner_id)
            except Exception as e:  # noqa: BLE001 - one unreadable asset must not cost the batch
                log.warning(
                    "webhook: could not read asset %s (%s); the next pass looks again",
                    asset_id,
                    describe_error(e),
                )
                continue
            if one is not None:
                found.append(one)
        if not found:
            return
        scan = await reuploads_among(self.accounts, found)
        if not scan.moves and not scan.skipped_reuploads:
            log.debug("webhook: %d new uploads, none the family has", len(batch))
            return
        log.info("webhook: %s", "; ".join(describe_assets(scan)))
        opts = MoveOptions.from_config(self.cfg)
        result = await move_assets(
            self.accounts, opts, scan.moves, limit=0, in_flight=self.in_flight
        )
        self.count(result)
        log.info("webhook: %s", result)
