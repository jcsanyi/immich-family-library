# Development

The language is Python 3.12, managed with `uv`. Tests use `pytest`. Lint and
format use `ruff`.

```
uv sync
uv run pytest -q
uv run ruff check . && uv run ruff format .
uv run ifl observe -c config.toml
```

## The API client

The API client is `immichpy` (https://github.com/timonrieger/immichpy),
generated from Immich's OpenAPI spec and async on aiohttp. It is pinned to
the Immich version in use: `immichpy` 9.0.1 for Immich 3.3.1. The client
repo's `COMPATIBILITY.csv` maps package versions to Immich versions. This is
the one place in the docs that records the pin. When it changes, update it
here.

For moves, use the generated `upload_asset`. Do not use the path-based
`assets.upload()` helper, which is a bulk uploader. If the immichpy project
goes quiet, the fallback is to regenerate the client from the spec with the
same generator.

The service is async throughout. The webhook receiver uses the aiohttp
server, so that there is one HTTP stack.

## Target Immich version

Develop against Immich 3.3. Cluster groups came in 3.2 and people sharing in
3.3. Do not propose workarounds for older versions.

Face detection stays off until every account is in a cluster group, so that no
facial recognition reset is needed. Do not rely on faces or on people sharing.

## Layout

- `src/ifl/config.py`: the config file, its defaults, and the view of it that `show-config` prints.
- `src/ifl/accounts.py`: one client per account, and the startup checks.
- `src/ifl/immich.py`: thin helpers over the generated client. Every Immich call the service makes is here.
- `src/ifl/observer.py`: the three scans, read-only. `observe` reports them. `process` acts on them.
- `src/ifl/convert.py`, `reconcile.py`, `move.py`: the three actions. `sidecar.py` is the XMP tag rewrite.
- `src/ifl/process.py`: one pass. `daemon.py`: the pass on an interval, plus the webhook receiver.
- `src/ifl/ledger.py`: the SQLite record.
- `src/ifl/cli.py`: argument parsing and the commands.

## Tests

The tests use no network. `tests/fakes.py` is a fake Immich: albums with
users and roles, assets with the fields the move reads, sidecars, edits,
metadata, shared links, partners, workflows, queues, and a log of every write.
It enforces the access rules that matter, so a test can prove which key did
what. The `patch_world` helper points a module's imported helpers at one fake.

Test files follow the source: `test_observer`, `test_convert`,
`test_reconcile`, `test_move`, `test_reuploads`, `test_process`,
`test_daemon` (which runs the receiver on a real aiohttp test server),
`test_sidecar`, `test_config`, `test_ledger`.

## The dev instance

Testing happens on a separate Immich instance, never on production.
`AGENTS.local.md`, which is gitignored, describes the one this project uses:
its URL, host, accounts, where the keys live, and how to deploy to it.

What is true of any dev instance for this project:

- It has an admin account, a family account, and the member accounts alice,
  bob, and carol. One shared password works for all of them in the web UI.
  The scripts below read it from `IFL_DEV_PASSWORD`.
- Keys and the password are never in the repo. The service reads the keys from
  `config.toml` in the checkout, which is gitignored. To rebuild it, copy
  `config.example.toml` and fill in the keys.
- The webhook token lives only in the two config files, the local one and the
  one on the dev host. They must agree. If they differ, `observe` reports every
  member's workflow as needing a fix.
- On the dev host the service runs only as the container. `ifl daemon` is the
  container's default command. For a one-off command next to it, use
  `docker compose run --rm ifl observe` (or `process`, `show-config`, ...).
  The daemon and a one-off share the ledger file safely. Do not run two
  `process`-style commands at once.
  `docker compose logs --no-log-prefix --since 5m` shows the daemon's log.
- The dev config has a `[webhook]` section that points at
  `http://ifl:8080/webhook/asset-created`. The daemon's first pass creates the
  per-member Immich workflows.

### The scripts

Two scripts act as the users do, through password login. Sharing an album or
creating a key is a thing a member does in the UI, and the service keys
deliberately cannot do it.

`scripts/seed_dev.py` uploads eight deterministic JPEGs with EXIF as the
members. It also creates the albums the observer expects. Those are the family
dropbox and "Family Trip", alice's "Beach 2025" shared with the family, and
alice's "Alice & Bob" shared only with bob. Run it as:

```
IFL_DEV_PASSWORD=... uv run --group seed python scripts/seed_dev.py
```

`--reset` wipes all five accounts' assets and albums first. Workflows and
partner shares survive, which is fine. Reconcile recreates them anyway.
Without `--reset`, a second run is a no-op, because Immich dedupes uploads.
Dedupe also counts trashed assets, so a moved photo is not re-uploaded until
the member empties the trash. `dev_scenarios.py reupload` does that.

`scripts/dev_scenarios.py` sets up, as a user does, each situation that the
service was verified against, one subcommand each. Its docstring lists them:

- albums to convert, with a description and an outside viewer, a merge into
  an existing family album, and several at once for the limit
- a broken reconcile state
- a bob-owned album that holds alice's photo
- a fully edited photo
- re-uploads after a purge
- a re-share after the family trashed its copy

Most scenarios assume that the seed has run and the photos have not moved
yet. After a full cycle, run `seed_dev.py --reset` and then the seed to get
back to the start.

### Key permissions

A key cannot change its own permissions. When a change adds permissions, run:

```
IFL_DEV_PASSWORD=... IFL_ADMIN_EMAIL=... uv run --group seed python scripts/dev_scenarios.py keys
```

It logs in with the password and brings every key up to the sets in
`accounts.py`. The web UI works too, under Account Settings, API Keys.
`observe` fails at startup until the keys match.

## Verifying on the dev instance

Anything that deletes gets verified by hand before it runs automatically.
Each action landed as a CLI command first. It was run by hand against the dev
instance, with a scenario set up as a user does. The result was checked from
each account's point of view through the API. `immich-notes.md` keeps what
those runs established. Keep that habit for anything that deletes.

## Branches

`main` is reviewed code. Each change lives on its own feature branch, which is
what the dev container deploys for testing. A branch squash-merges into
`main` once it has been reviewed and run on the dev instance. Commits carry no
tool attribution.

## Writing style

The docs follow two rule sets, chosen by reader:

- `how-it-works.md`, `for-members.md`, and the opening of `README.md` are
  plain English in the Orwell and Gowers tradition: active voice, concrete
  nouns, the condition before the command, no AI vocabulary. These docs
  explain, so voice and rhythm matter.
- `cli.md`, `configuration.md`, `deployment.md`, `immich-notes.md`, this file,
  the rest of `README.md`, and `AGENTS.md` follow ASD-STE100 Simplified
  Technical English, in pragmatic mode: sentences of 20 words for procedures
  and 25 for descriptions, the imperative for steps, no contractions, no
  "should", one name per thing. These docs are reference and procedure.

The Claude Code plugin at https://github.com/b1rdmania/claude-plain-english-skill
has one skill for each set and applies the rules. Either set is fine to write
by hand. When a doc's style is wrong, re-author it rather than patch it. Then
compare the result against the old text claim by claim, so that no fact is
lost.
