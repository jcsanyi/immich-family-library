# Immich Family Library

A small service that runs next to a self-hosted [Immich](https://immich.app) instance and gives a family one shared photo library while each member keeps their own account.

Members upload to their own accounts as usual. When they share an album with the family, or drop photos into the family dropbox, the service moves those photos into a dedicated family account. Everything the family account owns shows up in every member's timeline through partner sharing. Members never need to share their whole library with anyone.

Immich has no way to transfer ownership of a photo, so "move" means the service re-uploads the photo under the family account with all its metadata, then trashes the original.

## How it works

The short version. [docs/for-members.md](docs/for-members.md) says the same thing for the family, and [docs/how-it-works.md](docs/how-it-works.md) says it precisely.

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

Your tags don't carry over. Tags are private to each account anyway, so nobody else would see them. Instead the family copy gets one tag, `from/<your name>`, visible when browsing as the family account, and your original gets a `family` tag before it's trashed, so your own tag list shows what you've handed over. Both names are configurable.

Manual face corrections and stacks don't carry over yet. Likes and comments on album photos are lost when an album is converted or a photo moves.

A photo that's in one of your public shared links isn't moved. The link would break, so the service leaves the photo where it is and reports it until you take it out of the link or delete the link. The same goes for an album you've shared with the family that also has a public link.

## The phone backup problem

The Immich mobile app re-uploads any photo it can't find on the server. Once a moved original is trashed and the trash purges, your phone will upload it again. The service recognises these by checksum, within seconds through the webhook or on the next pass without it, and trashes them straight away, so they never reach the family timeline, but it's wasted upload and processing. Each one is counted, and a member with more than `reuploads_per_pass` (under `[warnings]`, default 10) in one pass is named in a warning.

If the family account has deleted a photo and you share it again, the service restores the family's copy from its trash rather than uploading it anew. A re-upload of a photo the family has deleted is left alone.

To stop the loop, turn on the experimental **Sync remote deletions** setting in the mobile app (Settings, Advanced). With it on, the app moves the local copy to your phone's trash the next time it opens, as long as the original is still in the server trash. The service never force-empties trash for exactly this reason.

If a member keeps re-uploading in volume, the service logs a warning naming them.

## Storage

Trashed originals count against a member's usage until the trash purges, 30 days by default. The initial conversion of existing albums is the big spike; after that it's a rolling month of new family photos. The family account should have no quota or a generous one.

## Requirements

- Immich 3.3 or later. Cluster groups (3.2) and people sharing (3.3) are needed for faces to work across accounts.
- A family Immich user with an API key, and an API key from every participating member. The service checks each key works before it touches anything. Members without a key can still share albums with the family user; those albums are reported, not converted.
- All accounts in one cluster group, created before face detection is turned on.
- Optionally, a `[webhook]` section in the config, so re-uploads are caught in seconds rather than on the next pass.

## Running it

A single container next to Immich, on the same docker network, with a volume for its state database. `compose.yml` is the deployment and `config.example.toml` the annotated configuration. The walkthrough is in [docs/deployment.md](docs/deployment.md).

## Documentation

- [docs/for-members.md](docs/for-members.md): the page to hand to family members. What they'll see, the one phone setting, what carries.
- [docs/how-it-works.md](docs/how-it-works.md): the model. The one rule, the flows, the move step by step, re-uploads, state and pacing.
- [docs/cli.md](docs/cli.md): every command, exit codes, a first run.
- [docs/configuration.md](docs/configuration.md): every setting with its default, and the API key permissions.
- [docs/deployment.md](docs/deployment.md): compose, the webhook, the ledger, upgrading, what to check before production.
- [docs/immich-notes.md](docs/immich-notes.md): what the service relies on in Immich 3.3.1 and how each point was confirmed.
- [docs/development.md](docs/development.md): tests, the fake Immich, the dev scripts.

## Status

Stages 1 to 7 are built and verified on a dev instance: observing, album conversion, family album consistency, the per-asset move, the full pass, re-upload detection, and the daemon with the webhook receiver. What's left before a first release, and the improvements after it, are the [issues](https://github.com/jcsanyi/immich-family-library/issues).
