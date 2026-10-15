# Deployment

The service is one container, on the same docker network as Immich, with a
volume for its run lock. It is not an Immich plugin: the plugin sandbox cannot
hold other users' keys, keep state, or poll. The only pieces on the Immich
side are things the service creates itself through the API: albums, partner
shares, and one workflow per member.

There are two ways to run the container:

- **In the Immich stack.** The service is one more service in Immich's own
  compose project. This is the usual way, and the README shows it.
- **As a separate project.** The service has its own compose file, and it
  joins the network of the Immich stack from outside.

Run only one copy of the service against an Immich instance. Two copies have
two run locks, so neither one stops the other, and both write to Immich.

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

## In the Immich stack

The README has the service definition. Add it to `docker-compose.override.yml`
in the directory of Immich's `docker-compose.yml`. Docker Compose reads that
file with Immich's own file, so the service becomes part of the Immich
project. If the override file already has a top-level `volumes:` section, add
`ifl-data` to it. Do not add a second section.

The service definition does these things:

- It uses the published image with the tag `0.1`. That tag moves to each
  patch release of 0.1. "Upgrading" below tells how to move to a later
  release.
- It is on the default network of the Immich stack, with no setting. The
  service reaches Immich at `http://immich_server:2283`, which is the URL in
  the example config. Immich reaches the webhook receiver at
  `http://ifl:8080/webhook/asset-created`.
- It starts after `immich-server`.
- It mounts `./ifl` read-only at `/config`. The config file is
  `ifl/config.toml`.
- It mounts a named volume, `ifl-data`, at `/data` for the run lock.
- It mounts the host's `/etc/localtime` read-only, so the container uses the
  host's timezone. `report_at` and the log times are in that timezone. Without
  the mount, they are in UTC.
- It runs `ifl daemon` by default, and restarts it always, as Immich's own
  services do.

Run each `docker compose` command in the directory of Immich's
`docker-compose.yml`. A command that names only `ifl` does not change the
Immich services.

## As a separate project

Use this if you do not want to change the files of the Immich stack. Put this
`compose.yml` in a directory of its own, with the config at
`config/config.toml`:

```yaml
name: immich-family-library

services:
  ifl:
    image: jcsanyi/immich-family-library:0.1
    container_name: ifl
    restart: unless-stopped
    volumes:
      - ./config:/config:ro
      - ifl-data:/data
      - /etc/localtime:/etc/localtime:ro
    networks:
      - immich

volumes:
  ifl-data:

networks:
  immich:
    external: true
    name: ${IMMICH_NETWORK:-immich_default}
```

The differences from the service in the Immich stack:

- It joins the network of the Immich stack as an external network. For the
  stock Immich `docker-compose.yml`, that network is `immich_default`. If
  your Immich project has a different name, set `IMMICH_NETWORK`. The command
  `docker network ls` shows the name.
- It names the container `ifl`, which is the host name in the webhook URL.
- It cannot wait for `immich-server`, because that service is in a different
  project.

`compose.yml` in the repository is this setup, with `build: .` in place of the
image. It is for development.

## The config

1. Get the annotated example config. In the Immich stack, run:

   ```
   docker compose run --rm -T ifl example-config > ifl/config.toml
   ```

   As a separate project, write it to `config/config.toml`.
2. Fill in the keys. If Immich is not at `http://immich_server:2283` on the
   network, change `url`.
3. Leave `readonly = true`.
4. Keep `lock_path = "/data/run.lock"`.
5. For the webhook, make up a token. Keep the `url` of the example.

Under rootless docker, root in the container is the unprivileged host user,
and it can read a 0600 config. Under rootful docker, add `user:` to the
service, to match the owner of the config file. The data volume is writable
by any user, so it needs no step.

## The first run

1. Run `docker compose run --rm ifl show-config --minimal`, and examine the
   settings.
2. Run `docker compose run --rm ifl observe`. It reports what the service
   will do, and it changes nothing.
3. When `observe` reports what you expect, set `readonly = false`.
4. Run `docker compose run --rm ifl reconcile-albums`.
5. Run `docker compose up -d`.
6. Run `docker compose logs -f --no-log-prefix ifl` to watch the log.

The first pass creates the dropbox, the partner shares and the workflows.
Then it converts and moves whatever is waiting, under the limits.

## The temp directory

The compose files mount nothing for the temp directory. A move keeps one file
at a time in `/tmp/immich-family-library`, in the writable layer of the
container. The disk that holds docker's data needs free space for the largest
photo or video that a member has. If that disk is too small, mount a volume
and set `temp_dir` in `[service]` to its path.

If you run the service outside a container, examine `temp_dir` first. On many
hosts `/tmp` is a RAM disk (tmpfs), and a file there uses memory. Set
`temp_dir` to a directory on a disk.

Give each instance of the service its own `temp_dir`. A command that writes to
Immich removes the `ifl-` files in that directory when it starts. If two
instances share the directory, one can remove a file that the other uses. Each
container has its own `/tmp`, so the default is safe there.

## Running one-off commands beside the daemon

`docker compose run --rm ifl <command>` runs a command in a second container
with the same config and the same data volume. Only one command that writes
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

## What the service keeps

The service keeps no data on disk. The record of where a family photo came
from is on the photo itself, in its `ifl` metadata entry and its
`from/<member>` tag. Nothing needs a backup.

The daemon keeps three things in memory: the mark of the re-upload scan, the
count of trashed re-uploads for each member, and the time of the last daily
report. A restart has these effects:

- The re-upload scan reads the last three days of each member's uploads one
  time. If the daemon was stopped for longer, re-uploads from before those
  three days are not found. They stay in the member's library as duplicates of
  family photos.
- The count starts again at zero. A daemon that stops cleanly logs its
  report before it exits. The next daily report covers only the time since
  the restart.

## The run lock

The one file that the service writes is the run lock, at `lock_path`. It
stops two commands that write to Immich from running at the same time.

The run lock works only between commands that open the same file. A command
from `docker compose run` is in a second container, and the data volume is
the one writable place that it shares with the daemon. Keep
`lock_path = "/data/run.lock"` from the example config. The data volume is
there for this file only.

The default `lock_path` is `run.lock` beside the config file. In the
container that directory is mounted read-only, so a config without the
setting fails at startup with a message that names the run lock.

Do not put the run lock in the `/tmp` of the container to remove the volume.
Each container has its own `/tmp`. A command from `docker compose run` then
does not see the lock of the daemon, the two write to Immich at the same
time, and nothing warns you.

The run lock uses flock. On an NFS volume, flock is not reliable. Keep the
volume on a local disk.

## External libraries

The service is not for use with Immich external libraries. It does not move a
photo that is in one. A member can share such a photo through an album, and
not through the dropbox. [how-it-works.md](how-it-works.md#external-libraries)
says what occurs in each case.

## Upgrading

Read [CHANGELOG.md](../CHANGELOG.md) before you upgrade. Do the steps in the
upgrade notes of each release after the one you run.

For a patch release, the tag `0.1` already points to the new image. Run
`docker compose pull ifl` and then `docker compose up -d`. For a later minor
release, for example 0.2, first change the tag in the service definition.

The service pins its API client to the Immich version it was built against.
[development.md](development.md) says which. Upgrading Immich past that
version needs a matching release of the service.

The service refuses to start on an Immich that is older than that version. On
a later minor or major version it starts, and it logs a warning that the
release is not tested there. Do not let it move photos on such a version
until a matching release is out: set `readonly` to true.

A release can add a permission to a key set. The startup check then refuses a
key that does not have the permission, and the service does not start. Before
you start such a release, add the permission to each key. The owner of a key
does this in Immich, under Account Settings, API Keys.

## Before production

Two gaps are open as issues:

- The mobile app's "Sync remote deletions" setting is what ends the re-upload
  loop for a photo. It exists only on Android 12 or later, and it has never
  been exercised on a phone (issue 13). Do it on prod with one hand-moved
  photo, from an Android phone, before you move anything else.
- Live photos are blocked, not handled (issue 14).
