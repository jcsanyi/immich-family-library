"""Stage 3: family album consistency.

An invariant rather than a trigger: the dropbox album exists, owned by the
family account, and every configured member is an editor on every
family-owned album. That covers albums created by hand in the family account,
members added to the config after albums exist, and a fresh install. Nobody is
ever removed; taking a member off the family is a manual job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from immichpy.client.generated.models.album_user_role import AlbumUserRole

from ifl.accounts import Accounts
from ifl.immich import AlbumSummary, add_album_users, create_album, set_album_user_role
from ifl.observer import ReconcileScan

log = logging.getLogger(__name__)


@dataclass
class ReconcileResult:
    created_dropbox: AlbumSummary | None
    shared: int  # (album, member) pairs added
    promoted: int  # (album, member) pairs promoted to editor

    def __str__(self) -> str:
        parts = []
        if self.created_dropbox:
            parts.append(f"created dropbox {self.created_dropbox.name!r}")
        parts.append(f"shared {self.shared}, promoted {self.promoted}")
        return "; ".join(parts)


async def reconcile(accounts: Accounts, scan: ReconcileScan) -> ReconcileResult:
    """Apply a reconcile scan. Every step is idempotent."""
    family = accounts.family
    editors = {m.user_id: AlbumUserRole.EDITOR for m in accounts.members.values()}
    result = ReconcileResult(created_dropbox=None, shared=0, promoted=0)

    if scan.create_dropbox:
        album = await create_album(family.client, scan.create_dropbox)
        await add_album_users(family.client, album.id, editors)
        log.info(
            "created dropbox %r (%s), shared with %d members", album.name, album.id, len(editors)
        )
        result.created_dropbox = album
        result.shared += len(editors)

    for fix in scan.fixes:
        if fix.add:
            await add_album_users(family.client, fix.album.id, {u: editors[u] for u in fix.add})
            log.info("shared %r with %d more members", fix.album.name, len(fix.add))
            result.shared += len(fix.add)
        for u in fix.promote:
            await set_album_user_role(family.client, fix.album.id, u, AlbumUserRole.EDITOR)
            result.promoted += 1
        if fix.promote:
            log.info("promoted %d members to editor on %r", len(fix.promote), fix.album.name)
    return result
