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
- `src/ifl/convert.py`, `reconcile.py`, `move.py`: the three actions. `move.py` also takes photos from an external library out of the dropbox. `sidecar.py` is the XMP tag rewrite.
- `src/ifl/process.py`: one pass. `daemon.py`: the pass on an interval, plus the webhook receiver.
- `src/ifl/lock.py`: the run lock.
- `src/ifl/cli.py`: argument parsing and the commands.

## Errors

### Where an error from Immich gets its type

Each `Account` wraps its client in `guard` (`immich.py`) when it is made, so
no account holds a bare client. The wrapper puts every call into the client
inside `immich_call`. Whatever the call raises
comes out as one type, `ImmichError`. That covers an error answer from
Immich, a reply that the client cannot read, and a network failure. The
wrapper finds the methods of the client as the code uses them, so a new
client version needs no change here.

The reason is that the type of an error does not say where the error came
from. A timeout, an `OSError` and a validation error can each come from
Immich or from a mistake in the service. The place does say it: an error
that comes out of a call to the client is from Immich. So that decision is
made one time, at the call, and no code above `immich.py` examines the type
of a client or network error.

An `ImmichError` holds the client method (`call`), the status if Immich
answered (`status`), the line of the service that wanted the call (`place`),
and the original error as its cause. `reason` is the same report without the
call and the place, for a message that already gives them. `refused` is true
when Immich answered 400, 401 or 403: Immich understood the request and said
no to the key. An outage, a 5xx status and a reply that cannot be read are
not refusals. Another 4xx status is not one either, because a proxy in front
of Immich can send it. Its text is one line:
`Immich answered 403 to albums.get_all_albums: Forbidden (observer.py:148 in scan_albums)`.

The place is the nearest line outside `immich.py`. One client method can
serve some parts of the service, and the method alone does not say which part
made the call.

An argument that the client refuses is the one error from inside a call
that is not from Immich. The client checks the arguments of a method before
it sends a request. The wrapper lets that error through unchanged, so it is a
fault with a traceback. A method name that does not exist fails earlier, when
the wrapper looks it up, and is a fault for the same reason.

Three cases need to be known:

- The two transfer helpers do not go through the wrapper. They use parts of
  the client that the client does not check, so a wrong argument there would
  look like an error from Immich. Each helper builds its request outside
  `immich_call`, and puts the send and the read of the response inside it.
  Each reports under one name, `the download of an original` or
  `the upload of an asset`.
- An argument that passes the checks of the client can still fail when the
  client builds the request. That error comes out of a client call, and the
  log names Immich. The text of the error names the argument.
- During an upload, the client reads the temporary file of the service while
  it sends it. If the disk fails at that moment, the error comes out of a
  client call, and the log names Immich. The place of an error cannot show
  this case. Look at the cause in the debug traceback.

Closing a client is not a piece of work. `close_client` logs a failure at
debug and continues, so that a client that does not close cannot change the
result of a command.

### Failures and faults

`Failure` is the base class for a condition that the service found and can
describe. `ImmichError`, `MoveError`, `ConvertError` and `DownloadMismatch`
derive from it. When Immich refuses something that the code checks for, raise
a `Failure`, not a `RuntimeError`.

Any error that is not a `Failure` is a fault in the service.

- `log_failure` logs one piece of work that failed. A `Failure` is one line,
  and its traceback goes to the debug log, so `-v` shows it. A fault is an
  error with its traceback.
- `describe_error` gives one line for any error. Use it when an error goes
  into a message.

The default level of `log_failure` is error. Give it `level=logging.WARNING`
when the service corrects the failure itself and no person has to act.

### Where to catch

Two levels catch errors, and each has one job.

- **One piece of work.** One album, one fix, one photo, the photos of one
  member in the dropbox, or one webhook asset.
  The loop catches every error, records it in its result, and continues with
  the next piece. The loops are `convert_albums`, `reconcile`, `move_assets`,
  `clear_dropbox`, and `Daemon.handle_batch`.
- **One process.** `main` in `cli.py` maps an error to an exit code. The two
  loops of the daemon log an error and continue.

Between these two levels, catch an error only when the code does something
with it other than report it. An error that only needs a log line or a
different wording must rise to one of the two levels, which report it once.

Some errors are answers. When Immich refuses to add a photo to an album with
one key, the refusal tells the move to try the next key
(`_relink_member_album`). When Immich answers 400 for an asset, the answer
tells the lookup that this member cannot read it (`get_asset_if_readable`).
When Immich refuses a key at startup, the refusal is the result of the key
check (`open_account`).
A catch of that kind is correct, on two conditions:

- It catches only the errors that carry the answer. That is an `ImmichError`
  that is `refused`, or one exact status. An outage is not an answer to the
  question, so it must rise: a key that got no reply was not refused. A fault
  in the service is not an answer either, and it must keep its traceback.
- The code acts on the answer. If no key is accepted, the move raises a
  `MoveError` that lists what each key was told.

One catch has a different reason. After the move trashes the original, it
removes the original from its albums (`_remove_original`). At that point the
move is done. An error from Immich there, an outage included, becomes a
warning on the result of the move. If the error rose, the move would be
reported as failed, and no later pass would try it again, because the
original is in the trash. A fault in the service is logged as an error with
its traceback, and it becomes a warning on the result too. In each case the
move goes on to the next album.

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
14 days. The `Release` workflow also runs the `Tests` workflow, before it
publishes anything. "Releases" below describes it.

Dependabot (`.github/dependabot.yml`) opens pull requests each week for
updates to the GitHub Actions and to the Python dependencies in `uv.lock`,
transitive dependencies included. Minor and patch updates to Python packages
come as one grouped pull request. A major release gets its own pull request.

The `Dockerfile` pins the Python base image and the uv image by version and
digest, so that a build is repeatable. Dependabot also updates these pins. For
the Python image, it offers only a new patch release. A new Python minor
version is a change to `.python-version`, `requires-python` and the
`Dockerfile` together.

## Versions and the changelog

The version comes from the git tags, through hatch-vcs. No file in the
repository holds a version number. A commit with the tag `vX.Y.Z` has the
version `X.Y.Z`. A later commit has a development version, for example
`0.1.1.dev3+g1a2b3c4`: three commits after `v0.1.0`, at commit `1a2b3c4`. If
the tree has changes that are not committed, the version also gets a date, for
example `.d20261015`.

`ifl --version` prints the version. Every command except `show-config` writes
it to the log at startup, next to the version of the Immich server.

In a checkout, uv makes the version when it installs the project. uv installs
the project again when the commit or the tags change (`tool.uv.cache-keys` in
`pyproject.toml`). A change that is not committed does not cause a new
install, so the date in a checkout can be out of date. To get the current
version, run `uv sync --reinstall-package immich-family-library`.

The `Dockerfile` copies the full tree into the build stage, `.git` included,
and hatch-vcs reads the version there. Only the virtual environment goes into
the final image. `.dockerignore` lists only files that git does not track. If
it lists a tracked file, git sees that file as deleted in the build stage.
Then every image gets a date in its version, and a release image does not have
the version of its tag. The `Tests` workflow fails if `.dockerignore` lists a
tracked file. It uses the pattern rules of git, which match at least the files
that the rules of Docker match.

`CHANGELOG.md` records the changes in each release that an operator can see.
An operator is a person who runs the service. New entries go into the
`## Unreleased` section at the top. This section exists only while it has
entries.

1. If a pull request makes a change that an operator can see, add an entry
   under `## Unreleased` in the same pull request. If the section is not
   there, add it above the newest release.
2. If the operator must do a step for the upgrade, write the step under
   `### Upgrade notes`. Examples are a new key permission and a new minimum
   Immich version.
3. Do not add an entry for a change to tests, docs or internal code only.

At a release, the entries can be merged, made shorter or removed, because by
then the full set of changes is known. Each upgrade note must stay.

## Releases

A push of a version tag starts the `Release` workflow in
`.github/workflows/release.yml`. The workflow publishes two things:

- the image on Docker Hub, `jcsanyi/immich-family-library`, for amd64 and
  arm64;
- a GitHub Release, with the section of `CHANGELOG.md` for that version as its
  notes.

A release tag is `vX.Y.Z`. Its image gets the Docker tags `X.Y.Z`, `X.Y` and
`latest`. A pre-release tag adds a label that a Python version accepts, for
example `v0.2.0-rc.1`, `v0.2.0-a.1` or `v0.2.0-beta.2`. Its image gets only
its own version as a Docker tag, and its GitHub Release is marked as a
pre-release.

The workflow runs the `Tests` workflow first. Then it does these checks before
it publishes anything:

- The tag has the correct form.
- The tag is on `main`.
- `CHANGELOG.md` has a section for the version, and the section is not empty.
- The amd64 image reports the version of the tag. A dirty build stage gives a
  development version, and this check stops it.

The workflow pushes the image before it creates the GitHub Release.

### To make a release

1. Make a branch from `main`, for example `release-0.2.0`.
2. In `CHANGELOG.md`, change `## Unreleased` to `## 0.2.0 - YYYY-MM-DD`.
3. Edit the entries of the release. Keep each upgrade note.
4. Open a pull request, and merge it after review.
5. Get the merge commit of that pull request:
   `gh pr view NUMBER --json mergeCommit -q .mergeCommit.oid`.
6. Tag that commit: `git tag -a v0.2.0 -m v0.2.0 COMMIT`. Do not tag a later
   commit on `main`, because its changes are not in the notes.
7. Push the tag: `git push origin v0.2.0`.
8. Watch the `Release` run on the Actions tab of the repository.

Each tag needs its own commit. If two tags are on one commit, it is not
certain which tag hatch-vcs uses for the version.

### If the workflow fails

If the run stops before the step "Build and push", it publishes nothing. Do
these steps:

1. Delete the tag on GitHub: `git push origin :refs/tags/v0.2.0`.
2. Delete the local tag: `git tag -d v0.2.0`.
3. Correct the problem through a pull request.
4. Tag the merge commit of that pull request with the same version, and push
   the tag.

If the image is on Docker Hub and only the step "Create the GitHub Release"
failed, make the GitHub Release by hand. Use the section of `CHANGELOG.md` as
the notes:

```
gh release create v0.2.0 --verify-tag --title v0.2.0 --notes-file NOTES.md
```

Do not use a published version again. If a published release has a fault,
correct it and release the next patch version. A person who pulled the image
keeps the old image under that version.

### The Docker Hub page

`DOCKER.md` is the page on Docker Hub. The workflow
`.github/workflows/dockerhub-description.yml` pushes it when a change to it
reaches `main`. It has a copy of the quick start in the README, because a
reader on Docker Hub can need it without the README. When you change one copy,
change the other in the same pull request. Links in `DOCKER.md` must be full
URLs, because Docker Hub shows the page away from the repository.

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

The docs use the plain-english plugin for Claude Code
(https://github.com/b1rdmania/claude-plain-english-skill). The plugin has two
skills. Each part of the docs uses one of them:

- `plain-english`:
  - `how-it-works.md`
  - `for-members.md`
  - the opening of `README.md`
  - `CHANGELOG.md`, except for any upgrade notes
  - the opening of `DOCKER.md`
- `simple-english`:
  - `cli.md`
  - `configuration.md`
  - `deployment.md`
  - `immich-notes.md`
  - this file
  - the rest of `README.md`
  - `AGENTS.md`
  - the upgrade notes in `CHANGELOG.md`
  - the rest of `DOCKER.md`

The docs use Canadian spelling, for example "behaviour" and "colour". This
replaces the spelling rule of the `simple-english` skill.

You can write a doc without the plugin. When the style of a doc is wrong,
write the doc again with its skill. Do not patch it. Then compare the result
with the old text claim by claim, so that no fact is lost.
