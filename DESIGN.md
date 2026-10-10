# Immich Family Library

Design for a service that sits beside an Immich instance and moves photos from individual family members' accounts into a shared family account, driven by what members share.

This is the design as of October 2026, targeting Immich 3.3. Nothing here is built yet. It's meant to be implemented and tested one stage at a time.

This is a working document, not the final documentation. It holds the overall goal and the stages still to come. As stages are built, the real docs replace the matching sections here. Anything in it can change through discussion; when it does, this file is updated to match.

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
2. Create a family-owned album with the same name and description. If one with that name already exists, use it: a crashed run resumes into it, and two albums that share a name merge, which is acceptable for a family.
3. Share it with all members as editors. Anyone else the original was shared with, non-participants included, is carried over with their original role so nothing changes from their side. If a non-participant editor later adds a photo, the asset scan reports it as blocked, same as any non-participant asset in a family album.
4. Add the original assets to the new album using each owner's key. Members can add their own assets to an album they're an editor on, so this needs no partner access.
5. Re-read the original album once. Add anything that appeared since step 4.
6. Delete the original album.

This takes seconds. The per-asset moves happen afterwards through the one rule.

### Family album consistency

An invariant the service keeps, rather than a trigger. Each poll makes whatever changes are needed so that:

- the dropbox album exists, owned by the family account;
- every family-owned album, dropbox included, has every configured member on it as an editor.

That covers albums created by hand in the family account, members added to the config after albums already exist, and a fresh install with nothing set up yet. It never removes anyone from an album; taking a member off the family is a manual job, since it also means deciding what happens to their photos.

### Per-asset move

Triggered when a member-owned asset is found in a family-owned album.

1. Precheck: the owner's key exists and works. Take a per-asset lock in the state DB.
2. Record the asset in the ledger: checksum, owner, original asset id.
3. Read what needs carrying: album memberships (own and shared, across all participants), edit actions (crop/rotate/mirror), the sidecar file if one exists.
4. Download the original bytes. Download the sidecar via the asset-files endpoint if present.
5. Upload to the family account with: the original bytes, the sidecar as `sidecarData`, original `fileCreatedAt` and `fileModifiedAt`, favourite, and the original's visibility (timeline or archive). No interim hidden state: the fix-ups take seconds, and `hidden` is an internal Immich value for the video half of live photos, not something to lean on.
   Metadata extraction reads the sidecar on its first pass and prefers its date, so there's nothing to wait for and nothing to overwrite afterwards.
6. Apply the edit actions to the new asset, verbatim, with the family key. The bytes are identical so crop coordinates still apply.
7. Re-link: add the new asset to every album the original was in. Family albums via the family key, except the dropbox. Member-owned albums via that member's key (the member sees the family copy through partner sharing). If a contributor's key can't add, fall back to the album owner's key.
8. Trash the original with the owner's key. Trash, not permanent delete. See the re-upload section for why.
9. Update the ledger with the family asset id and release the lock.

Every step is derived from Immich state, so a failure at any step is resumed by running the move again: it checks by checksum whether the family copy exists before uploading, redoes the fix-ups regardless (applying edits replaces the list, adding to an album an asset already in it is a no-op, so they're idempotent), and checks whether the original still exists before trashing.

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
- When an upload lands in a member's account with a checksum the family account already owns (detected via the AssetCreate workflow trigger with a webhook action, or by polling), the system trashes it immediately, before most of the processing queue reaches it. That restarts the 30-day clock. The ledger says which member it came from originally, for the warning below.
- Members are told to turn on the experimental "sync remote deletions" setting in the mobile app. With it on, the next time the app opens after a trash, the phone moves its local copy to the device trash, and the loop ends for that photo. This only works while the asset is still in the server trash, which is another reason never to force-purge.
- The system counts re-uploads per member. A member with the flag on should produce zero. A member looping produces the whole moved library once per purge cycle, forever, with upload bandwidth and ML cost each time. Warn on volume, per member, not per asset.
- Permanent delete is strictly worse on both counts: the phone re-uploads on its next backup run instead of in 30 days, and the sync-deletions flag has nothing to sync against.

## Storage

Trashed originals count toward the member's usage until the purge. During the initial conversion of existing shared albums that's briefly close to double the size of whatever's converted. After that it's a rolling window of about a month of new family photos. If the initial spike is a problem, the move path is a queue, so cap moves per day in config. Don't shorten trash retention to get there; that also shortens the window members have to open the app and sync the deletion.

The family account should have no quota, or a generous one. It's the destination for everything.

## Limits

Config caps how much one pass does: `albums_per_pass` for conversions and, once the move path exists, `assets_per_pass` for moves. The two are independent: a pass converts up to X albums, then moves up to Y assets. 0 means no limit. Commands given a specific id ignore the caps; `convert-album --all` and `process` honor them. `process` is a single pass, so what's left over waits for the next run. The long-running service is what turns the caps into a rate: one pass, a fixed wait, another pass.

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

The ledger is the record of where a family asset came from. It's keyed by checksum and survives a reclaim and a later re-move.

Tags in Immich are per-user and invisible to anyone but the owner, so a family-account tag like `source/jon` is a private backup label the system can use to rebuild the ledger, not a human-facing marker. Add it anyway; it's cheap. The description field is the only thing members can see on a partner asset, and the system shouldn't write into it.

## State

Immich is the source of truth for anything in flight. A move is derived from what the server says, not from local state: the family account having the checksum means it's uploaded; the member's original still existing means it hasn't been trashed; the fix-ups in between are idempotent and simply re-run. So the move path is restartable after a crash with nothing local, and re-uploads are caught by "a member owns a checksum the family already owns".

The ledger is a record, not a controller. One SQLite table, keyed by checksum: origin member as Immich user id, origin asset id, family asset id, moved-at, reclaimed-at, re-upload count. Config labels are never stored: renaming a member in config changes how messages refer to them and nothing else. Reports resolve ids to labels through the current config at read time. It exists for the two things Immich can't tell us once the original is purged: who a family asset came from (reclaim authorization) and who is looping (per-member warnings). It also serves as the audit trail.

Locks are in-process; the service is a single process. Album memberships are read live at move time, since the original still exists until the last step.

## Detection

Each poll is two scans in a fixed order: albums first, then assets. Album conversions are acted on before the asset scan runs, so a just-converted album is already family-owned when its photos are considered, in the same poll. Poll each member's key and the family key on a short interval (a minute is fine for a family):

- family albums: list assets, find member-owned ones.
- member albums: find ones with the family user as a shared user.
- dropbox and reclaim albums: list contents.

There is no retry queue. Anything blocked (missing key, non-participant contributor) is simply found again on the next poll and re-evaluated.

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
- The sync-deletions flag on iOS, and the failure mode: trash via API, don't open the app, purge, open the app. If it re-uploads, that's the production failure mode. Deferred: mobile tests may run on prod instead of dev, with one hand-moved photo, once stage 6 (re-upload detection and re-trash) is in and tested. Stage 6 is a hard gate before anything touches prod.
- Live photos: the video half needs uploading and linking via livePhotoVideoId.

## Stages

Stages 2 to 4 are CLI commands only, run by hand against the dev instance and verified one at a time. Nothing acts on its own until stage 7.

1. Read-only observer. Polls everything, logs what the rules would do. No writes. Done: `ifl observe`, verified on the dev instance locally and as a container on the Immich docker network.
2. Album conversion. `ifl convert-album <album-id>`, and `--all` for every convertible album up to the limit. Done: verified on the dev instance for a fresh album, one with a description and a non-participant viewer, a merge into an existing family album, and `--all` stopping at the limit.
3. Family album consistency. `ifl reconcile-albums`: creates the dropbox if missing and adds every configured member as editor to every family-owned album, promoting members who are only viewers. Done: verified on the dev instance with the dropbox deleted, an unshared family album, and a member demoted to viewer.
4. Per-asset move. `ifl move <asset-id>`: sidecar, edits, re-link, trash; verify every carried field round-trips. Covers the dropbox.
5. `ifl process`: one complete pass from the CLI, honoring the per-pass limits. Album scan, conversions, reconcile, asset scan, moves. Everything the service will eventually do each pass, run once by hand.
6. Re-upload detection and re-trash as part of `process`, found by polling. Per-member warnings.
7. The long-running service: `process` passes with a fixed wait between them, plus the webhook receiver for the AssetCreate trigger so re-uploads get trashed in seconds instead of on the next poll.
8. Packaging, documentation and publishing: a published container image, the compose and config examples the README promises, and the real docs that let the rest of this file shrink.

## Future improvements

Not stages. Things worth doing once the service is running on prod, in no particular order.

- People sharing automation once face detection is on.
- Reclaim.
- Manual face reassignment and stacks on the family copy.
- Slack notifications for warnings and errors, so a blocked album or a looping member doesn't sit unnoticed in the container logs.

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
