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
- Removing an asset from an album needs the `albumAsset.delete` permission.
  An editor on the album can remove their own asset. The asset stays in its
  other albums, and nothing else about it changes. Confirmed on dev with
  alice's key on a family album. Whether an editor can remove the asset of
  another user is not tested, and the service does not do it.
- An id that is not in the album comes back from the removal as
  `success: false` with the error `not_found`, and the album does not change.
  This is so for an asset that the caller owns and for an id that does not
  exist. Confirmed on dev with alice's key on a family album.
- `GET /albums?assetId=` returns only the albums that the caller owns or is
  on. A partner of an asset's owner can add the asset to an album that the
  owner is not on. The owner's key then does not list that album. This is why
  the move lists the albums with every participant's key. Confirmed on dev:
  bob added a photo of alice, his partner, to his own unshared album. Bob's
  key listed that album for the photo. Alice's key and the family's key did
  not. No key got an error.
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

## Reading one asset (`GET /assets/{id}`)

- A key that cannot read an asset gets a 400 with the message "Not found or no
  asset.read access". The answer is the same for an id that does not exist and
  for another user's asset, live or trashed. The service takes a 400 to mean
  "not this member's" and treats any other failure as an error. Confirmed on
  dev.
- A key that adds an asset to an album it has no access to gets a 400 with
  the message "Not found or no albumAsset.create access". Nothing is added.
  The re-link takes this answer to mean "try the next key". Confirmed on dev
  with the key of a member who is not on the album.
- A member's key can read a partner's asset. The family partner-shares with
  every member, so a member's key reads family assets. The caller checks the
  owner. Confirmed on dev.

## Per-asset metadata store (`/assets/{id}/metadata`)

- Each asset has a key-value store. The owner reads and writes it through the
  API, with `asset.read` and `asset.update`. The service writes one key,
  `ifl`, on each family copy: the origin user id, the origin asset id, and
  the move time.
- The store of a trashed asset stays readable. The move reads the `ifl` entry
  of a family copy in the trash before it restores that copy. Confirmed on
  dev with the family's key.
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
- The `checksum` of an uploaded asset is the SHA-1 of its original file, in
  base64. The move makes the same checksum from the file that it downloaded,
  and compares the two before the upload. Confirmed on dev with three photos.
  An asset in an external library is different. See "External libraries".
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
  family had deleted. Confirmed on dev: a restored original is again in every
  album it was in when it was trashed.
- A restore changes `updatedAt` of the asset. So a restored original waits
  for `settle_seconds` again before the move takes it. Confirmed on dev.
- A restore does not start the upload workflow, so the webhook gets nothing.
  Confirmed on dev: a restored original of alice's was in no album, the
  family held a copy that came from alice, and the webhook got no post.
- A trashed asset can be removed from an album, and it stays in the trash.
  Confirmed on dev:
  - The owner of the asset removed it from family albums, as an editor, and
    from an album of their own.
  - The owner of a member album removed another member's trashed asset from
    it.
  - The owner of the asset is refused for an album that they are not on. The
    answer is a 400 with "Not found or no albumAsset.delete access".
  - The family key is refused with a 403, because it does not have the
    `albumAsset.delete` permission. The family key never needs it: the owner
    of the asset is an editor on every family album.

## External libraries (`services/library.service.ts`)

The service does not move an asset that is in an external library. These
points are what it relies on to find one and to leave it alone.

- The asset response gives `libraryId` for an asset in an external library,
  and null for an uploaded asset. A search by album with the family's key
  returns the field. The asset scan and the move use it. Confirmed on dev.
- The `checksum` of such an asset is not a hash of its file. It is the SHA-1
  of the text `path:` and then the path of the file. Confirmed in the source
  and on dev: the value was equal to the hash of the path.
- Such an asset goes into an album as an uploaded asset does. Confirmed on
  dev: a conversion put one into the family album with the key of its owner,
  and the asset stayed with that owner.

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

## Faces and people (`services/person.service.ts`)

The points in this section were tested on dev with three accounts in one
cluster group: family, alice and bob. Carol, outside the group, was used for
one test of edited photos. The test photos came from Labeled Faces in the
Wild, at 250 × 250 pixels. That size was enough for detection and
recognition.

### Cluster groups and person groups

- A user joins a cluster group by invitation. A member of the group sends a
  request (`PUT /cluster-groups/{id}/requests`), and the invited user accepts
  it. Each user starts in a group of their own. Confirmed on dev.
- Cluster groups (3.2) pool recognition across accounts. Faces of one person
  in two accounts become one person group. Each account that has faces of the
  person gets its own person row, with the same `personGroupId`.
- Recognition adds the face in one new upload to a person group that another
  account already has. No re-run of recognition is necessary, and no people
  share is necessary. Confirmed on dev: one photo uploaded to bob joined a
  person group of the family. One photo uploaded to the family joined a
  person group of alice. Each face joined its group 2 seconds after
  detection, and the uploader got a new person row.
- A family copy that the move makes is such an upload. Thus its faces join the
  person group that the member already has. This test did not use the move.
- `GET /people` lists a person only when two conditions are true
  (`getVisiblePeopleQuery` in `repositories/person.repository.ts`):
  - The account can see a photo with a face of that person: its own photo, a
    partner-shared photo, or a photo in a shared album. Only photos in the
    timeline count. Archived, hidden and locked photos do not.
  - The person has a name, or has at least `people.minimumFaces` faces. This
    is a preference of each user, and it is 3 by default.
- `GET /people/{id}` returns a person that the list does not show. Confirmed
  on dev.
- The statistics of a person count the partner-shared photos too.

### People sharing (3.3)

- A share is for one person, from a sharer to a recipient, with a role:
  `read`, `write` or `admin`. The web app always shares with `admin`. Both
  users must be in the same cluster group.
- If the recipient has no row for the person, the share makes one. The new
  row gets the name and birth date of the sharer at that time. If the
  recipient has a row, the share does not change it. Confirmed on dev.
- "Share everyone" (`type: everyone`) shares the people that the account has
  at that time. Immich does not keep it as a setting. People that recognition
  finds later are not shared. The toggle in the web app shows "on" only when
  every current person is shared. Confirmed on dev.

### Edits through a share

- Each account keeps its own name, birth date and hidden flag for a person.
- An edit of the name or birth date changes the copy of the editor. It also
  changes the copy of each user who shares that person with the editor as
  `write` or `admin` (`updateForWritableOwners`). The change goes no further.
- Confirmed on dev, with shares only from the family to alice and bob:
  - A name from alice reached the family. It did not reach bob.
  - A name from the family reached nobody.
- Confirmed on dev, with shares in both directions between the family and
  each member:
  - A name from the family reached alice and bob.
  - A name from alice reached the family. It did not reach bob.
  - The family saved alice's name again, with no change, in the web app. Bob
    then got the name.
- `PUT /people/{id}` with `userId` changes the copy of that user. It accepts
  only the name and birth date. That user must share the person with the
  caller as `write` or `admin`. Confirmed on dev.
- A birth date moves through a share as a name does. The hidden flag stays
  with the account that sets it. Confirmed on dev.
- The web app shows the name of another user as "aka" below "Add a name". It
  does not show their birth date. Confirmed on dev, in the web app as bob.
- The preference `people.updateStrategy` is "everyone" by default. When a user
  sets it to "self", the web app sends their own `userId`. Their edits then
  change only their own copy. This is from the web and server source only.

### Detection and recognition

- Detection reads the preview of the asset, not the original. Each face keeps
  its box in the pixels of that preview, with the width and height of the
  preview.
- Detection is deterministic. The same file in two accounts gave the same
  faces, with the same boxes to the pixel. Confirmed on dev with 3 photos and
  8 faces.
- Recognition is not deterministic across accounts. The person of a face
  depends on the faces that the cluster group has at that time. Confirmed on
  dev: a face in the same file had a person in alice's copy and no person in
  the family's copy.
- Two copies of one file count as two faces toward the threshold. Thus a copy
  can make a person for a face that had too few matches before.
- Two settings control how many faces a person needs:
  - `machineLearning.facialRecognition.minFaces` is an admin setting, 3 by
    default. Recognition makes a person only when it finds this many similar
    faces.
  - `people.minimumFaces` is a preference of each user. It only controls
    which unnamed people the list shows.
- Recognition skips a face that has a person, and a face that is not from
  machine learning. Thus an assignment that a user or the service makes stays
  in place. A face with too few matches is deferred, and a later recognition
  job can give it a person.
- After an upload, detection is queued when thumbnail generation ends
  (`services/job.service.ts`). Detection skips a hidden asset.
- The API does not tell when detection is done on an asset. Detection sets
  `asset_job_status.facesRecognizedAt`, but no endpoint returns it for one
  asset. No event or workflow trigger comes after detection. An asset that
  has no faces and an asset that is not detected yet look the same.
- The web app shows only faces that have a person. The "Edit people" button
  (a pencil, next to "Tag People") shows all the faces of a photo, and can
  give a person to a face that has none. Confirmed on dev.

### Edited photos

Immich 3.3.1 and 3.3.3 have a fault with faces on photos that were edited in
Immich. Issue 32499 reports it, with the steps to reproduce it. Before
PR 31240, detection did not run on an edited photo.
That PR made detection read the edited preview, but it does not map the boxes
back to the original.

- An edit does not run detection again. It makes new edited thumbnails only.
  The faces keep their boxes in the coordinates of the original. The API maps
  each box through the edits when it returns the face. Confirmed on dev with a
  crop and a rotation.
- Detection uses the edited preview when one exists. It stores the new boxes
  in the coordinates of the edited image. The API then maps them through the
  edits a second time, so the boxes show in the wrong place. Confirmed on dev:
  a face in an 80 × 80 crop of a 250 × 250 photo showed about 3 times too
  large, and mostly outside the photo.
- The new boxes do not match the old ones, so detection removes all the old
  faces from machine learning, with their people. Confirmed on dev.
- Thus the fault occurs when detection runs after an edit. Examples are the
  first detection on a library that has edited photos, the admin job for
  detection on all assets, and "Refresh faces" on one photo.
- A copy that is edited soon after upload is in a race. On idle dev, detection
  read the unedited preview about 3 seconds before the edited preview was
  ready, so the faces were correct. When the machine learning queue is long,
  the edited preview is ready first.

### Changing faces through the API

These points were confirmed on dev with the family's password session, on a
copy that was not edited.

- `PUT /faces/{id}` gives a person to a face. The `id` in the path is the
  person. The body holds the face id.
- An account can give a face only to a person that it has a row for. If not,
  the answer is a 400 with "Not found or no person.update access". A people
  share from the user who has the person makes the row.
- `DELETE /faces/{id}` with `force: false` marks the face as deleted.
  Detection matches deleted faces too, so a later run does not add the face
  again. The other faces keep their people. With `force: true`, the row is
  removed, and the source shows that the next run adds the face again.
- `POST /faces` makes a face with `sourceType: manual`. It needs a person. It
  takes the box in the coordinates of the asset as the user sees it, after
  the edits. It stores the box in the coordinates of the original. Confirmed
  on dev, before and after a crop.
- Detection does not add a second face where a face already is. A new box
  that matches an existing face gives its embedding to that face.

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
- "Share everyone" is not kept: issue 32049 (moved to discussion 32050), discussion 32084
- Shared names show only as "aka": issue 32273
- Faces on edited photos: issue 32499, issue 31584 (closed as a duplicate of
  31106), issue 31106, PR 31240 (merged, detection reads the edited preview),
  PR 29393 (closed, also mapped the boxes back)
- Detection does not match faces imported from metadata on large originals:
  issue 32076
