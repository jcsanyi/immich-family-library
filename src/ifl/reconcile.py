"""Family album consistency.

An invariant rather than a trigger: the dropbox album exists, owned by the
family account; every configured member is an editor on every family-owned
album; and the family partner-shares its library with every member, which is
what lets a member put a family copy into their own album. That covers albums
created by hand in the family account, members added to the config after
albums exist, and a fresh install. Nobody is ever removed; taking a member off
the family is a manual job.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from immichpy.client.generated.models.album_user_role import AlbumUserRole

from ifl.accounts import Accounts
from ifl.config import WebhookConfig
from ifl.immich import (
    AlbumSummary,
    add_album_users,
    create_album,
    create_partner,
    create_workflow,
    log_failure,
    set_album_user_role,
    update_workflow,
)
from ifl.observer import WORKFLOW_NAME, ReconcileScan

log = logging.getLogger(__name__)


@dataclass
class ReconcileResult:
    created_dropbox: AlbumSummary | None
    shared: int  # (album, member) pairs added
    promoted: int  # (album, member) pairs promoted to editor
    partnered: int = 0  # members the family now partner-shares with
    workflows: int = 0  # webhook workflows created or fixed
    failed: list[str] = field(default_factory=list)  # fixes that raised; found again next pass

    def __str__(self) -> str:
        parts = []
        if self.created_dropbox:
            parts.append(f"created dropbox {self.created_dropbox.name!r}")
        parts.append(
            f"shared {self.shared}, promoted {self.promoted}, partnered {self.partnered}, "
            f"workflows {self.workflows}"
        )
        if self.failed:
            parts.append(f"failed {len(self.failed)}")
        return "; ".join(parts)


async def reconcile(
    accounts: Accounts, scan: ReconcileScan, webhook: WebhookConfig | None = None
) -> ReconcileResult:
    """Apply a reconcile scan. Every step is idempotent, and one failing doesn't
    stop the others."""
    family = accounts.family
    editors = {m.user_id: AlbumUserRole.EDITOR for m in accounts.members.values()}
    names = {m.user_id: m.name for m in accounts.members.values()}
    result = ReconcileResult(created_dropbox=None, shared=0, promoted=0)

    async def fixed(what: str, step: Callable[[], Awaitable[None]]) -> bool:
        """Run one fix. If it raises, list it and let the others run: the scan
        finds it again next pass. The fix comes as a callable, so that building
        its arguments is inside the `try` as well."""
        try:
            await step()
        except Exception as e:  # noqa: BLE001 - every fix is independent and idempotent
            result.failed.append(f"{what}: {log_failure(log, f'could not {what}', e)}")
            return False
        return True

    async def create_dropbox(name: str) -> None:
        result.created_dropbox = album = await create_album(family.client, name)
        await add_album_users(family.client, album.id, editors)

    if scan.create_dropbox and await fixed(
        f"create dropbox {scan.create_dropbox!r}", lambda: create_dropbox(scan.create_dropbox)
    ):
        album = result.created_dropbox
        result.shared += len(editors)
        log.info(
            "created dropbox %r (%s), shared with %d members", album.name, album.id, len(editors)
        )

    for fix in scan.fixes:
        if fix.add and await fixed(
            f"share {fix.album.name!r}",
            lambda fix=fix: add_album_users(
                family.client, fix.album.id, {u: editors[u] for u in fix.add}
            ),
        ):
            result.shared += len(fix.add)
            log.info("shared %r with %d more members", fix.album.name, len(fix.add))
        for u in fix.promote:
            who = names.get(u, u)
            if await fixed(
                f"promote {who} on {fix.album.name!r}",
                lambda fix=fix, u=u: set_album_user_role(
                    family.client, fix.album.id, u, AlbumUserRole.EDITOR
                ),
            ):
                result.promoted += 1
                log.info("promoted %s to editor on %r", who, fix.album.name)

    for uid in scan.partner_with:
        who = names.get(uid, uid)
        if await fixed(
            f"partner-share with {who}", lambda uid=uid: create_partner(family.client, uid)
        ):
            result.partnered += 1
            log.info("partner-shared the family library with %s", who)

    if webhook is not None:
        for fix in scan.workflows:
            what = f"set up the webhook workflow for {fix.member.name}"

            def step(fix=fix) -> Awaitable[None]:
                client = fix.member.client
                if fix.existing is None:
                    return create_workflow(client, WORKFLOW_NAME, webhook.url, webhook.token)
                return update_workflow(client, fix.existing, webhook.url, webhook.token)

            if await fixed(what, step):
                result.workflows += 1
                verb = "created" if fix.existing is None else "fixed"
                log.info("%s the webhook workflow for %s", verb, fix.member.name)
    return result
