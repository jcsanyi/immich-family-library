"""Stage 7: the long-running service.

Two things in one process, sharing the accounts, the state and a set of
in-flight asset ids:

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
writes to Immich beside it, and it is the state file's only writer: the file
is loaded once, at start. Both halves
share one in-memory state, which saves itself on every change.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import signal
from uuid import UUID

from aiohttp import web

from ifl.accounts import Account, Accounts
from ifl.config import Config
from ifl.immich import TOKEN_HEADER, get_asset
from ifl.move import MoveOptions, move_assets
from ifl.observer import AssetScan, describe_assets, reupload_items
from ifl.process import run_pass
from ifl.state import State

log = logging.getLogger(__name__)

BATCH_WAIT = 1.0  # seconds to collect more events after the first, so a backup burst is one check


class Daemon:
    def __init__(self, accounts: Accounts, cfg: Config, state: State):
        self.accounts = accounts
        self.cfg = cfg
        self.state = state
        self.in_flight: set[UUID] = set()
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

    async def passes(self) -> None:
        while not self.stopping.is_set():
            try:
                result = await run_pass(
                    self.accounts, self.cfg, self.state, in_flight=self.in_flight
                )
                log.info("pass done: %s", result)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pass failed; next one in %ds", self.cfg.interval_seconds)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), timeout=self.cfg.interval_seconds)

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
        members = {m.user_id: m for m in self.accounts.members.values()}
        by_member: dict[UUID, list] = {}
        for asset_id, owner_id in dict.fromkeys(batch):
            candidates = [members[owner_id]] if owner_id in members else list(members.values())
            for member in candidates:
                asset = await self._read(member, asset_id)
                if asset is not None:
                    by_member.setdefault(member.user_id, []).append(asset)
                    break
        if not by_member:
            return
        scan = AssetScan()
        await reupload_items(
            self.accounts, [(members[uid], assets) for uid, assets in by_member.items()], scan
        )
        if not scan.moves and not scan.skipped_reuploads:
            log.debug("webhook: %d new uploads, none the family has", len(batch))
            return
        log.info("webhook: %s", "; ".join(describe_assets(scan)))
        opts = MoveOptions.from_config(self.cfg)
        result = await move_assets(
            self.accounts, opts, scan.moves, state=self.state, limit=0, in_flight=self.in_flight
        )
        log.info("webhook: %s", result)

    @staticmethod
    async def _read(member: Account, asset_id: UUID):
        try:
            asset = await get_asset(member.client, asset_id)
        except Exception:  # noqa: BLE001 - not this member's, or gone already
            return None
        if asset.owner_id != member.user_id or asset.is_trashed:
            return None
        return asset
