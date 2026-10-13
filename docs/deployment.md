# Deployment

The service is one container, on the same docker network as Immich, with a
volume for its state file. It is not an Immich plugin: the plugin sandbox cannot
hold other users' keys, keep state, or poll. The only pieces on the Immich
side are things the service creates itself through the API: albums, partner
shares, and one workflow per member.

## Before the first run

1. **Accounts.** Create the family user. Give it no quota, or a generous
   one. Every participating member needs their own user, which they already
   have. A trashed original counts against the member's quota until the
   trash purges, 30 days by default.
2. **Cluster group.** If face detection is going to be on, first put every
   member and the family account in one cluster group. Then faces pool across
   accounts from the first recognition pass. If face detection is on before
   the group exists, a recognition reset is needed later.
3. **Keys.** Get the family account's API key and one from each member, with
   the permissions in [configuration.md](configuration.md). An admin user's
   key with `queue.read` is optional. Members create theirs under Account
   Settings, API Keys. Tell them which boxes to tick.
4. **Config.** Copy `config.example.toml` to `config/config.toml`. Fill in the
   URL and the keys. Leave `readonly = true`. Set
   `state_path = "/data/state.json"`. For the webhook, make up a token and
   set `url` to the container's name on the network.

## Compose

`compose.yml` in the repo:

- It joins the Immich stack's network as an external network. For the stock
  Immich `docker-compose.yml` that network is `immich_default`. If your
  project is named differently, set `IMMICH_NETWORK` (`docker network ls`
  shows the name).
- It mounts `./config` read-only at `/config`. It mounts a named volume,
  `ifl-data`, at `/data` for the state file.
- It mounts the host's `/etc/localtime` read-only, so the container uses the
  host's timezone. `report_at` and the log times are in that timezone. Without
  the mount, they are in UTC.
- It names the container `ifl`, which is what the webhook URL uses. No port
  is published. Immich reaches the receiver inside the network.
- It runs `ifl daemon` by default, and restarts it unless stopped.

Under rootless docker, root in the container is the unprivileged host user,
and it can read a 0600 config. Under rootful docker, set `user:` in the
compose file to match the owner of `./config`. The state volume is writable
by any user, so it needs no step.

```
docker compose build          # or pull, once the image is published
docker compose run --rm ifl show-config --minimal
docker compose run --rm ifl observe
```

When `observe` reports what you expect, set `readonly = false` and run:

```
docker compose run --rm ifl reconcile-albums
docker compose up -d
docker compose logs -f --no-log-prefix
```

The first pass creates the dropbox, the partner shares and the workflows.
Then it converts and moves whatever is waiting, under the limits.

## Running one-off commands beside the daemon

`docker compose run --rm ifl <command>` runs a command in a second container
with the same config and the same state volume. Only one command that writes
to Immich can run at a time, and the run lock makes sure of that. The daemon
holds the run lock for as long as it runs. A command that writes to Immich
(`process`, `move`, `convert-album`, `reconcile-albums`) needs it too. So
beside a running daemon, the command refuses with exit code 3. Stop the
daemon first, or let the daemon do the work. `observe` and `show-config` do
not take the run lock and are always safe.

## Webhook

With `[webhook]` configured, each member gets a workflow named
`immich-family-library`. Its trigger is AssetCreate. It has one step, the
core plugin's "Trigger Webhook" action, which posts to `url` with the token
in the `X-IFL-Token` header. `reconcile-albums` creates the workflow, and
repairs it if the URL or the token changes. Members can see it under their
workflows. If a member disables or deletes it, the next reconcile puts it
back.

Immich's plugin host only calls hosts that the plugin's manifest allows. The
core plugin allows any host. If a later Immich tightens that, the symptom is
an error in Immich's log and a receiver that never hears anything. The pass
still catches every re-upload, only later.

## The state file

`/data/state.json` is the state file. It holds each member's re-upload scan
mark, each member's tally of trashed re-uploads since the last daily report,
and the time of that report. Nothing in it needs a backup. If it is lost, the
re-upload marks start again at the next pass, and one daily report is
dropped. Re-uploads from the gap are not found and stay in the member's
library as duplicates of family photos. The record of where a family photo
came from is on the photo itself, in its `ifl` metadata entry and its
`from/<member>` tag.

The file beside it, `state.json.lock`, is the run lock. It is not a lock on
the state file. It stops two commands that write to Immich from running at the
same time, and all of them take it, including the commands that do not use
the state file. It is in this directory because the service can write here.
The run lock uses flock. On an NFS volume, flock is not reliable. Keep the
state volume on a local disk.

If you remove a member from the config, the service removes that member from
the state file at its next start.

Do not edit the file. The daemon loads it one time, when it starts, and
writes it from memory after that. If the file does not parse at start, the
service moves it to `state.json.bad` and starts with an empty state.

## Upgrading

Pull or build the new image and run `docker compose up -d`. The service pins
its API client to the Immich version it was built against.
[development.md](development.md) says which. Upgrading Immich past that
version needs a matching release of the service. Check the changelog.

The service refuses to start on an Immich that is older than that version. On
a later minor or major version it starts, and it logs a warning that the
release is not tested there. Do not let it move photos on such a version
until a matching release is out: set `readonly` to true.

## Before production

Two gaps are open as issues:

- The mobile app's "Sync remote deletions" setting is what ends the re-upload
  loop for a photo. It exists only on Android 12 or later, and it has never
  been exercised on a phone (issue 13). Do it on prod with one hand-moved
  photo, from an Android phone, before you move anything else.
- Live photos are blocked, not handled (issue 14).
