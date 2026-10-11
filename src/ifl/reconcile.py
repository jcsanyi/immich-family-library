"""Stage 3: family album consistency.

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


def _failed(result: ReconcileResult, what: str, e: Exception) -> None:
    """One fix raised. Log it, list it, and let the others run; the scan finds
    this one again next pass."""
    log.exception("could not %s", what)
    result.failed.append(f"{what}: {type(e).__name__}: {e}")


async def reconcile(
    accounts: Accounts, scan: ReconcileScan, webhook: WebhookConfig | None = None
) -> ReconcileResult:
    """Apply a reconcile scan. Every step is idempotent, and one failing doesn't
    stop the others."""
    family = accounts.family
    editors = {m.user_id: AlbumUserRole.EDITOR for m in accounts.members.values()}
    names = {m.user_id: m.name for m in accounts.members.values()}
    result = ReconcileResult(created_dropbox=None, shared=0, promoted=0)

    if scan.create_dropbox:
        try:
            album = await create_album(family.client, scan.create_dropbox)
            result.created_dropbox = album
            await add_album_users(family.client, album.id, editors)
            result.shared += len(editors)
            log.info(
                "created dropbox %r (%s), shared with %d members",
                album.name,
                album.id,
                len(editors),
            )
        except Exception as e:  # noqa: BLE001 - every fix is independent and idempotent
            _failed(result, f"create dropbox {scan.create_dropbox!r}", e)

    for fix in scan.fixes:
        if fix.add:
            try:
                await add_album_users(family.client, fix.album.id, {u: editors[u] for u in fix.add})
                result.shared += len(fix.add)
                log.info("shared %r with %d more members", fix.album.name, len(fix.add))
            except Exception as e:  # noqa: BLE001
                _failed(result, f"share {fix.album.name!r}", e)
        for u in fix.promote:
            try:
                await set_album_user_role(family.client, fix.album.id, u, AlbumUserRole.EDITOR)
                result.promoted += 1
                log.info("promoted %s to editor on %r", names.get(u, u), fix.album.name)
            except Exception as e:  # noqa: BLE001
                _failed(result, f"promote {names.get(u, u)} on {fix.album.name!r}", e)

    for uid in scan.partner_with:
        try:
            await create_partner(family.client, uid)
            result.partnered += 1
            log.info("partner-shared the family library with %s", names.get(uid, uid))
        except Exception as e:  # noqa: BLE001
            _failed(result, f"partner-share with {names.get(uid, uid)}", e)

    if webhook is not None:
        for fix in scan.workflows:
            try:
                if fix.existing is None:
                    await create_workflow(
                        fix.member.client, WORKFLOW_NAME, webhook.url, webhook.token
                    )
                    log.info("created the webhook workflow for %s", fix.member.name)
                else:
                    await update_workflow(
                        fix.member.client, fix.existing, webhook.url, webhook.token
                    )
                    log.info("fixed the webhook workflow for %s", fix.member.name)
                result.workflows += 1
            except Exception as e:  # noqa: BLE001
                _failed(result, f"set up the webhook workflow for {fix.member.name}", e)
    return result
