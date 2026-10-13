# Development

The language is Python 3.12, managed with `uv`. Tests use `pytest`. Lint and
format use `ruff`.

## Local setup

You need `uv` (https://docs.astral.sh/uv/). It installs the Python version in
`.python-version` by itself.

1. Clone the repository and run `uv sync`. This creates `.venv` with the
   locked dependencies and the `ifl` command.
2. Copy `config.example.toml` to `config.toml` and fill in the URL and the
   keys of the dev instance. Delete the `lock_path` line: its value is for the
   container, and the default is correct for a checkout. `config.toml` is
   gitignored. `AGENTS.local.md`
   says where the keys live.
3. Run `uv run ifl show-config` to check the config. Then run
   `uv run ifl observe` for a read-only pass. Both read `config.toml` in the
   current directory. [cli.md](cli.md) describes each command.

The local checkout talks to the dev instance over the network. Never point it
at production. The daemon runs on the dev host, not on your machine. See "The
dev instance" below.

With [direnv](https://direnv.net/) installed, the committed `.envrc` does the
sync for you when you enter the directory, and puts `.venv/bin` and `bin/` on
`PATH`. Then `ifl`, `pytest`, `ruff`, `lint`, `format` and `tests` run without
a path or the `uv run` prefix. Run `direnv allow`
once to approve the file. Personal settings go in `.envrc.local`, which is
git-ignored and which `.envrc` loads when it exists.

Before you commit, run the checks. The three scripts in `bin/` work from any
directory, with or without direnv. Each runs the tool version that `uv.lock`
pins, through `uv run`, so the result matches CI.

```
bin/lint      # ruff check and ruff format --check; changes nothing
bin/format    # ruff's safe fixes, then ruff format
bin/tests     # the unit tests, with a coverage table of the files that have gaps
```

`bin/tests` passes its arguments to `pytest`. Every pytest run reports
coverage, which is set in `pyproject.toml`. For one test, the table is
noise, so use `bin/tests --no-cov -k name`. For the HTML report, use
`bin/tests --cov-report=html` and open `htmlcov/index.html`. CI runs
`bin/lint` and `bin/tests`.

Do not use `uv format` for this project. It fetches its own `ruff`, which can
be a different version from the pinned one, and the two can format a file
differently.

## The API client

The API client is `immichpy` (https://github.com/timonrieger/immichpy),
generated from Immich's OpenAPI spec and async on aiohttp. It is pinned to
the Immich version in use: `immichpy` 9.0.1 for Immich 3.3.1. The client
repo's `COMPATIBILITY.csv` maps package versions to Immich versions. This is
the one place in the docs that records the pin. When it changes, update it
here.

A new `immichpy` major is generated from a new Immich minor and drops support
for older servers. Dependabot opens a pull request for each one, and the tests
on it usually fail, because the fake Immich builds the real response models
and the `test_immich` stubs carry the real client signatures. Each failure
names a call or a model that the new client no longer accepts. That pull
request is the prompt to upgrade. Do these steps together:

1. Upgrade the dev instance to the matching Immich version.
2. Take the new pin, and raise `MIN_SERVER` in `src/ifl/accounts.py` to that
   Immich version. Update the version in `README.md` and in this section.
3. Re-check every point in `immich-notes.md` on the dev instance, and fix the
   code and the docs where Immich changed.

Patch and minor releases of `immichpy` keep the same minimum server. Merge
those when the tests pass.

The generated `download_asset` and `upload_asset` hold the whole file in
memory, so the move does not use them. `download_original` and `upload_asset`
in `immich.py` call the request builders beneath those methods
(`_download_asset_serialize`, `_upload_asset_serialize`) and give aiohttp a
stream. Do not use the path-based `assets.upload()` helper, which is a bulk
uploader. If the immichpy project goes quiet, the fallback is to regenerate
the client from the spec with the same generator. The builders are part of
what the generator makes for every call.

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
- `src/ifl/lock.py`: the run lock.
- `src/ifl/cli.py`: argument parsing and the commands.

## Errors

Two levels catch errors, and each has one job.

- **One piece of work.** One album, one fix, one photo, or one webhook asset.
  The loop catches every error, records it in its result, and continues with
  the next piece. The loops are `convert_albums`, `reconcile`, `move_assets`,
  and `Daemon.handle_batch`.
- **One process.** `main` in `cli.py` maps an error to an exit code. The two
  loops of the daemon log an error and continue.

Do not catch an error between these two levels. Let it rise.

Three things in `immich.py` keep the reports the same everywhere:

- `Failure` is the base class for a condition that the service found and can
  describe. `MoveError`, `ConvertError` and `DownloadMismatch` derive from it.
  When Immich refuses something that the code checks for, raise a `Failure`,
  not a `RuntimeError`.
- `log_failure` logs one piece of work that failed. A `Failure`, or an error
  from Immich or the network (`IMMICH_ERRORS`), is one line. Any other error
  is a fault in the service, and the log keeps its traceback.
- `describe_error` gives one line for any error. Use it when an error goes
  into a message. The text of the client for an Immich error has four lines
  and all the response headers.

## Tests

The tests use no network. `tests/fakes.py` is a fake Immich: albums with
users and roles, assets with the fields the move reads, sidecars, edits,
metadata, shared links, partners, workflows, queues, and a log of every write.
It enforces the access rules that matter, so a test can prove which key did
what. The `patch_world` helper points a module's imported helpers at one fake.

The fake's assets and albums are real `immichpy` response models. `FakeAsset`
builds an `AssetResponseDto` and `FakeWorld.album` builds an `AlbumResponseDto`
and runs it through `summarize_album`. The models validate every field, on
construction and on assignment, so the fake cannot drift from the shapes the
service reads when the `immichpy` pin changes.

Test files follow the source: `test_observer`, `test_convert`,
`test_reconcile`, `test_move`, `test_reuploads`, `test_process`,
`test_daemon` (which runs the receiver on a real aiohttp test server),
`test_sidecar`, `test_config`, `test_lock`, and `test_cli` for the exit
codes. Two files test the layers the
fake replaces, against stub clients that return real models: `test_immich`
covers every adapter helper, and `test_accounts` covers the startup checks on
each key.

The two transfer helpers are the exception. They go beneath the generated
methods, so no signature covers them. Their tests in `test_immich` send real
requests to an aiohttp server on a local socket, and examine what arrives.

Each stub method in `test_immich` carries the signature of the installed
client's real method, and pydantic validates every call against it. The stub
refuses a call that the real client would refuse. So when the `immichpy` pin
changes and a method loses a parameter, changes a type, or disappears, the
tests fail before the code reaches the dev instance.

## Continuous integration

The `Tests` workflow in `.github/workflows/tests.yml` runs on every pull
request to `main` and on every push to `main`. It installs the locked
dependencies with `uv sync --frozen`, then runs `bin/lint` and `bin/tests`.
The job summary shows the coverage table. The HTML coverage report is an artifact of the run, kept for
14 days.

Dependabot (`.github/dependabot.yml`) opens pull requests each week for
updates to the GitHub Actions and to the Python dependencies in `uv.lock`,
transitive dependencies included. Minor and patch updates to Python packages
come as one grouped pull request. A major release gets its own pull request.

## The dev instance

Testing happens on a separate Immich instance, never on production.
Use `AGENTS.local.md` (which is gitignored) to describe the dev instance this
project uses: its URL, host, accounts, where the keys live, and how to deploy
to it.

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
  The daemon holds the run lock, so a one-off command that writes to Immich
  exits 3 beside it. `observe` is fine.
  `docker compose logs --no-log-prefix --since 5m` shows the daemon's log.
- The dev config has a `[webhook]` section that points at
  `http://ifl:8080/webhook/asset-created`. The daemon's first pass creates the
  per-member Immich workflows.

### The scripts

Two scripts act as the users do, through password login. Sharing an album or
creating a key is a thing a member does in the UI, and the service keys
deliberately cannot do it.

`bin/dev/seed_dev.py` uploads eight deterministic JPEGs with EXIF as the
members, plus one two-second video clip as alice. The clip is the checked-in
file `bin/dev/fixtures/seed-clip.mp4`, so that its bytes never change. The
script also creates the albums the observer expects. Those are the family
dropbox and "Family Trip" (which holds alice's trip photo and her clip),
alice's "Beach 2025" shared with the family, and alice's "Alice & Bob" shared
only with bob. Run it as:

```
IFL_DEV_PASSWORD=... uv run --group seed python bin/dev/seed_dev.py
```

`--reset` wipes all five accounts' assets and albums first. Workflows and
partner shares survive, which is fine. Reconcile recreates them anyway.
Without `--reset`, a second run is a no-op, because Immich dedupes uploads.
Dedupe also counts trashed assets, so a moved photo is not re-uploaded until
the member empties the trash. `dev_scenarios.py reupload` does that.

`bin/dev/dev_scenarios.py` sets up, as a user does, each situation that the
service was verified against, one subcommand each. Its docstring lists them:

- albums to convert, with a description and an outside viewer, a merge into
  an existing family album, and several at once for the limit
- a broken reconcile state
- a bob-owned album that holds alice's photo
- a private album of bob's, which alice is not on, that holds alice's photo
- a fully edited photo
- re-uploads after a purge
- a re-share after the family trashed its copy
- a live photo, and a locked, a hidden and an archived photo, in a family
  album
- a member's photo in one of their own shared links, which blocks its move
- a photo that the family got by a direct upload while a member still holds it
- a video of 2 GB or more in a family album

Most scenarios assume that the seed has run and the photos have not moved
yet. After a full cycle, run `seed_dev.py --reset` and then the seed to get
back to the start.

### Key permissions

A key cannot change its own permissions. When a change adds permissions, run:

```
IFL_DEV_PASSWORD=... IFL_ADMIN_EMAIL=... uv run --group seed python bin/dev/dev_scenarios.py keys
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
`main` once it has been reviewed and run on the dev instance. Commits, pull
requests, issues and comments carry no tool attribution.

## Writing style

Each doc follows one of two rule sets, depending on who reads it and why:

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
