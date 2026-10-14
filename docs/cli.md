# Commands

`ifl` is one entry point with subcommands. Every command takes:

- `-c PATH`, `--config PATH`: the config file. The default is `config.toml`
  in the current directory. In the container it is `/config/config.toml`.
- `-v`, `--verbose`: debug logging.

Logging goes to stderr. `show-config` is the only command that writes to
stdout.

## Startup

Every command except `show-config` checks the server and opens every account
first. The server must be Immich 3.3 or later, and this is checked before any
key. Each key must work, belong to the configured email, and carry the
permissions the service needs (listed in [configuration.md](configuration.md)).
If any of this fails, the command exits with code 2 before it touches
anything. A config pointed at another Immich server fails the same way, which
is why the emails are checked.

If Immich does not answer during these checks, the command exits with code 1.
No answer says nothing about a key. A `url` that names a host that does not
exist fails this way too, and the message names the host. So does a `url`
that names a server that is not Immich.

While `readonly` is true in the config, every command that writes to Immich
refuses to start. `observe` and `show-config` still work.

Only one command that writes to Immich can run at a time, because two at the
same time can create an album or a workflow twice. Each of these commands
takes the run lock, a file at `lock_path`, before it does anything else. If
the daemon or another command already holds the run lock, the command exits
with code 3 and changes nothing. [deployment.md](deployment.md) explains where
to keep the file.

## `ifl observe`

One pass of the three scans, reported and nothing more. It never writes to
Immich. It reports:

- `CONVERT` for each album that a pass converts, and `BLOCKED` with the
  reason for each one it does not.
- The family album fixes that reconcile makes: `CREATE dropbox`, `SHARE`,
  `PROMOTE`, `PARTNER-SHARE`, `CREATE webhook workflow`, `FIX webhook
  workflow`, or "family albums consistent".
- `MOVE` lines for member photos in family albums, with `[dropbox]` marked.
  `REUPLOAD` lines for members' recent uploads that the family already has.
  `SKIP` lines for re-uploads whose family copy is trashed. `BLOCKED` photos.
- `REMOVE` lines for photos from an external library that are in the dropbox.
  A pass takes these out of the dropbox and does not move them. A photo from
  an external library in a different family album has no line.
- A count of re-uploads waiting per member.
- A warning if Immich has jobs waiting on the queues that a move depends on.
  While they wait, a pass moves nothing. This needs the admin key.

Run it first on any new install. Run it again after any config change, before
you set `readonly` to false.

## `ifl convert-album <album-id>` and `ifl convert-album --all`

Converts one member album that is shared with the family, or every such album
up to `albums_per_pass`. Both forms convert only what `observe` reports. The
same scan decides, so a blocked album stays blocked here, and the single-id
form tells you why. The single-id form ignores the limit. `--all` logs each
result and a summary (converted, failed, remaining). If anything failed, it
exits 1.

The album id is in the web UI's address bar when the album is open.

## `ifl reconcile-albums`

Makes the family albums consistent. It creates the dropbox if it is missing.
It adds every member as an editor to every family-owned album and promotes
viewers. It partner-shares the family library with every member. With
`[webhook]` configured, it creates or fixes each member's webhook workflow. It
removes nothing. On a consistent instance it does nothing and says so. One
fix that fails does not stop the others. If any fix failed, it exits 1.

## `ifl move <asset-id>` and `ifl move --all`

`--all` moves every member photo that is in a family album, and every
re-upload the family already has, up to `assets_per_pass`. The decision is the
same as in `observe`. It finds only the re-uploads of the last three days. It
reports moved, blocked, failed and remaining.

With an asset id, the command moves that one photo. The asset id is in the
address bar when the photo is open. If the photo is blocked, the command
reports the reason and exits 1.

The command does not move a photo from an external library. With an asset id,
it gives the reason and exits 1. `--all` leaves these photos where they are,
and it takes none out of the dropbox. Only a pass does that.

The command looks up one id directly, so the age of the photo does not matter.
If the photo is in no family album, the command checks whether it is a
re-upload: the family has the same file, in a copy that the service moved from
this member. If it is, the command adds the family's copy to the albums that
hold the member's photo, and trashes the member's photo. In every other case
the command moves nothing, gives the reason and exits 1. The cases are: the
family has no copy, the family's copy is in the trash, and the family's copy
came from somewhere else.

The summary line of `--all` counts the re-uploads it trashed. The command adds
nothing to the daily report, which only the daemon keeps. If anything failed,
it exits 1.

With `admin_api_key` set, both forms refuse to start while Immich's sidecar or
metadata queues have jobs waiting.

A move that fails partway leaves the original in place. Run it again, and it
resumes from what Immich has.

## `ifl process`

One complete pass: the album scan and conversions under `albums_per_pass`,
reconcile, the queue check if an admin key is configured, then the asset scan
and moves under `assets_per_pass`, re-uploads included. Before the moves, it
takes each photo from an external library out of the dropbox, with no limit. A just-converted
album's photos move in the same pass. A queue backlog skips the moves for this
pass, not the album work. It ends with one summary line, which counts the
re-uploads it trashed. It does not make the daily report, which only the
daemon keeps. If any conversion, reconcile fix, removal from the dropbox or
move failed, it exits 1.

Run it by hand, or from cron, as an alternative to the daemon.

## `ifl daemon`

The service. It runs a `process` pass every `interval_seconds`. With
`[webhook]` configured, it also runs the receiver that Immich's per-member
workflows post new uploads to. A re-upload posted there is trashed within
seconds. Nothing else the receiver sees is acted on, so the interval and the
limits remain the only pacing for real moves. It stops cleanly on SIGINT or
SIGTERM. If a pass fails, the failure is logged and the next pass runs on
schedule. If a webhook batch fails, the failure is logged and the next pass
picks up what it missed. It holds the run lock for as long as it runs.

The daemon counts the re-uploads it trashes, for each member. Once a day,
when `report_at` passes, it logs one warning for each member with a count
above zero, and starts the count again. The count is in memory. When the
daemon stops on SIGINT or SIGTERM, it logs the same warnings for what it has
counted so far. After a restart, the next report covers only the time since
the restart, and the warning gives that time.

This is the container's default command.

## `ifl show-config [--minimal]`

Prints the effective configuration as TOML on stdout, with every key and
token redacted to its last four characters. `--minimal` leaves out settings
that match their defaults. It needs no network, so it works before the keys
do.

## Exit codes

- `0`: everything asked for was done.
- `1`: something was blocked or failed. The log says what. For `--all`,
  `reconcile-albums` and `process`, only failures count, not blocks. If Immich
  does not answer, at startup or during a scan, the command stops with one
  line and exits 1.
- `2`: the config is wrong, or Immich refused a key, or `readonly` refused the
  command, or the run lock cannot be opened.
- `3`: the daemon or another writing command holds the run lock. Wait for
  it, or stop the daemon.

## A first run

```
ifl show-config --minimal     # the config parses and points where you think
ifl observe                   # keys work; see what would happen
# set readonly = false
ifl reconcile-albums          # dropbox, editors, partner shares, workflows
ifl observe                   # now shows conversions and moves waiting
ifl convert-album <id>        # one album by hand, look at it in the UI
ifl move <id>                 # one photo by hand, check the copy
ifl process                   # a whole pass under the limits
ifl daemon                    # or start the container
```
