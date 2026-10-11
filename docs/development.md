# Development

Python 3.12, managed with `uv`. Tests with `pytest`, lint and format with
`ruff`. The API client is `immichpy`, generated from Immich's OpenAPI spec,
pinned to the Immich version in use (9.0.1 for 3.3.1; the client repo's
COMPATIBILITY.csv maps versions).

```
uv sync
uv run pytest -q
uv run ruff check . && uv run ruff format .
uv run ifl observe -c config.toml
```

## Layout

- `src/ifl/config.py`: the config file, its defaults, and `show-config`'s view of it.
- `src/ifl/accounts.py`: one client per account, and the startup checks.
- `src/ifl/immich.py`: thin helpers over the generated client; every Immich call the service makes is here.
- `src/ifl/observer.py`: the three scans, read-only. `observe` reports them; `process` acts on them.
- `src/ifl/convert.py`, `reconcile.py`, `move.py`: the three actions. `sidecar.py` is the XMP tag rewrite.
- `src/ifl/process.py`: one pass. `daemon.py`: the pass on an interval plus the webhook receiver.
- `src/ifl/ledger.py`: the SQLite record.
- `src/ifl/cli.py`: argument parsing and the commands.

## Tests

No network. `tests/fakes.py` is a fake Immich: albums with users and roles,
assets with the fields the move reads, sidecars, edits, metadata, shared
links, partners, workflows, queues, and a log of every write. It enforces the
access rules that matter, so a test can prove which key did what. The
`patch_world` helper points a module's imported helpers at one fake. Test
files follow the source: `test_observer`, `test_convert`, `test_reconcile`,
`test_move`, `test_reuploads`, `test_process`, `test_daemon` (which runs the
receiver on a real aiohttp test server), `test_sidecar`, `test_config`,
`test_ledger`.

## The dev instance

Testing happens on a separate Immich instance, never production. It needs an
admin, a family account and a few member accounts sharing one password, with
API keys in a config file. `AGENTS.local.md`, which is gitignored, describes
the one this project uses.

Two scripts act as the users would, through password login, because sharing
an album or creating a key is a thing a member does in the UI and the service
keys deliberately can't:

- `scripts/seed_dev.py` uploads eight deterministic JPEGs with EXIF as the
  members and sets up the starting albums. `--reset` wipes assets and albums
  first. Re-running without it is a no-op (dedupe counts trashed assets).
- `scripts/dev_scenarios.py` arranges the situations each stage was verified
  against, one subcommand each; its docstring lists them. `keys` brings the
  dev API keys up to the permission sets in `accounts.py`.

Both read the shared password from `IFL_DEV_PASSWORD`.

## How stages were verified

Each stage landed as a CLI command first, run by hand against the dev
instance with a scenario set up as a user would, and checked from each
account's point of view through the API before anything ran automatically.
`docs/immich-notes.md` keeps what those runs established. The habit is worth
keeping for anything that deletes.

## Branches

`dev` is the working branch and what the dev container deploys. `main` is
reviewed code, updated by squash-merging `dev` once a stage has been looked
at. Commits carry no tool attribution.
