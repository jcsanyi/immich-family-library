# Deployment

One container, on the same docker network as Immich, with a volume for the
ledger. Not an Immich plugin: the plugin sandbox can't hold other users' keys,
keep state, or poll. The only Immich-side pieces are things the service
creates itself through the API: albums, partner shares, and one workflow per
member.

## Before the first run

1. **Accounts.** Create the family user. Every participating member needs
   their own user, which they have already.
2. **Cluster group.** If face detection is going to be on, put every member
   and the family account in one cluster group first, so faces pool across
   accounts from the first recognition pass. Face detection on before the
   group means a recognition reset later.
3. **Keys.** The family account's API key and one from each member, with the
   permissions in [configuration.md](configuration.md). Optionally an admin
   user's key with `queue.read`. Members create theirs under Account
   Settings, API Keys; tell them which boxes to tick.
4. **Config.** Copy `config.example.toml` to `config/config.toml`, fill in the
   URL and keys, leave `readonly = true`, set `ledger_path =
   "/data/ledger.sqlite"`. For the webhook, make up a token and set `url` to
   the container's name on the network.

## Compose

`compose.yml` in the repo:

- joins the Immich stack's network as an external network. For the stock
  Immich `docker-compose.yml` that's `immich_default`; set `IMMICH_NETWORK`
  if your project is named differently (`docker network ls` shows it);
- mounts `./config` read-only at `/config` and `./data` at `/data`;
- names the container `ifl`, which is what the webhook URL uses. No port is
  published; Immich reaches the receiver inside the network;
- runs `ifl daemon` by default, restarting unless stopped.

Under rootless docker, root in the container is the unprivileged host user
and can read a 0600 config. Under rootful docker, set `user:` in the compose
file to match the owner of `./config` and `./data`.

```
docker compose build          # or pull, once the image is published
docker compose run --rm ifl show-config --minimal
docker compose run --rm ifl observe
```

When `observe` reports what you expect, set `readonly = false` and:

```
docker compose run --rm ifl reconcile-albums
docker compose up -d
docker compose logs -f --no-log-prefix
```

The first pass creates the dropbox, the partner shares and the workflows, and
then converts and moves whatever is waiting, under the limits.

## Running one-off commands beside the daemon

`docker compose run --rm ifl <command>` runs a command in a second container
with the same config and ledger. SQLite handles two processes on the ledger
file, but don't run two `process`-style commands at once; the in-flight set
that keeps the daemon's own pass and receiver apart lives inside one process.
`observe` and `show-config` are always safe.

## Webhook

With `[webhook]` configured, each member gets a workflow named
`immich-family-library`: trigger AssetCreate, one step, the core plugin's
"Trigger Webhook" action, posting to `url` with the token in the `X-IFL-Token`
header. `reconcile-albums` creates it and repairs it if the URL or token
change. Members can see it under their workflows; if one disables or deletes
it, the next reconcile puts it back.

Immich's plugin host only calls hosts the plugin's manifest allows; the core
plugin allows any. If a later Immich tightens that, the symptom is an error in
Immich's log and a receiver that never hears anything. The pass still catches
every re-upload, just later.

## The ledger

`/data/ledger.sqlite` is the audit trail and the only place that knows who a
family photo came from once the original is purged. Back it up with the rest
of your data. Losing it means the re-upload marks reset (one slow full scan)
and reclaim, when it exists, can't tell owners apart; nothing else depends
on it.

## Upgrading

Pull or build the new image and `docker compose up -d`. The service pins its
API client to the Immich version it was built against (`immichpy` 9.0.1 for
Immich 3.3.1); upgrading Immich past that needs a matching release of the
service. Check the changelog.

## Before touching production

Two things the design still lists as unverified:

- The mobile app's "Sync remote deletions" setting, which is what ends the
  re-upload loop for a photo, has never been exercised on a phone. Do it on
  prod with one hand-moved photo before moving anything else.
- Live photos are blocked by the move, not handled.

Both are tracked as issues.
