# Immich Family Library

Design for a service that sits beside an Immich instance and moves photos from individual family members' accounts into a shared family account, driven by what members share.

This is the design as of October 2026, targeting Immich 3.3. Nothing here is built yet. It's meant to be implemented and tested one stage at a time.

## The problem

Immich has no concept of a shared library. Assets belong to one user. Shared albums don't put photos into other members' timelines, and partner sharing is all-or-nothing per user. There's no API to transfer ownership of an asset, and the maintainers have been clear that one isn't coming soon.

What I want is closer to Google Photos' partner library: each member keeps their own account and decides what to share, and shared photos show up in everyone's timeline as part of one family collection.

## Accounts

- Every member has their own Immich user.
- There is one extra user, the family account. It owns everything that's been shared.
- The family account partner-shares with every member, with "show in timeline" on. That's how family photos appear in each member's timeline. Members do not need to partner-share back; the system never needs the family account to see members' private libraries.
- All members and the family account are in one cluster group so face recognition pools across accounts. Face detection is currently off on the instance. Enable it after the cluster group exists so the first recognition pass runs on the pooled set.
- Every participating member hands the system an API key. The family account has one too. See the permissions table below.

## The one rule

A member-owned asset sitting in a family-owned album gets moved to the family account.

Everything else (album conversion, the dropbox, manual adds to a family album) is just a way of getting a member-owned asset into a family-owned album. There's one move path, and all the care about state, locking and retries lives there.

## What "shared with the family" means

An album counts as shared with the family when the family user is in its shared-user list. That's the only signal.

Albums shared privately between members are left alone. If a photo in one of those is later shared with the family, the family copy gets re-added to the private album, so the private album looks unchanged to everyone in it.

Non-participants (users with no API key) can still use Immich normally and share albums with the family user. The system just won't convert anything it can't fully handle, and warns.

## Flows

### Album conversion

Triggered when a member-owned album has the family user as a shared user.

1. Precheck: keys exist and work for the album owner and every asset owner in the album. If not, leave the album alone, warn, and retry on later polls.
2. Create a family-owned album with the same name and description.
3. Share it with all members as editors.
4. Add the original assets to the new album using each owner's key. Members can add their own assets to an album they're an editor on, so this needs no partner access.
5. Re-read the original album once. Add anything that appeared since step 4.
6. Delete the original album.

This takes seconds. The per-asset moves happen afterwards through the one rule.

### Per-asset move

Triggered when a member-owned asset is found in a family-owned album.

1. Precheck: the owner's key exists and works. Take a per-asset lock in the state DB.
2. Record the asset in the ledger: checksum, owner, original asset id, albums it's in. The ledger row is written before anything is deleted, always.
3. Read what needs carrying: album memberships (own and shared, across all participants), edit actions (crop/rotate/mirror), the sidecar file if one exists.
4. Download the original bytes. Download the sidecar via the asset-files endpoint if present.
5. Upload to the family account with: the original bytes, the sidecar as `sidecarData`, original `fileCreatedAt` and `fileModifiedAt`, favourite, and visibility set to hidden.
   Metadata extraction reads the sidecar on its first pass and prefers its date, so there's nothing to wait for and nothing to overwrite afterwards.
6. Apply the edit actions to the new asset, verbatim, with the family key. The bytes are identical so crop coordinates still apply.
7. Re-link: add the new asset to every album the original was in. Family albums via the family key, except the dropbox. Member-owned albums via that member's key (the member sees the family copy through partner sharing). If a contributor's key can't add, fall back to the album owner's key.
8. Flip visibility to timeline.
9. Trash the original with the owner's key. Trash, not permanent delete. See the re-upload section for why.
10. Update the ledger with the family asset id and release the lock.

Failure at any step leaves the ledger row with enough to resume or roll back. Steps are idempotent: re-running checks whether the family copy already exists by checksum before uploading again.

### Dropbox

A family-owned album flagged in config. Behaves exactly like any other family album in the move path, except the family copy is not re-added to it. Use it for photos that belong in the family timeline but not in any particular album.

A family-owned asset found in the dropbox (or any family album it's already in the right place for) is just removed from the dropbox. Nothing to move.

### Reclaim

Not in the first version, but the design should leave room for it.

Each member gets a private album flagged as their reclaim album. When a family-owned asset shows up in it:

1. Check the ledger: the requesting member must be the original owner of that checksum. Otherwise remove it from the reclaim album and warn.
2. Run the move path in reverse: download, upload to the member with sidecar, apply edits, re-link to the member's current albums, trash the family copy.
3. The asset disappears from every family album because the family copy is gone. That's forced by the design, not a choice. If it were re-added to a family album the one rule would move it straight back. Document this for members.

The family account has no phone backing up to it, so reclaim has no re-upload loop.

## The re-upload loop

The mobile app backs up by asking the server whether it already has each file's hash for that user. Once the original is trashed and the trash purges (30 days by default), the phone no longer finds the hash and uploads the photo again. Immich doesn't remember intentionally deleted hashes. This is a known limitation with no fix upstream.

How we handle it:

- Originals are trashed, never permanently deleted. A trashed asset still answers "yes" to the hash check, so the phone leaves it alone until the purge.
- The ledger holds every moved checksum. When an upload lands in a member's account with a checksum in the ledger (detected via the AssetCreate workflow trigger with a webhook action, or by polling), the system trashes it immediately, before most of the processing queue reaches it. That restarts the 30-day clock.
- Members are told to turn on the experimental "sync remote deletions" setting in the mobile app. With it on, the next time the app opens after a trash, the phone moves its local copy to the device trash, and the loop ends for that photo. This only works while the asset is still in the server trash, which is another reason never to force-purge.
- The system counts re-uploads per member. A member with the flag on should produce zero. A member looping produces the whole moved library once per purge cycle, forever, with upload bandwidth and ML cost each time. Warn on volume, per member, not per asset.
- Permanent delete is strictly worse on both counts: the phone re-uploads on its next backup run instead of in 30 days, and the sync-deletions flag has nothing to sync against.

## Storage

Trashed originals count toward the member's usage until the purge. During the initial conversion of existing shared albums that's briefly close to double the size of whatever's converted. After that it's a rolling window of about a month of new family photos. If the initial spike is a problem, the move path is a queue, so cap moves per day in config. Don't shorten trash retention to get there; that also shortens the window members have to open the app and sync the deletion.

The family account should have no quota, or a generous one. It's the destination for everything.

## What's carried and what's lost

Carried on the family copy:

- EXIF, in the file.
- Description, corrected date and time zone, GPS edits, rating, via the sidecar Immich already writes whenever a member edits those.
- Favourite and visibility, set on the upload request.
- Crop, rotate, mirror, via the edits API.
- Faces, via the cluster group. The family copy's faces should cluster with the original's. Verify this happens for individual new uploads and not only on a full re-run. If only on re-run, use the cluster group regenerate-people endpoint periodically.
- Tag names, as a side effect of the sidecar's TagsList. Family-account tags are invisible to members, so this is only useful for round-tripping.

Lost:

- Manual face corrections. The faces endpoint can read assignments off the original and reassign on the copy, so this is recoverable. Deferred.
- Stacks. Recreatable once every asset in a stack has moved. Deferred.

## Provenance

The ledger is the source of truth for where a family asset came from. It's keyed by checksum and survives a reclaim and a later re-move.

Tags in Immich are per-user and invisible to anyone but the owner, so a family-account tag like `source/jon` is a private backup label the system can use to rebuild the ledger, not a human-facing marker. Add it anyway; it's cheap. The description field is the only thing members can see on a partner asset, and the system shouldn't write into it.

## State

A small local database (SQLite is fine) holding:

- ledger: checksum, origin user, origin asset id, family asset id, album memberships at move time, timestamps, status, re-upload count.
- locks: per-asset, so album conversion and a manual add don't both try to move the same photo.
- retry queue: operations blocked on a missing or broken key, re-checked each poll.
- per-member counters for re-upload warnings.

## Detection

Poll each member's key and the family key on a short interval (a minute is fine for a family):

- family albums: list assets, find member-owned ones.
- member albums: find ones with the family user as a shared user.
- dropbox and reclaim albums: list contents.

Plus the AssetCreate workflow trigger with a webhook action, so looped re-uploads get trashed fast. Workflows in 3.x have only AssetCreate, AssetMetadataExtraction and AssetTagged triggers. The added-to-album trigger is an open PR with known bugs, so album detection stays polling-based.

## API key permissions

| Key | Needs |
|---|---|
| Member | asset read, download, delete, upload; asset edit read; asset file download; album read, create, update, delete; albumAsset create |
| Family | asset read, upload, update, delete; asset edit create; album read, create, update, delete; albumAsset create; album user add; partner update; tag create and tag asset; person read and update (for people sharing automation) |

Precheck validates that a key works (hit the current-user endpoint) rather than that it merely exists. Keys get revoked and trimmed. A 403 on an operation is treated the same as a missing key: block, warn, retry later.

Which keys an operation needs:

| Operation | Keys |
|---|---|
| Convert album | album owner, every asset owner in it |
| Move asset | asset owner, family, plus owners of any member albums it gets re-linked to |
| Dropbox | asset owner, family |
| Reclaim | requesting member, family |
| Re-trash a looped upload | asset owner |

## Things to verify on the dev instance before trusting them

- Sidecar precedence for description, GPS and rating. The code is explicit for dates only.
- A member's key can add a partner-shared asset to an album they're an editor on but don't own.
- Trashing an asset right after upload actually stops the queued ML jobs, or whether they run anyway.
- A single new upload in a cluster-group account clusters with existing cross-user faces, versus only on a full re-run.
- The sync-deletions flag on iOS, and the failure mode: trash via API, don't open the app, purge, open the app. If it re-uploads, that's the production failure mode.
- Live photos: the video half needs uploading and linking via livePhotoVideoId.

## Stages

1. Read-only observer. Polls everything, builds the ledger of what would move, logs the plan. No writes.
2. Per-asset move, single asset, by hand. Sidecar, edits, re-link, hidden-then-visible, trash. Verify every carried field round-trips.
3. Family album add and dropbox, driven by polling.
4. Album conversion.
5. Re-upload detection and re-trash, with the webhook. Per-member warnings.
6. People sharing automation once face detection is on.
7. Reclaim.
8. Deferred: manual face reassignment, stacks.

## Prior art

Nothing does this. Closest:

- donnchawp/immich-shared-library copies assets and ML data between accounts by writing to Postgres directly. Copy, not move; same-filesystem hardlinks; pinned to Immich versions.
- Trust1509/immich-family-tools matches people across accounts and builds shared albums via the API. Never moves assets.
- alangrainger/immich-person-to-album and ajb3932/immich-partner-sharing populate albums by face.

Upstream, the roadmap lists "better sharing" and "user groups" with no dates, and a maintainer-adjacent comment says assets may eventually be library-owned with an alias system. This whole project may become unnecessary one day. Not soon.

## References

- Ownership transfer requests: immich discussions 7885, 13334, 23776
- Shared album photos in timeline: discussion 26874
- Admin key can't delete other users' assets: issue 6788
- Mobile re-uploads after purge: issue 23897, discussion 4282
- fileCreatedAt overwritten by extraction: issue 18591
- Sync remote deletions: PR 16732, issue 20978
- Tags per-user: issue 22308, discussion 12845
- Workflows and webhooks: PR 29258 (merged), PR 30524 (added-to-album trigger, open)
- Cluster groups: v3.2.0 release notes. People sharing: v3.3.0 release notes, discussion 32196
