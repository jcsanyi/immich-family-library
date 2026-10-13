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

## What the service moves

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
5. The original album is deleted, once Immich has accepted every photo. Only
   the album: the photos are still the member's, now sitting in a family
   album, which is exactly what the service moves.

Every step is idempotent. A conversion that died partway finishes on the next
run.

The service reads the original once. A photo that someone adds to the original
after that read, and before the delete, is not carried over. The window is
short: the read, one add request per contributor, and the delete. Immich has
no way to lock an album or to delete it only if it is unchanged. The photo is
not lost: it stays in its owner's library, in no album, and the owner can add
it to the family album.

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
sidecar, which means the sidecar is current. Then it changes one thing: the
tag list becomes the single entry `from/<member>`. Extraction prefers that
list over any keywords in the file, so the family copy ends up with exactly
that tag and none of the member's. Both tag names are set in `[tags]`.

What happens to an edit made while the photo is being moved isn't defined. The
photo still moves. Whether that last edit reaches the family copy depends on
whether Immich had written it to the sidecar by the time the move read it. If
it hadn't, the edit stays behind on the original in the trash.

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
outright.

A member who restores a moved original from their trash doesn't get the photo
back. A trashed photo keeps its place in its albums, so the restored original
is once more a member's photo in a family album, and the next pass trashes it
again. The family's copy isn't touched: its `ifl` entry names that original,
which says the edits were already carried, so they aren't carried twice.

State comes from Immich at every step. The family owning the checksum means
the upload happened. The original still existing means it hasn't been
trashed. The copy's `ifl` entry naming the original means the fix-ups
happened, since it's written last of them. A move that died anywhere finishes
on the next run.

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
  the trash purges, 30 days by default. Members on Android are asked to turn
  on the app's "Sync remote deletions" setting, which makes the phone drop
  its local copy while the original is still in the server trash. That ends
  the loop for that photo. The setting exists only on Android 12 or later, so
  an iPhone keeps looping and the service keeps trashing. The service never
  force-empties trash, for exactly this reason.
- The asset scan has a second source: per member, the photos uploaded since
  the last look, with their checksums batched through the family's bulk
  check. The last look is a per-member mark in the state file. A match goes
  through the same move: the copy is found by checksum, nothing is uploaded,
  the copy is re-linked into any album the duplicate sits in, the duplicate
  is trashed, and it joins the member's daily tally.
- A member's first scan reads nothing. Before the service has moved anything
  of theirs, nothing they hold can be a re-upload, so the mark simply starts
  at that pass. Photos they and the family both held from before are left
  where they are. The same goes for a lost state file: re-uploads from the gap
  are never looked for, and sit as harmless duplicates in the member's
  library.
- The moved tag tells a duplicate from a crashed move. The service puts that
  tag on an original before it uploads, so an original wearing it is a move
  that died partway, and the member's photo is the one to finish from. An
  original without it is a re-upload, or a photo the family got some other
  way, and the family's copy is the one that counts: its edits and provenance
  are left alone, and the move skips the extraction and settle checks, since
  it needs nothing the new upload's jobs produce. The copy's `ifl` entry has
  the last word: if it names a different original, the copy already has its
  owner, whatever tag the member's photo wears.
- A match whose family copy is itself in the trash is left alone. The family
  deleting a photo is a signal, not something to answer by trashing the
  member's. But if a member shares that photo into a family album again, the
  service restores the family's copy from the trash rather than uploading it
  anew.
- A match whose family copy is gone for good by the time the move runs is
  left alone too. With no copy it's the member's own photo again, and only a
  family album makes a photo the family's. The scan stops matching it at the
  next pass.
- The service tallies trashed re-uploads per member, from the pass and from
  the webhook alike. A re-upload here means what the re-upload scan or the
  webhook found: an upload a phone made on its own. A photo a member puts
  into a family album by hand is a move or a re-share, never a loop, so it
  isn't counted even when the family already has it. A member with the
  setting on produces none. Once a day, at `report_at`, one warning per member
  says how many of their photos were re-uploaded and trashed in the last day.
  A member with none gets no line. The tally is what a nudge to the member
  will one day be built on; a loop needs a nudge, not a pager.

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

A converted album keeps its name, description, photos and users. It loses its
cover, its sort order and its activity setting, which go back to Immich's
defaults (see the issues).

## Provenance

Each family copy carries two marks. The `from/<member>` tag is for people,
since tags are per account and so it's visible to whoever browses as the
family. The `ifl` metadata entry, with the origin user id, origin asset id
and move time, is for the service, and it's the one record of where a copy
came from. The description is the one field members can see on a partner's
photo, and the service never writes into it.

## State

Immich is the source of truth for everything. Where a copy came from is in its
`ifl` entry, and whether a move is finished is in what Immich holds. The
service keeps one small JSON file, the state file, and nothing in it is needed
for correctness. It holds each member's re-upload scan mark, each member's
tally of trashed re-uploads since the last daily report, and when that report
last ran. Losing it means the marks start again at the next pass and one
report is dropped. The file never stores config labels. Immich stores a label
in one place only, the `from/<member>` tag. Apart from that tag, renaming a
member in the config changes only how messages read.

A member taken out of the config is dropped from the state file the next time
the daemon, `process` or `move` starts. Added back later, they start as a new
member does: their mark begins at that pass, and nothing from the gap is
trashed. The file isn't meant to be edited by hand.

Only one command that writes to Immich runs at a time. Two at once could each
look for a family album, find none, and both create it, and the same goes for
the dropbox and the members' workflows. They could also both work on one
photo. So every writing command takes the run lock first: the daemon for as
long as it runs, a one-off command for its own length. A second one is refused
before it touches anything. `observe` writes nothing and never takes it.

The run lock is a file, and it sits beside the state file only because that
directory is the one place the service can write. It isn't a lock on the state
file. But it has that effect: whoever holds it is the state file's only
writer, so the file needs no locking of its own.

The daemon's pass and its webhook receiver share a set of in-flight asset ids,
so they never work on the same photo at once. Whoever is second skips, and a
skipped re-upload holds its member's mark back so it's found again.

Any unfinished re-upload holds the mark the same way: one that's blocked,
failed, or left over the per-pass cap. The next pass re-reads that member's
uploads from the oldest unfinished one onward, so a re-upload that stays
blocked, say in one of the member's shared links, means the scan re-reads
that stretch of their library every pass until the block clears. Nothing is
missed, but the work grows with their uploads since then, and the pass log
says whose mark is held and from when.

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
