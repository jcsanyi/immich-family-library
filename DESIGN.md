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

1. Precheck: keys exist and work for the album owner and every asset owner in the album, and the album has no shared link (`hasSharedLink` on the album response): deleting the original would break the public link, so the member decides, by removing the link or accepting it. If either fails, leave the album alone, warn, and retry on later polls.
2. Create a family-owned album with the same name and description. If one with that name already exists, use it: a crashed run resumes into it, and two albums that share a name merge, which is acceptable for a family.
3. Share it with all members as editors. Anyone else the original was shared with, non-participants included, is carried over with their original role so nothing changes from their side. If a non-participant editor later adds a photo, the asset scan reports it as blocked, same as any non-participant asset in a family album.
4. Add the original assets to the new album using each owner's key. Members can add their own assets to an album they're an editor on, so this needs no partner access.
5. Re-read the original album once. Add anything that appeared since step 4.
6. Delete the original album.

This takes seconds. The per-asset moves happen afterwards through the one rule.

### Family album consistency

An invariant the service keeps, rather than a trigger. Each poll makes whatever changes are needed so that:

- the dropbox album exists, owned by the family account;
- every family-owned album, dropbox included, has every configured member on it as an editor;
- the family account partner-shares with every member.

That covers albums created by hand in the family account, members added to the config after albums already exist, and a fresh install with nothing set up yet. It never removes anyone from an album or a partner list; taking a member off the family is a manual job, since it also means deciding what happens to their photos.

The family creates the share (`POST /partners`, family key). "Show in timeline" is the receiving member's own setting and is left to them; it's what puts family photos in their timeline but nothing in the system depends on it. The move path does depend on the share existing: adding an asset to an album checks the caller is the asset's owner or a partner of the owner, so a member can only re-link a family copy into their own album once the family shares with them. `reconcile-albums` keeps the share in place.

### Per-asset move

Triggered when a member-owned asset is found in a family-owned album. Checked against the Immich 3.3.1 source (`utils/access.ts`, `services/asset-media.service.ts`, `services/metadata.service.ts`) and the dev instance; the calls below are the ones that exist and the key each one works under.

Before the asset scan, one check for the whole pass: `GET /queues` under an admin key with just `queue.read` (every queue route is admin-only). If the `sidecar` or `metadataExtraction` queue has jobs waiting, the sidecar we ask for in step 4 would sit behind them, so don't start the scan; log the backlog and leave the moves for the next pass. Active jobs don't count, only waiting ones: a few in flight finish in seconds, a backlog doesn't. The other queues (faces, smart search, OCR, thumbnails) don't touch anything the move reads and can churn for days, so they're ignored. The admin key is optional; without it the pass runs and relies on the per-asset poll timeout.

Read, all with the owner's key, since edits, asset files and the sidecar are owner-only:

1. `GET /assets/{id}`: checksum, file name, `fileCreatedAt`, `fileModifiedAt`, favourite, visibility, `livePhotoVideoId`, duration, `updatedAt`, `exifInfo`. Two preconditions, both "blocked, next pass" rather than errors: `exifInfo` must be present, which is the only visible sign that metadata extraction has run (there's no per-asset job status; the queue endpoints are admin-only and instance-wide), and `updatedAt` must be older than a settle period so a fresh upload or a photo whose jobs just finished isn't touched mid-flight. Visibility `locked` (private folder) or `hidden` (the video half of a live photo, handled with its photo) means skip and report.
   Two more blocks, for a photo shared outside the family in a way the move would break. If the asset is in one of the member's shared links (`GET /shared-links` with the owner's key, `sharedLink.read`, once per pass, the individual-asset links checked against each asset), trashing the original breaks the public link, so block until the member removes it from the link or deletes the link. And if it's in an album owned by a non-participant that the asset owner is no longer an editor on, nothing can re-link the family copy there, so block rather than let the photo quietly vanish from that album. An album the owner can no longer see at all isn't detectable; accepted.
   Two warnings, not blocks. If the owner partner-shares their library with a non-participant, that person stops seeing every photo the owner gives to the family: an account-level condition, warned once per pass per member and partner, not per photo. If the asset is in a stack, the stack loses a member when the original is trashed (Immich handles the fix-up): warned per asset, since it only changes the owner's own view. Likes and comments on album photos don't survive a conversion or a move either; not detectable per photo without extra calls, so that's a line in the README rather than a check.
2. `GET /albums?assetId={id}`: every album the owner can see that holds the asset. An asset can only sit in an album its owner owns or is on, so the owner's view is the full list.
3. `GET /assets/{id}/edits`: the crop, rotate and mirror list.
4. The sidecar, written fresh by Immich on request. Immich writes the XMP sidecar from its own exif row whenever description, date, GPS, rating or tags change, but the write is a queued job, so a sidecar read right after an edit can be one edit behind. Rather than re-implement Immich's encoding of dates and zones, make Immich write it now: tag the original with the configured moved tag (`family` by default; `PUT /tags/{tagId}/assets`, the tag upserted once per pass with `tag.create`, applied with `tag.asset`). The tag event queues a sidecar write immediately; poll `GET /asset-files?assetId={id}&type=sidecar` and `GET /asset-files/{fileId}` until the sidecar's `TagsList` contains the tag, which on dev took 0.1s. If it hasn't appeared within a short timeout the asset is blocked for this pass; the queue check above is what keeps that from happening routinely. That sidecar carries every edit made up to that moment, in Immich's own format. The tag stays on the trashed original, where it doubles as the member's own record of what they've handed over.
   Then rewrite one element: replace the `TagsList` with the single entry `<prefix>/<member>` (`from` by default), and drop any `hierarchicalSubject` or `dc:subject` so no tag reaches the family copy under another name. Extraction takes `TagsList` ahead of embedded keywords and replaces the copy's tags with it, so the copy ends up with exactly that tag. Doing it in the sidecar keeps tags inside the one upload; tagging afterwards would mean waiting for extraction, which otherwise overwrites them, with no clean signal for when it's done. Members' keys need `assetFile.read` and `assetFile.download`.
5. `GET /assets/{id}/original`: the bytes.

Write, with the family key:

6. `POST /assets/bulk-upload-check` with the checksum. If the family already has it, that's the copy; skip the upload. This is the resume check, and the same lookup stage 6 uses to spot re-uploads.
7. `POST /assets`: bytes, `filename`, `fileCreatedAt`, `fileModifiedAt`, `isFavorite`, `visibility`, `sidecarData`, `livePhotoVideoId`, `duration`, with the `x-immich-checksum` header. A `duplicate` status returns the existing id, so a race here resolves itself. Immich queues metadata extraction immediately; extraction reads the sidecar and spreads it over the embedded tags, so every sidecar value wins and the embedded date tags are dropped when the sidecar has a date. Nothing to wait for and nothing to fix afterwards.
8. `PUT /assets/{id}/edits` with the original's list, verbatim. The bytes are identical so the coordinates still apply. Replaces the list, so re-running is harmless.
9. `PUT /assets/{id}/metadata` with key `ifl` and the origin user id, origin asset id and time. Immich's per-asset key/value store, API-only, invisible in the UI. The machine-readable twin of the `from/<member>` tag.

Re-link:

10. For every album from step 2: family-owned albums via the family key, except the dropbox. Member-owned albums via the album owner's key, which works because an owner can always add and the family partner-shares with them (step 3 of the invariant). If the album owner isn't a participant, try the asset owner's key instead, which works only if they're still an editor on that album. If neither applies the album is reported and skipped. Adding an asset already in an album is a no-op.

Finish, with the owner's key:

11. `DELETE /assets` with `force: false`, only if the original still exists and isn't already trashed. Trash, not permanent delete; see the re-upload section.
12. Ledger: checksum, origin user id, origin asset id, family asset id, moved-at.

Every step reads its state from Immich, so a run that died anywhere is finished by running it again. Steps 6 and 11 are the only ones that branch on state; the rest are idempotent.

Live photos: the video is its own asset with visibility `hidden`, linked from the photo by `livePhotoVideoId`. Move the video first, as hidden, then the photo with the new video id. The seed data has none, so this is designed but unverified until a sample exists.

What "every carried field round-trips" means for the stage 4 verification, on the family copy once extraction has run: same checksum; `exifInfo` description, rating, latitude, longitude, `dateTimeOriginal` and `timeZone` equal to the original's; `isFavorite` and `visibility` equal; the edits list equal; present in the same family albums and in the owner's private albums that held the original; the family copy tagged `from/alice` and nothing else, even when the original was tagged; the original tagged `family` and trashed, not deleted. alice-garden.jpg is the private-album case: it's in alice's "Alice & Bob" and in the family's "Garden".

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
- When an upload lands in a member's account with a checksum the family account already owns (detected via the AssetCreate workflow trigger with a webhook action, or by polling), the system trashes it immediately. That restarts the 30-day clock and gives the phone's sync-deletions flag something to act on. It does not save the processing: every single-asset job handler in 3.3.1 looks the asset up by id with no check on `deletedAt`, so metadata, thumbnails and ML run on the trashed asset regardless. The ledger says which member it came from originally, for the warning below.
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
- Description, corrected date and time zone, GPS edits, rating, via the sidecar Immich already writes whenever a member edits those. Extraction merges the sidecar over the embedded tags for every tag, not just dates (`metadata.service.ts`, `getExifTags`).
- Favourite and visibility, set on the upload request.
- Crop, rotate, mirror, via the edits API.
- Faces, via the cluster group. The family copy's faces should cluster with the original's. Verify this happens for individual new uploads and not only on a full re-run. If only on re-run, use the cluster group regenerate-people endpoint periodically.
- A `from/<member>` tag, via the rewritten sidecar. Tags are per-user, so it's visible to whoever browses as the family account, which is where it's useful.

Lost:

- The member's own tags. Dropped on purpose; the family copy carries only `from/<member>`.
- Likes and comments on album photos. Album activity belongs to the album and the asset, and neither survives.
- Manual face corrections. The faces endpoint can read assignments off the original and reassign on the copy, so this is recoverable. Deferred.
- Stacks. Recreatable once every asset in a stack has moved. Deferred.

## Provenance

The ledger is the record of where a family asset came from. It's keyed by checksum and survives a reclaim and a later re-move.

Each family copy carries two marks. The `from/<member>` tag is the human one: tags are per-user, so it shows up for anyone browsing as the family account and nowhere else. Both halves are configurable under `[tags]`: `from_prefix` (default `from`) and, on the member's side, `moved` (default `family`), the tag that goes on the original and triggers the sidecar write. The prefix tag uses the config label, the one place a label lands in Immich; renaming a member in config doesn't retag what's already moved. The `ifl` entry in Immich's per-asset metadata store (`PUT /assets/{id}/metadata`, a JSON value under a key) is the machine one, with the origin user id, origin asset id and move time, API-only and invisible in the UI, which the ledger can be rebuilt from. The description field is the only thing members can see on a partner asset, and the system shouldn't write into it.

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

Immich's permission names, as the API key editor shows them:

| Key | Needs |
|---|---|
| Member | `user.read`, `apiKey.read`, `album.read`, `album.delete`, `albumAsset.create`, `asset.read`, `asset.download`, `asset.delete`, `asset.edit.get`, `assetFile.read`, `assetFile.download`, `tag.create`, `tag.asset`, `sharedLink.read`, `partner.read` |
| Family | the member set less the tag permissions, plus `album.create`, `albumUser.create`, `albumUser.update`, `asset.upload`, `asset.update`, `asset.edit.create`, `partner.read`, `partner.create`; later `person.read` and `person.update` for people sharing |
| Admin (optional) | `queue.read` only. A key belonging to an admin user, used for nothing but the queue check before a pass |

Tag permissions sit with members only: the moved tag goes on the member's original, and the family copy's tag arrives through the sidecar, not the tags API. Access rules that matter (from `utils/access.ts`): downloading an original is allowed to the owner, anyone on an album holding it, or a partner; edits, asset files and the sidecar are owner-only; adding to an album needs editor on the album and owner-or-partner on the asset; `POST /assets/copy` needs ownership of both assets, so it can't be used across accounts.

Precheck validates that a key works (hit the current-user endpoint) rather than that it merely exists. Keys get revoked and trimmed. A 403 on an operation is treated the same as a missing key: block, warn, retry later.

Which keys an operation needs:

| Operation | Keys |
|---|---|
| Convert album | album owner, every asset owner in it; the owner's key also answers whether the album has a shared link |
| Move asset | asset owner, family, plus owners of any member albums it gets re-linked to; admin for the queue check if configured |
| Dropbox | asset owner, family |
| Reclaim | requesting member, family |
| Re-trash a looped upload | asset owner |

## Things to verify on the dev instance before trusting them

- Sidecar precedence for description, GPS and rating: confirmed in the 3.3.1 source, the sidecar is spread over the embedded tags for every key. Still to see round-trip on dev in stage 4.
- That extraction reads the `TagsList` we rewrite and replaces the member's tags on the family copy. Stage 4: alice-trip-1.jpg already carries a member tag on dev; the copy must have only `from/alice`.
- That tagging an asset writes the sidecar at once with every pending edit in it: confirmed on dev, 0.1s on an idle instance. The settle period covers a busy one.
- A member's key can add a partner-shared asset to an album they own or edit: confirmed in the access rules (owner or partner on the asset), to be seen live in stage 4 with alice-garden.jpg and "Alice & Bob".
- Trashing an asset right after upload doesn't stop its queued jobs: confirmed in the source (`asset-job.repository.ts`), the single-asset queries have no `deletedAt` filter. Re-trashing is about the trash clock and the sync-deletions flag, not saving work.
- A single new upload in a cluster-group account clusters with existing cross-user faces, versus only on a full re-run.
- The sync-deletions flag on iOS, and the failure mode: trash via API, don't open the app, purge, open the app. If it re-uploads, that's the production failure mode. Deferred: mobile tests may run on prod instead of dev, with one hand-moved photo, once stage 6 (re-upload detection and re-trash) is in and tested. Stage 6 is a hard gate before anything touches prod.
- Live photos: the video half needs uploading and linking via livePhotoVideoId.

## Stages

Stages 2 to 4 are CLI commands only, run by hand against the dev instance and verified one at a time. Nothing acts on its own until stage 7.

1. Read-only observer. Polls everything, logs what the rules would do. No writes. Done: `ifl observe`, verified on the dev instance locally and as a container on the Immich docker network.
2. Album conversion. `ifl convert-album <album-id>`, and `--all` for every convertible album up to the limit. Done: verified on the dev instance for a fresh album, one with a description and a non-participant viewer, a merge into an existing family album, and `--all` stopping at the limit. The shared-link block on the original album was added to the design afterwards and lands with stage 4.
3. Family album consistency. `ifl reconcile-albums`: creates the dropbox if missing, adds every configured member as editor to every family-owned album, promoting members who are only viewers, and partner-shares the family library with every member. Done: verified on the dev instance with the dropbox deleted, an unshared family album, a member demoted to viewer, and no partners.
4. Per-asset move. `ifl move <asset-id>`: preconditions, tag-triggered sidecar, edits, provenance, re-link, trash; verify every carried field round-trips as listed under the move flow. Covers the dropbox. Also adds the `[tags]`, settle period and optional `[admin]` config, and the queue check before the asset scan. The re-link depends on the partner share, which stage 3's reconcile already keeps.
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
