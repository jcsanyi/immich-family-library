# How it works

The model behind the service, and what each part does. For the member's view
read [for-members.md](for-members.md). For settings and commands read
[configuration.md](configuration.md) and [cli.md](cli.md).

## Accounts

Every member has their own Immich user. One extra user, the family account,
owns everything the members have shared. It partner-shares its library with
every member. Whether a member shows that library in their timeline is their
own setting, and nothing here depends on it.

The family account and every participating member give the service an API
key. A member without a key can still share an album with the family user,
but the service converts and moves nothing of theirs, and says so.

## The one rule

The service moves any member-owned photo it finds in a family-owned album to
the family account. Album conversion, the dropbox, and adding to a family
album are just ways of getting a photo into that position.
There is one move path, and all the care about state and retries lives there.

An album is shared with the family when the family user is on its shared-user
list. Nothing else counts. Albums that members share among themselves, with
the family user not on them, are none of the service's business.

## The three scans

A pass is three scans in a fixed order, and the service acts on each before
it starts the next:

1. **Albums.** Member-owned albums with the family user on them get converted.
   This runs first so the asset scan later in the same pass already sees the
   converted album as family-owned.
2. **Family albums.** The consistency invariant, below, is restored.
3. **Assets.** Member-owned photos in family-owned albums get moved. So do
   re-uploads, below.

`observe` runs the same three scans and only reports. `process` runs them and
acts. The daemon runs `process` on an interval.

## Album conversion

For a member album with the family user on it:

1. Precheck. The owner and every member who contributed a photo have keys that
   work, nobody outside the family contributed, and the album has no public
   shared link. If any of that fails, the album is left alone with a reason,
   and the next pass looks again.
2. The family account gets an album with the same name and description. If it
   already has one by that name, that one is used, so two albums sharing a
   name merge.
3. Every configured member becomes an editor. Anyone else the original was
   shared with is carried over at their original role, so nothing changes from
   their side.
4. Each contributor adds their own photos to the family album, with their own
   key. Editors can add their own photos, and that's all it takes.
5. The service re-reads the original once, in case anything was added in the
   meantime, then asks the server whether every photo is now in the family
   album.
6. The original album is deleted. Only the album: the photos are still the
   member's, now sitting in a family album, which is exactly what the one rule
   acts on.

Every step is idempotent. A conversion that died partway finishes on the next
run.

## Family album consistency

This is an invariant the service restores on every pass, not something a
trigger fires:

- the dropbox album exists, owned by the family;
- every configured member is an editor on every family-owned album, and a
  member who's only a viewer gets promoted;
- the family account partner-shares its library with every member;
- with a webhook configured, every member has the Immich workflow that posts
  their uploads to it.

So an album someone makes by hand in the family account, or a member added
later, needs no manual sharing.

The service never removes anyone from anything. Taking a member out of the
family is a manual job, because it also means deciding what happens to their
photos.

## The move

Everything is read with the owner's key, because edits, asset files and the
sidecar are owner-only in Immich. Everything is written with the family's.

**Before touching it.** The photo has to have finished metadata extraction and
sat unchanged for `settle_seconds`. It can't be locked or hidden, a live photo
(not handled yet), in one of the owner's public shared links, or in an album
owned by someone outside the family that the owner can no longer edit. Any of
those blocks it for this pass, with a reason, and the next pass finds it
again. Two things only warn: a photo in a stack (the stack loses it), and an owner
who partner-shares with someone outside the family (that person stops seeing
what the owner gives to the family).

**The sidecar.** Immich keeps a photo's Immich-side edits, meaning its
description, date and zone, location and rating, in an XMP sidecar that it
writes itself whenever those change. The service never writes that file. It
gets Immich to, by adding the moved tag (`family` by default) to the original,
which fires the sidecar job. The service waits until the tag shows up in the
sidecar, which means the sidecar is current. Then it changes one thing: the tag list becomes the single
entry `from/<member>`. Extraction prefers that list over any keywords in the
file, so the family copy ends up with exactly that tag and none of the
member's. Both tag names are set in `[tags]`.

**The upload.** The family's bulk upload check says whether the family already
has the checksum. If not, the original bytes go up with the rewritten sidecar,
the original's dates, its favourite flag and its visibility. Extraction on the
copy reads the sidecar on its first pass, so there's nothing to fix afterwards.

**Fix-ups.** The original's crop, rotate and mirror edits are applied to the
copy verbatim. The bytes are identical, so the coordinates hold. This is the
one step that waits on an Immich job: the edits endpoint needs the copy's
dimensions, and extraction sets those within seconds. An `ifl` entry in the
copy's metadata store records who it came from and from which asset.

**Re-linking.** The copy goes into every album the original was in. Family
albums get it with the family key, except the dropbox. Member-owned albums get
it with the album owner's key, which works because the family partner-shares
with them. If that owner has no configured key, or the call fails, the asset
owner's key is tried where they're an editor. If every eligible key fails, the
move stops before the trash. A photo never quietly vanishes from an album.

**The trash.** The original is trashed with the owner's key, never deleted
outright, and the ledger records the move.

State comes from Immich at every step. The family owning the checksum means
the upload happened. The original still existing means it hasn't been
trashed. The fix-ups are idempotent. A move that died anywhere finishes on the
next run.

## The dropbox

One family album, named in the config, that behaves like any other except the
copy isn't added back to it. It's for photos that belong in the family
timeline and in no album.

## Re-uploads

The mobile app backs up by asking the server which files it already has. Once
a moved original leaves the member's trash, the phone can't find it any more
and uploads it again. Immich doesn't remember intentionally deleted hashes,
and upstream has no fix.

What the service does about it:

- Originals are trashed, never deleted, so the phone leaves them alone until
  the trash purges, 30 days by default. Members are asked to turn on the
  app's "Sync remote deletions" setting, which makes the phone drop its local
  copy while the original is still in the server trash. That ends the loop
  for that photo. The service never force-empties trash, for exactly this
  reason.
- The asset scan has a second source: per member, the photos uploaded since
  the last look, with their checksums batched through the family's bulk
  check. The last look is a per-member mark in the ledger. A match goes
  through the same move: the copy is found by checksum, nothing is uploaded,
  the copy is re-linked into any album the duplicate sits in, the duplicate
  is trashed, and the ledger counts it.
- The family copy's `ifl` entry tells a duplicate from a crashed move: a
  duplicate's entry names a different original. A duplicate leaves the copy's edits and
  provenance alone, and skips the extraction and settle checks, since it needs
  nothing the new upload's jobs produce.
- A match whose family copy is itself in the trash is left alone. The family
  deleting a photo is a signal, not something to answer by trashing the
  member's. But if a member shares that photo into a family album again, the
  service restores the family's copy from the trash rather than uploading it
  anew.
- The service counts re-uploads per checksum and per member. A member with the
  setting on produces none. A member who passes `reuploads_per_pass` in one
  pass gets a warning naming them.

Trashing a re-upload quickly saves Immich no work, since every job already
queued for it runs regardless. What it does is restart the 30-day clock and
give the phone's setting something to act on.

## What's carried, what's lost

Carried on the family copy: everything in the file; description, corrected
date and zone, location and rating, through the sidecar; favourite and
visibility; crop, rotate and mirror; and faces, once face detection is on,
through the cluster group. The copy gets one tag, `from/<member>`.

Lost: the member's own tags, on purpose; likes and comments on album photos;
and, for now, manual face corrections and stacks (see the issues).

## Provenance

Each family copy carries two marks. The `from/<member>` tag is for people,
since tags are per account and so it's visible to whoever browses as the
family. The `ifl` metadata entry, with the origin user id, origin asset id
and move time, is for the service, and the ledger can be rebuilt from it. The
description is the one field members can see on a partner's photo, and the
service never writes into it.

## State

Immich is the source of truth for anything in flight. The ledger is one SQLite
file, and it's a record, not a controller: what moved, from whom, to what,
when, plus the re-upload count per checksum and the per-member scan marks. It
exists for the two things Immich can't tell us once an original is purged,
namely who a family photo came from and who is looping, and it's the audit
trail. Config labels are never stored. Renaming a member in the config changes
how messages read and nothing else. The one place a label lands in Immich is
the `from/<member>` tag.

The daemon's pass and its webhook receiver share a set of in-flight asset ids,
so they never work on the same photo at once. Whoever is second skips, and a
skipped re-upload holds its member's mark back so it's found again.

## Pacing

Two caps in the config, `albums_per_pass` and `assets_per_pass`, bound what
one pass does, and the daemon's `interval_seconds` turns them into a rate.
That's the brake on the storage spike of an initial conversion, when trashed
originals briefly double the space a converted album takes. After that, the
trash holds a rolling month of new family photos. With an admin key
configured, a pass skips its moves while Immich's sidecar or metadata queues
have a backlog, since the sidecar step would wait behind them.

The webhook never wakes the pass. It handles re-uploads alone, which move no
bytes and so aren't what the caps meter. Everything else waits for the
interval.
