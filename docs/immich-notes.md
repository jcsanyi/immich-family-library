# Immich notes

What the service relies on in Immich, as of 3.3.1, and how each point was
confirmed: in the server source (`server/src/...` at the `v3.3.1` tag), on
the dev instance, or both. These started as assumptions in the design and are
listed here so the next Immich upgrade knows what to re-check.

## Access rules (`utils/access.ts`)

- Reading or downloading an asset: its owner, anyone on an album holding it,
  or a partner of the owner.
- Edits, asset files (so the sidecar) and the per-asset metadata store:
  owner only. Hence the move reads with the member's key and writes with the
  family's.
- Adding an asset to an album: owner or editor on the album, and owner or
  partner on the asset (`AssetShare`). This is why the family partner-shares
  with every member: it's what lets a member put a family copy into their own
  album. Confirmed on dev with alice's and bob's keys.
- `POST /assets/copy` needs ownership of both assets, so it can't copy a
  sidecar or album memberships across accounts.
- Queue endpoints (`/queues`) are admin-only, hence the optional admin key.
- Workflows belong to a user and fire only for that user's assets.

## Sidecar (`services/metadata.service.ts`)

- Immich writes an XMP sidecar whenever description, date, location, rating
  or tags change. It writes only the fields the user edited (its locked
  properties) plus the tag list (`TagsList`). An unedited photo's sidecar
  holds nothing but tags.
- On extraction the sidecar is spread over the embedded tags, so every
  sidecar value wins, and the embedded date tags are dropped when the sidecar
  has a date. `TagsList` is preferred over Lightroom's `hierarchicalSubject`
  and Dublin Core's `subject`. All confirmed on dev: every edited field
  round-tripped, and a tagged original produced a copy with only our tag.
- Extraction never rewrites the sidecar. Tags read from `hierarchicalSubject`
  or `subject` are folded into `TagsList` only when a later sidecar write
  happens.
- A tag add fires the sidecar job at once: 0.1s on an idle dev instance.
- An edit does not move the asset's `updatedAt`. The sidecar job does, seconds
  later. So "recently updated" is false exactly while a write is pending, and
  the settle period can't guard against a stale sidecar. The tag trigger does.

## Uploads (`services/asset-media.service.ts`)

- The `filename` form field is applied to every multipart part. With a
  sidecar attached, the sidecar part was checked as a `.jpg` and rejected
  ("Unsupported file type"). The service sends no `filename` field; each part
  names itself.
- The duplicate check is per owner, and trashed assets count. Re-uploading a
  moved photo is a no-op until the member's trash is emptied.
- `POST /assets/bulk-upload-check` answers whether an account has a checksum,
  and whether that asset is trashed. It's the resume check and the re-upload
  check.
- The edits endpoint refuses until the asset has dimensions, which extraction
  sets a few seconds after upload ("Asset dimensions are not available for
  editing"). The move waits for them before applying edits.

## Jobs

- No per-asset job status exists. `exifInfo` on the asset response appears
  once extraction has run; `hasMetadata` and `resized` are constants in the
  response mapper and say nothing.
- Every single-asset job handler looks its asset up by id with no check on
  `deletedAt` (`repositories/asset-job.repository.ts`), so metadata,
  thumbnails and ML run on a trashed asset regardless. Trashing a re-upload
  fast saves no work; it restarts the trash clock.

## Trash and albums

- `DELETE /assets` with `force: false` sets `deletedAt` and status trashed;
  the purge happens later per the trash retention setting. Trashed assets stay
  in their albums' rows and in dedupe.
- `POST /trash/restore/assets` brings a trashed asset back, memberships and
  metadata intact. Used when a member shares a photo the family had deleted.

## Search

- Metadata search as a member returns partners' assets too. Callers filter by
  owner. `createdAfter` is what the re-upload scan uses.

## Workflows and the webhook (`services/workflow-execution.service.ts`, `packages/plugin-core`)

- 3.3.1 has three triggers: AssetCreate, AssetMetadataExtraction,
  AssetTagged. No trigger for adding to an album or sharing one; the
  added-to-album trigger is PR 30524, open as of October 2026.
- The core plugin's "Trigger Webhook" action (`immich-plugin-core#webhook`)
  takes `url`, `method`, `headerName`, `headerValue`, and posts
  `{type, trigger, data: {asset}}` as JSON. The asset carries at least `id`
  and `ownerId`; the service treats both as hints and re-reads the asset with
  the owner's key. The call is fire-and-forget on Immich's side.
- The hosts a plugin may call come from its manifest, not a setting; the core
  plugin allows any host. Confirmed on dev: a re-upload was trashed 2.1s after
  the upload call returned.

## Faces (unverified)

Cluster groups (3.2) pool recognition across accounts. Whether a single new
upload clusters with existing cross-account faces, or only a full re-run does,
is untested; face detection is off on both instances. If only on a re-run,
the cluster group's regenerate-people endpoint is the fallback.

## Upstream references

- Ownership transfer requests: discussions 7885, 13334, 23776
- Shared album photos in timeline: discussion 26874
- Admin key can't delete other users' assets: issue 6788
- Mobile re-uploads after purge: issue 23897, discussion 4282
- `fileCreatedAt` overwritten by extraction: issue 18591
- Sync remote deletions: PR 16732, issue 20978
- Tags are per user: issue 22308, discussion 12845
- Workflows and webhooks: PR 29258 (merged); PR 30524 (added-to-album trigger, open)
- Cluster groups: v3.2.0 release notes. People sharing: v3.3.0 release notes, discussion 32196

## Related projects

Nothing else does this. Closest: donnchawp/immich-shared-library copies assets
and ML data between accounts by writing to Postgres directly (copy, not move;
pinned to Immich versions); Trust1509/immich-family-tools matches people
across accounts and builds shared albums, never moving assets;
alangrainger/immich-person-to-album and ajb3932/immich-partner-sharing populate
albums by face. Upstream's roadmap lists "better sharing" and "user groups"
with no dates, and a maintainer-adjacent comment suggests assets may one day
be library-owned with aliases. This project may become unnecessary. Not soon.
