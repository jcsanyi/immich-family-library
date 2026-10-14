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
   re-uploads, below. Photos from an external library are the exception:
   they're never moved, and any in the dropbox are taken back out.

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
owned by someone outside the family that nobody with a key can add to. Any of
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

The bytes travel by way of a file, never through memory, so a long video costs
disk space and not RAM. The service downloads the original into its temp
directory, checks the file against the checksum Immich holds for the photo,
and uploads from the file. A download that doesn't match stops the move there,
because the original is about to be trashed on the strength of that upload.
The file is deleted as soon as the upload ends, whether it worked or not.

**Fix-ups.** The original's crop, rotate and mirror edits are applied to the
copy verbatim. The bytes are identical, so the coordinates hold. This is the
one step that waits on an Immich job: the edits endpoint needs the copy's
dimensions, and extraction sets those within seconds. An `ifl` entry in the
copy's metadata store records who it came from and from which asset.

**Re-linking.** The copy goes into every album the original was in. The
service asks for those albums with every key it holds, because the photo's
owner can't see them all: a partner can put the photo into a private album of
their own. Family albums get the copy with the family key, except the dropbox.
Any other album gets it with the key of someone who owns that album or is an
editor on it: the album's owner first, then the photo's owner, then anyone
else with a key. That works because the family partner-shares with every
member. If every eligible key fails, the move stops before the trash. A photo
never quietly vanishes from an album the service can see.

One kind of album is out of reach: one that only people outside the family
are on. No key lists it, so the service can't know the photo is there, and
the photo leaves that album when the original goes to the trash.

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

## External libraries

The service isn't meant for photos in an Immich external library, and it
doesn't support moving them. An external library is a folder of the member's
own that Immich reads in place. A photo from one is never moved to the family
account.

It can still be shared with the family:

- In a family album, it stays. Everyone on the album sees it there, as they
  would any album photo. It's still the member's, so it isn't in the family's
  library, and partner sharing doesn't bring it into anyone's timeline.
- In a member's album that gets shared with the family, conversion carries it
  into the family album with the rest, and it stays there in the same way.
  The other photos in the album move as usual.

The asset scan passes over such a photo without a word. It isn't a block,
since nothing the member does would change it.

The dropbox is where this doesn't work. The dropbox puts a photo into the
family's library and into no album, and that can't happen here. So a pass
takes an external-library photo back out of the dropbox, with its owner's key.
The photo itself isn't touched, and it stays in any other album it's in. Each
member whose photos came out gets one warning for the pass, with the count.
Nothing is uploaded, so no cap applies.

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
  check. A match is a re-upload only when the family's copy came from that
  same member, which the copy's `ifl` entry says. It then goes through the
  same move: the copy is found by checksum, nothing is uploaded, the copy is
  re-linked into any album the duplicate sits in, the duplicate is trashed,
  and it joins the member's daily tally.
- A family copy that came from somewhere else makes no claim on a member's
  photo. The family may have uploaded the file itself, or got it from another
  member who also had it. Either way the member's photo stays where it is,
  and the scan says it skipped it. Only a family album makes a photo the
  family's: if the member puts theirs into one, it's moved like any other,
  and the family's existing copy stands in for it.
- The last look is a mark the daemon keeps in memory, and the scan never reads
  further back than three days. A daemon that has just started has no mark,
  so its first pass reads all three days, and so does every one-off `process`,
  `move` and `observe`. A daemon that was down for longer than that leaves a
  gap: re-uploads from the gap are never looked for, and sit as harmless
  duplicates in the member's library.
- The moved tag tells a duplicate from a crashed move. The service puts that
  tag on an original before it uploads, so an original wearing it is a move
  that died partway, and the member's photo is the one to finish from. An
  original without it is a re-upload, or a photo the family got some other
  way that has now been put into a family album, and the family's copy is the
  one that counts: its edits and provenance
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
- The daemon tallies trashed re-uploads per member, from the pass and from
  the webhook alike. A re-upload here means what the re-upload scan or the
  webhook found: an upload a phone made on its own. A photo a member puts
  into a family album by hand is a move or a re-share, never a loop, so it
  isn't counted even when the family already has it. A member with the
  setting on produces none. Once a day, at `report_at`, one warning per member
  says how many of their photos were re-uploaded and trashed since the last
  report. A member with none gets no line. The tally lives in the daemon's
  memory. A daemon that is stopped reports what it has counted before it
  exits, and one that crashes loses it. Either way the next report covers
  only the time since the restart. A one-off `process` or `move` keeps no
  tally: its summary line gives the count. The tally is what a nudge to the
  member will one day be built on; a loop needs a nudge, not a pager.

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
service keeps nothing on disk between runs.

The daemon remembers three things, in memory only: the re-upload scan's mark,
each member's tally of trashed re-uploads, and when it last reported them.
None of them is needed for correctness. A restart costs one read of the
scan's three days, and it brings the day's report forward to the moment of
the stop. Immich
stores a config label in one place only, the `from/<member>` tag. Apart from
that tag, renaming a member in the config changes only how messages read.

Only one command that writes to Immich runs at a time. Two at once could each
look for a family album, find none, and both create it, and the same goes for
the dropbox and the members' workflows. They could also both work on one
photo. So every writing command takes the run lock first: the daemon for as
long as it runs, a one-off command for its own length. A second one is refused
before it touches anything. `observe` writes nothing and never takes it. The
run lock is a file, and the only one the service keeps.

The daemon's pass and its webhook receiver share a set of in-flight asset ids,
so they never work on the same photo at once. Whoever is second skips, and a
skipped re-upload holds the mark back so it's found again.

Any unfinished re-upload holds the mark the same way: one that's blocked,
failed, or left over the per-pass cap. The mark stays where it was, and the
pass log says so. While a re-upload stays blocked, say in one of the member's
shared links, every pass re-reads every member's uploads from the held mark
onward, so the read grows until the block clears. It can't grow past three
days: by then the scan no longer finds that re-upload, and the mark moves
again.

## Pacing

Two caps in the config, `albums_per_pass` and `assets_per_pass`, bound what
one pass does, and the daemon's `interval_seconds` turns them into a rate.
That's the brake on the storage spike of an initial conversion, when trashed
originals briefly double the space a converted album takes. After that, the
trash holds a rolling month of new family photos. With an admin key
configured, a pass skips its moves while Immich's sidecar or metadata queues
have a backlog, since the sidecar step would wait behind them.

A photo that is blocked before the move starts work on it doesn't count
toward `assets_per_pass`, so photos that are blocked on every pass, live
photos say, never hold up the ones behind them. A move that fails does count,
and so does one that gives up waiting for Immich to write the sidecar.

Re-uploads move no bytes, so they aren't what the caps meter. A pass handles
every re-upload it found before it starts on the album moves, and none of
them counts toward `assets_per_pass`, whether it's trashed, blocked or fails.

The webhook never wakes the pass. It handles re-uploads alone, with no cap,
for the same reason. Everything else waits for the interval.
