# Commands

`ifl` is one entry point with subcommands. Every command takes:

- `-c PATH`, `--config PATH`: the config file. Default `config.toml` in the
  current directory. In the container it's `/config/config.toml`.
- `-v`, `--verbose`: debug logging.

Logging goes to stderr. `show-config` is the only command that writes to
stdout.

## Startup

Every command except `show-config` opens every account first: the key must
work, belong to the configured email, and carry the permissions the service
needs (listed in [configuration.md](configuration.md)). The server must be
Immich 3.3 or later. A problem here exits with code 2 before anything is
touched. A config pointed at the wrong server fails this way too, which is
the point of checking emails.

While `readonly` is true in the config, every command that would write to
Immich refuses to start. `observe` and `show-config` still work.

## `ifl observe`

One pass of the three scans, reported and nothing more. Never writes to
Immich; it does create the ledger file if it's missing, to report its counts.
Reports:

- `CONVERT` albums the album scan would convert, and `BLOCKED` ones with why.
- The family album fixes reconcile would make: `CREATE dropbox`, `SHARE`,
  `PROMOTE`, `PARTNER-SHARE`, `CREATE webhook workflow`, or "family albums
  consistent".
- `MOVE` lines for member photos in family albums, `[dropbox]` marked;
  `REUPLOAD` lines for members' recent uploads the family already has; `SKIP`
  lines for re-uploads whose family copy is trashed; `BLOCKED` photos.
- A count of re-uploads waiting per member, as a warning above
  `reuploads_per_pass`.
- The ledger's counts.

Run it first on any new install, and after any config change, before flipping
`readonly`.

## `ifl convert-album <album-id>` and `ifl convert-album --all`

Converts one member album that's shared with the family, or every such album
up to `albums_per_pass`. Both only convert what `observe` would: the same
scan decides, so a blocked album stays blocked here, and the single-id form
tells you why. The single-id form ignores the limit. `--all` logs each result
and a summary (converted, failed, remaining) and exits 1 if anything failed.

The album id is in the web UI's address bar when the album is open.

## `ifl reconcile-albums`

Makes the family albums consistent: creates the dropbox if missing, adds
every member as an editor to every family-owned album and promotes viewers,
partner-shares the family library with every member, and, with `[webhook]`
configured, creates or fixes each member's webhook workflow. Removes nothing.
Running it on a consistent instance does nothing and says so.

## `ifl move <asset-id>` and `ifl move --all`

Moves one member photo that's in a family album (or is a re-upload the family
already has), or every such photo up to `assets_per_pass`. Same decision as
`observe`. A photo that's blocked is reported with the reason and exits 1.

The asset id is in the address bar when the photo is open. `--all` reports
moved, blocked, failed and remaining, advances the re-upload marks for what
finished, and exits 1 if anything failed.

With `admin_api_key` set, both forms refuse to start while Immich's sidecar or
metadata queues have jobs waiting.

A move that fails partway leaves the original in place. Run it again and it
resumes from what Immich has.

## `ifl process`

One complete pass: album scan and conversions under `albums_per_pass`,
reconcile, the queue check if an admin key is configured, asset scan and moves
under `assets_per_pass`, re-uploads included. A just-converted album's photos
move in the same pass. A queue backlog skips the moves for this pass, not the
album work. Ends with one summary line and the ledger counts. Exit 1 if any
conversion or move failed.

Run by hand, or from cron, as an alternative to the daemon.

## `ifl daemon`

The service. A `process` pass every `interval_seconds`, and, with `[webhook]`
configured, the receiver Immich's per-member workflows post new uploads to.
A re-upload posted there is trashed within seconds; nothing else the receiver
sees is acted on, so the interval and the limits remain the only pacing for
real moves. Stops cleanly on SIGINT or SIGTERM. A pass that fails is logged
and the next one runs on schedule; a failed webhook batch is logged and the
next pass picks up what it missed. This is the container's default command.

## `ifl show-config [--minimal]`

The effective configuration as TOML on stdout, every key and token redacted
to its last four characters. `--minimal` leaves out settings that match their
defaults. Needs no network, so it works before the keys do.

## Exit codes

- `0`: everything asked for was done.
- `1`: something was blocked or failed; the log says what. For `--all` and
  `process`, only failures count, not blocks.
- `2`: the config or a key is wrong, or `readonly` refused the command.

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
