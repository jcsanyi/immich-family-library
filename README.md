# Immich Family Library

A small service that runs next to a self-hosted [Immich](https://immich.app) instance and gives a family one shared photo library while each member keeps their own account.

Members upload to their own accounts as usual. When they share an album with the family, or drop photos into the family dropbox, the service moves those photos into a dedicated family account. Everything the family account owns shows up in every member's timeline through partner sharing. Members never need to share their whole library with anyone.

Immich has no way to transfer ownership of a photo, so "move" means the service re-uploads the photo under the family account with all its metadata, then trashes the original.

## How it works

There's one extra Immich user, the family account, and it owns everything that's been shared. It partner-shares to every member with "show in timeline" on, which is what puts family photos in each member's timeline.

The service watches for one thing: a member-owned photo sitting in an album the family account owns. When it finds one, it moves it. Every feature below is just a way of getting a photo into that position.

### Sharing an album

Share any of your albums with the family user. The service recreates it as a family-owned album with the same name and description, shares that with every member as an editor, moves the photos into it, and deletes your original. You and the other members see the same album you shared; it's just owned by the family now. Anyone else you had shared it with keeps their access, at the same role. If a family album with that name already exists, the two are merged into it.

Albums you share privately with other members are left alone. If a photo in one of those later gets shared with the family, the family copy is added back to the private album, so nothing changes from your point of view.

### Adding to a family album

Any member can add their own photos to any family album. The service moves them. It also keeps the family albums consistent: the dropbox exists, and every family album is shared with every member as an editor, so an album created by hand in the family account, or a member added later, needs no manual sharing.

### The dropbox

One family album is configured as the dropbox. Photos added to it are moved to the family account but not re-added to the dropbox. Use it for photos that belong in the family timeline but not in any particular album.

### Reclaiming a photo

Each member has a private reclaim album. Add a family-owned photo that you originally shared and the service moves it back to you. It disappears from every family album, because the family copy is gone. Only the original owner can reclaim a photo.

## What's carried over

The moved copy keeps everything embedded in the file, plus the edits you made in Immich: description, corrected date and time zone, location, rating, favourite, and crop, rotate and mirror. Faces are recognised again on the family copy and, because all accounts share a cluster group, they cluster with the same people.

Manual face corrections and stacks don't carry over yet.

## The phone backup problem

The Immich mobile app re-uploads any photo it can't find on the server. Once a moved original is trashed and the trash purges, your phone will upload it again. The service recognises these by checksum and trashes them straight away, so they never reach the family timeline, but it's wasted upload and processing.

To stop the loop, turn on the experimental **Sync remote deletions** setting in the mobile app (Settings, Advanced). With it on, the app moves the local copy to your phone's trash the next time it opens, as long as the original is still in the server trash. The service never force-empties trash for exactly this reason.

If a member keeps re-uploading in volume, the service logs a warning naming them.

## Storage

Trashed originals count against your usage until the trash purges, 30 days by default. The initial conversion of existing albums is the big spike; after that it's a rolling month of new family photos. The family account should have no quota or a generous one.

## Requirements

- Immich 3.3 or later. Cluster groups (3.2) and people sharing (3.3) are needed for faces to work across accounts.
- A family Immich user with an API key.
- An API key from every participating member. The service checks each key works before it touches anything, and skips operations it doesn't have the keys for. Members without a key can still share albums with the family user; they just won't be converted, and the service warns.
- All accounts in one cluster group, created before face detection is turned on.
- Optionally, an Immich workflow with a webhook action on asset upload, pointing at the service, so re-uploads get caught in seconds instead of on the next poll.

## Running it

The service is a single container, deployed in the same compose stack as Immich with a volume for its state database. `compose.yml` shows the deployment and `config.example.toml` is the annotated configuration: the Immich URL, the family account and its API key, one section per member with their API key, and the name of the dropbox album.

Two settings guard against accidents:

- `readonly` under `[service]`, true unless you set it to false. While it's on, every command that would write to Immich refuses to run. Leave it on until `observe` reports what you expect.
- `[limits]` caps how much one pass does. `albums_per_pass` (default 5, 0 for no limit) is the most albums `convert-album --all` will convert in one run; the rest wait for the next. Commands given a specific id ignore the limits.

### Commands

Every command is one pass that exits. Each takes `-c path/to/config.toml` (default `config.toml` in the current directory) and `-v` for debug logging.

- `ifl observe` reports what the rules would do: albums that would be converted, photos that would be moved, and anything blocked and why. Never writes.
- `ifl convert-album <album-id>` converts one album a member has shared with the family. `ifl convert-album --all` converts every such album, up to the limit.
- `ifl show-config` prints the effective configuration as TOML with keys redacted to their last four characters. `--minimal` prints only what differs from the defaults.

The remaining commands arrive with the stages listed below.

## Status

Under development, in the stages listed in [DESIGN.md](DESIGN.md). That's the working design document: flows, state model, failure handling, API permissions, and the list of Immich behaviours still being verified on a dev instance. It'll shrink as real documentation replaces it.
