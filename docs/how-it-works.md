# How it works

The model behind the service, and what each part does. The member-facing
version is [for-members.md](for-members.md); the operator's reference for
settings and commands is [configuration.md](configuration.md) and
[cli.md](cli.md).

## Accounts

Every member has their own Immich user. One extra user, the family account,
owns everything that's been shared. The family account partner-shares its
library with every member; whether a member shows that in their timeline is
their own setting, and nothing here depends on it. Every participating member
gives the service an API key; so does the family account. Members without a
key can still share albums with the family user, but nothing of theirs is
converted or moved, and the service says so.

## The one rule

A member-owned photo sitting in a family-owned album gets moved to the family
account. Everything else, album conversion, the dropbox, adding to a family
album, is a way of getting a photo into that position. There is one move path,
and all the care about state and retries lives there.

An album counts as shared with the family when the family user is on its
shared-user list. That's the only signal. Albums shared between members with
the family not on them are left alone.

## The three scans

A pass is three scans in a fixed order, each acted on before the next runs:

1. **Albums.** Member-owned albums that have the family user on them are
   converted. Running this first means the asset scan in the same pass already
   sees the converted album as family-owned.
2. **Family albums.** The consistency invariant is restored (below).
3. **Assets.** Member-owned photos in family-owned albums, plus re-uploads
   (below), are moved.

`observe` runs the same three scans and only reports. `process` runs them and
acts. The daemon runs `process` on an interval.

## Album conversion

For a member album with the family user on it:

1. Precheck. Keys exist and work for the owner and for every member who
   contributed a photo; nobody outside the family contributed; the album has
   no public shared link. Anything else leaves the album alone, with a reason,
   and it's looked at again next pass.
2. The family account gets an album of the same name and description, or uses
   the one it already has with that name. Two albums sharing a name merge.
3. Every configured member is added as an editor. Anyone else the original
   was shared with is carried over at their original role, so nothing changes
   from their side.
4. Each contributor adds their own photos to the family album with their own
   key. Editors can add their own photos; that's all it takes.
5. The original is re-read once for anything added meanwhile, and the server
   is asked whether every photo is in the family album now.
6. The original album is deleted. Only the album; the photos are still the
   member's, now sitting in a family album, which is what the one rule acts on.

Every step is idempotent, so a conversion that died partway is finished by
running it again.

## Family album consistency

An invariant kept on every pass rather than something triggered:

- the dropbox album exists, owned by the family;
- every configured member is an editor on every family-owned album, promoted
  if they're only a viewer;
- the family account partner-shares its library with every member;
- with a webhook configured, every member has the Immich workflow that posts
  their uploads to it.

It never removes anyone from anything. Taking a member out of the family is a
manual job, since it also means deciding what happens to their photos.

## The move

Everything is read with the owner's key, since edits, asset files and the
sidecar are owner-only in Immich, and everything is written with the family's.

**Before touching it.** The photo must have finished metadata extraction and
not have changed within `settle_seconds`; it must not be locked or hidden, a
live photo (not handled yet), in one of the owner's public shared links, or
in an album owned by someone outside the family that the owner can no longer
edit. Any of those blocks it for this pass, with a reason, and it's found
again next time. Two things only warn: a photo in a stack (the stack loses it)
and an owner who partner-shares with someone outside the family (that person
stops seeing what the owner gives to the family).

**The sidecar.** Immich keeps a photo's Immich-side edits, description, date
and zone, location, rating, in an XMP sidecar it writes itself whenever those
change. The service doesn't write that file; it asks Immich to. Tagging the
original with the moved tag (`family`) fires Immich's sidecar job, and the
service waits until the tag shows up in the sidecar, which means it's current.
Then it changes one thing: the tag list becomes the single entry
`from/<member>`. Immich's extraction prefers that list over any keywords in
the file, so the family copy ends up with exactly that tag and none of the
member's.

**The upload.** The family's bulk upload check says whether the family already
has the checksum. If not, the original bytes go up with the rewritten sidecar,
the original's dates, favourite flag and visibility. Extraction on the copy
reads the sidecar on its first pass, so there's nothing to fix afterwards.

**Fix-ups.** The original's crop, rotate and mirror edits are applied to the
copy verbatim; the bytes are identical, so the coordinates hold. This is the
one step that waits on an Immich job, because the edits endpoint needs the
copy's dimensions, which extraction sets within seconds. An `ifl` entry in the
copy's metadata store records who it came from and from which asset.

**Re-linking.** The copy is added to every album the original was in: family
albums with the family key, except the dropbox; member-owned albums with the
album owner's key, which works because the family partner-shares with them,
falling back to the asset owner's key where they're an editor. A re-link that
fails under every eligible key stops the move before the trash, so a photo
never quietly vanishes from an album.

**The trash.** The original is trashed with the owner's key, never deleted
outright, and the ledger records the move.

State comes from Immich at every step: the family owning the checksum means
the upload happened, the original still existing means it hasn't been trashed,
the fix-ups are idempotent. A move that died anywhere is finished by running
it again.

## The dropbox

One family album, named in the config, that behaves like any other except the
copy isn't added back to it. Use it for photos that belong in the family
timeline and in no album.

## Re-uploads

The mobile app backs up by asking the server which files it already has. Once
a moved original leaves the member's trash, the phone no longer finds it and
uploads it again. Immich doesn't remember intentionally deleted hashes, and
upstream has no fix.

What the service does about it:

- Originals are trashed, never deleted, so the phone leaves them alone until
  the trash purges, 30 days by default. Members are asked to turn on the
  app's "Sync remote deletions" setting, which makes the phone drop its local
  copy while the original is still in the server trash. That ends the loop
  for that photo. The service never force-empties trash, for exactly this
  reason.
- The asset scan has a second source: per member, photos uploaded since the
  last look (a per-member mark in the ledger) with their checksums batched
  through the family's bulk check. Matches are handled by the same move: the
  copy is found by checksum, nothing is uploaded, the copy is re-linked into
  any album the duplicate sits in, the duplicate is trashed, and the ledger
  counts it.
- A duplicate is told from a crashed move by the family copy's `ifl` entry
  naming a different original. A duplicate leaves the copy's edits and
  provenance alone, and skips the extraction and settle checks, since it needs
  nothing the new upload's jobs produce.
- A match whose family copy is itself in the trash is left alone: the family
  deleting a photo is a signal, not something to answer by trashing the
  member's. But a photo the family deleted that a member then shares into a
  family album again restores the family's copy from the trash, rather than
  uploading it anew.
- Re-uploads are counted per checksum and per member. A member with the
  setting on produces none. Above `reuploads_per_pass` in one pass, the
  member is named in a warning.

Trashing a re-upload quickly doesn't save Immich any work: every job already
queued for it runs regardless. It restarts the 30-day clock and gives the
phone's setting something to act on.

## What's carried, what's lost

Carried on the family copy: everything in the file; description, corrected
date and zone, location and rating through the sidecar; favourite and
visibility; crop, rotate and mirror; faces, once face detection is on, through
the cluster group. The copy gets one tag, `from/<member>`.

Lost: the member's own tags, on purpose; likes and comments on album photos;
manual face corrections and stacks, for now (see the issues).

## Provenance

Two marks on each family copy. The `from/<member>` tag is for people: tags are
per account, so it's visible to whoever browses as the family. The `ifl`
metadata entry, with the origin user id, origin asset id and move time, is for
the service, and the ledger can be rebuilt from it. The description is the one
field members can see on a partner's photo, and the service never writes into
it.

## State

Immich is the source of truth for anything in flight. The ledger is one SQLite
file and a record, not a controller: what moved, from whom, to what, when,
plus the re-upload count per checksum and the per-member scan marks. It
exists for the two things Immich can't tell us once an original is purged,
who a family photo came from and who is looping, and it's the audit trail.
Config labels are never stored; renaming a member in the config changes how
messages read and nothing else. The one place a label lands in Immich is the
`from/<member>` tag.

The daemon's pass and its webhook receiver share a set of in-flight asset ids
so they never work on the same photo at once; whoever is second skips, and a
skipped re-upload holds its member's mark back so it's found again.

## Pacing

Two caps in the config, `albums_per_pass` and `assets_per_pass`, bound what
one pass does; the daemon's `interval_seconds` turns them into a rate. That's
the brake on the storage spike during an initial conversion, when trashed
originals briefly double the space a converted album takes. With an admin key
configured, a pass skips its moves while Immich's sidecar or metadata queues
have a backlog, since the sidecar step would wait behind them.

The webhook never wakes the pass. It only handles re-uploads, which move no
bytes and so aren't what the caps meter; everything else waits for the
interval.
