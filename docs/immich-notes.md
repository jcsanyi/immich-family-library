# Immich notes

What the service relies on in Immich, as of 3.3.1, and how each point was
confirmed: in the server source (`server/src/...` at the `v3.3.1` tag), on
the dev instance, or both. These started as assumptions in the design. They
are listed here so that the next Immich upgrade knows what to re-check.

## Where to look

- The Immich API reference site is a JavaScript app and fetches as an empty
  page. Use the OpenAPI spec instead:
  https://raw.githubusercontent.com/immich-app/immich/main/open-api/immich-openapi-specs.json.
  Each path carries an `x-immich-permission` field that names the API key
  scope it needs.
- For behaviour the spec does not describe (job ordering, sidecar handling,
  what extraction overwrites), read the server source. Sidecar read and write
  live in `server/src/services/metadata.service.ts`.
- The GitHub discussions and issues under "Upstream references" below record
  what upstream has said about ownership transfer, the re-upload loop, and
  tags. Read them before you assume that something has changed.

## Access rules (`utils/access.ts`)

- Reading or downloading an asset is allowed for its owner, for anyone on an
  album that holds it, and for a partner of the owner.
- Edits, asset files (so the sidecar) and the per-asset metadata store are
  owner-only. That is why the move reads with the member's key and writes
  with the family's.
- Adding an asset to an album needs owner or editor on the album, and owner
  or partner on the asset (`AssetShare`). This is why the family
  partner-shares with every member: it is what lets a member put a family
  copy into their own album. Confirmed on dev with alice's and bob's keys.
- `POST /assets/copy` needs ownership of both assets, so it cannot copy a
  sidecar or album memberships across accounts.
- Queue endpoints (`/queues`) are admin-only. That is why the queue check
  needs an admin key.
- Workflows belong to a user and fire only for that user's assets.
- `GET /api-keys/me` needs no permission. A key can always read its own name
  and permissions, which is the startup check. The list of an account's keys
  (`GET /api-keys`) needs `apiKey.read`, and the service does not use it.
  Confirmed on dev: a key with only `user.read` got 200 from the first call
  and 403 from the second.

## Per-asset metadata store (`/assets/{id}/metadata`)

- Each asset has a key-value store. The owner reads and writes it through the
  API, with `asset.read` and `asset.update`. The service writes one key,
  `ifl`, on each family copy: the origin user id, the origin asset id, and
  the move time.
- The key column accepts any string. Immich defines one key of its own,
  `mobile-app`, which the phone app uses for an iCloud id. No page in the web
  app or the mobile app shows the store. Read it with the API, as the owner.
- An audit table beside the store feeds Immich's sync protocol, and Immich's
  own mobile app depends on the store. Both make its removal unlikely. A
  change to its semantics, for example a fixed list of keys, is possible. Check
  this section on each upgrade.
- The store is the one record of which original a copy came from. The
  `from/<member>` tag holds the member. The move reads the entry only as a
  veto: a copy whose entry names a different original is not this asset's. If
  the store changes, the fallback is a second tag on the copy,
  `ifl/<origin-asset-id>`, written by the move from then on. Older copies keep
  the member tag and lose the asset id.

## Sidecar (`services/metadata.service.ts`)

- Immich writes an XMP sidecar whenever the description, date, location,
  rating or tags change. It writes only the fields the user edited (its
  locked properties) plus the tag list (`TagsList`). An unedited photo's
  sidecar holds nothing but tags.
- On extraction, the sidecar is spread over the embedded tags, so every
  sidecar value wins. When the sidecar has a date, the embedded date tags are
  dropped. `TagsList` is preferred over Lightroom's `hierarchicalSubject` and
  Dublin Core's `subject`. All confirmed on dev: every edited field
  round-tripped, and a tagged original produced a copy with only our tag.
- Extraction never rewrites the sidecar. Tags read from `hierarchicalSubject`
  or `subject` are folded into `TagsList` only when a later sidecar write
  happens.
- A tag add fires the sidecar job at once: 0.1s on an idle dev instance.
- An edit does not move the asset's `updatedAt`. The sidecar job does,
  seconds later. So "recently updated" is false exactly while a write is
  pending, and the settle period cannot guard against a stale sidecar. The
  tag trigger does, for every edit made before the tag went on: a sidecar
  that holds the tag is newer than the tag.
- The tag trigger does not cover an edit made after the tag went on. Such an
  edit reaches the family copy only if Immich wrote it to the sidecar before
  the move read the sidecar. This applies to an edit made while a move runs.
  It also applies when the original already has the tag at the start, because
  the move then adds no tag and gets no new write. That occurs after a move
  that stopped partway, and when a member uses the same tag name. Immich has
  no call that writes the sidecar again, so the service accepts this limit.

## Uploads (`services/asset-media.service.ts`)

- The `filename` form field is applied to every multipart part. With a
  sidecar attached, the sidecar part was checked as a `.jpg` and rejected
  ("Unsupported file type"). The service sends no `filename` field. Each part
  names itself.
- The duplicate check is per owner, and trashed assets count. Re-uploading a
  moved photo is a no-op until the member's trash is emptied.
- `POST /assets/bulk-upload-check` answers whether an account has a checksum,
  and whether that asset is trashed. It is the resume check and the re-upload
  check.
- The edits endpoint refuses until the asset has dimensions, which extraction
  sets a few seconds after upload ("Asset dimensions are not available for
  editing"). The move waits for them before it applies edits.
- The asset response gives `duration` as an integer in milliseconds, and null
  for a still image. The upload endpoint takes the same integer. Confirmed on
  dev: a two-second clip reads as 2000 on the original and on the family copy.
  Older Immich versions gave a string, and the first move code expected one.

## Jobs

- No per-asset job status exists. `exifInfo` on the asset response appears
  once extraction has run. `hasMetadata` and `resized` are constants in the
  response mapper and say nothing.
- Every single-asset job handler looks its asset up by id with no check on
  `deletedAt` (`repositories/asset-job.repository.ts`). So metadata,
  thumbnails and ML run on a trashed asset regardless. Trashing a re-upload
  fast saves no work. It restarts the trash clock.

## Trash and albums

- `DELETE /assets` with `force: false` sets `deletedAt` and the status
  trashed. The purge happens later, per the trash retention setting. Trashed
  assets stay in their albums' rows and in dedupe.
- `POST /trash/restore/assets` brings a trashed asset back, with memberships
  and metadata intact. The service uses it when a member shares a photo the
  family had deleted.

## Search

- Metadata search as a member returns partners' assets too. Callers filter by
  owner. `createdAfter` is what the re-upload scan uses.
- A search by album with the family's key returns each member-owned asset
  with its `visibility` and its `livePhotoVideoId`. Hidden and archived assets
  are in the result. The asset scan uses these two fields to block live and
  hidden photos. Confirmed on dev.
- A search by album does not return a locked asset. A photo that was in a
  family album and then locked was gone from the search, for the family and
  for its owner. Confirmed on dev.
- A search with `visibility=locked` needs an elevated session. An API key gets
  401, "Elevated permission is required". Confirmed on dev.
- Immich refuses to add a locked asset to an album (`no_permission`).
  Confirmed on dev.

## Workflows and the webhook (`services/workflow-execution.service.ts`, `packages/plugin-core`)

- 3.3.1 has three triggers: AssetCreate, AssetMetadataExtraction,
  AssetTagged. There is no trigger for adding to an album or for sharing one.
  The added-to-album trigger is PR 30524, open as of October 2026.
- The core plugin's "Trigger Webhook" action (`immich-plugin-core#webhook`)
  takes `url`, `method`, `headerName` and `headerValue`. It posts
  `{type, trigger, data: {asset}}` as JSON. The asset carries at least `id`
  and `ownerId`. The service treats both as hints and re-reads the asset with
  the owner's key. The call is fire-and-forget on Immich's side.
- The hosts a plugin can call come from its manifest, not from a setting. The
  core plugin allows any host. Confirmed on dev: a re-upload was trashed 2.1s
  after the upload call returned.

## Mobile app (`mobile/lib`)

- The bottom tabs are Photos, Search, Albums and Library
  (`pages/common/tab_shell.page.dart`). Partners are listed on the Library
  tab (`presentation/pages/library.page.dart`). The partner's page has the
  "Show in timeline" switch (`presentation/pages/partner_detail.page.dart`).
  On the web, that switch is under Account Settings, Partner Sharing
  (`web/src/routes/(user)/user-settings/SharingSettings.svelte`). The
  partner's page on the web has no switch.
- "Sync remote deletions" is under Settings, Advanced, and is shown only on
  Android with SDK 31 (Android 12) or later
  (`widgets/settings/advanced_settings.dart`). There is no iOS equivalent.

## Faces (unverified)

Cluster groups (3.2) pool recognition across accounts. Whether a single new
upload clusters with existing cross-account faces, or only a full re-run
does, is untested (issue 1). If only a re-run does, the cluster group's
regenerate-people endpoint is the fallback.

## Upstream references

- Ownership transfer requests: discussions 7885, 13334, 23776
- Shared album photos in timeline: discussion 26874
- Admin key cannot delete other users' assets: issue 6788
- Mobile re-uploads after purge: issue 23897, discussion 4282
- `fileCreatedAt` overwritten by extraction: issue 18591
- Sync remote deletions: PR 16732, issue 20978
- Tags are per user: issue 22308, discussion 12845
- Workflows and webhooks: PR 29258 (merged), PR 30524 (added-to-album trigger, open)
- Cluster groups: v3.2.0 release notes. People sharing: v3.3.0 release notes, discussion 32196
