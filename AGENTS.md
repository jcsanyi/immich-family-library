# Notes for agents

## Orientation

Read `README.md` first. It says what the system does, in user-facing terms, and
defines the vocabulary: member, family account, dropbox, reclaim.

`docs/` is the design and the documentation in one. Read `docs/how-it-works.md`
in full before you do anything. It covers the flows, the state model, the
metadata that travels with an asset, the mobile re-upload problem and how we
handle it, and pacing. The other docs:

- `docs/configuration.md`: the config reference and the API key permissions.
- `docs/immich-notes.md`: what we have verified about Immich, and the upstream
  references.
- `docs/development.md`: the test setup and the history of the dev instance.
- `docs/cli.md` and `docs/deployment.md`: the operator side.
- `docs/for-members.md`: the hand-out for family members.

Work still to do lives in the GitHub issues, not in a document.

Any part of the design can change through conversation with the user. When it
does, update the relevant doc in the same change, so that the docs never
disagree with the decision. If the code and the docs disagree, and there is no
record of a decision, ask. Do not assume that either one is right.

Two terms that the docs use and `README.md` does not:

- The one rule: the service moves a member-owned asset found in a family-owned
  album to the family account. Every flow is a way to get an asset into that
  position.
- Ledger: the local SQLite record, keyed by checksum, of what moved, from whom,
  and where it went. It is a record, not a controller. The move path derives
  its state from Immich.

## Decisions

- The language is Python 3.12, managed with `uv`. Tests use `pytest`. Lint and
  format use `ruff`.
- The API client is `immichpy` (https://github.com/timonrieger/immichpy). It is
  generated from the Immich OpenAPI spec and is async on aiohttp. Pin it to the
  Immich version in use. `COMPATIBILITY.csv` in the immichpy repo maps package
  versions to Immich versions (9.0.1 for Immich 3.3.1). For moves, use the
  generated `upload_asset`. Do not use the path-based `assets.upload()`
  helper, which is a bulk uploader. If the immichpy project goes quiet, the
  fallback is to regenerate the client from the spec with the same generator.
- The service is async throughout. The webhook receiver uses the aiohttp
  server, so that there is one HTTP stack.
- The service deploys as a standalone container in the Immich compose stack,
  with a volume for the SQLite ledger. It is not an Immich plugin: the plugin
  sandbox cannot hold other users' keys, keep state, or poll. The only piece on
  the Immich side is a workflow with a webhook action that points at the
  container.

## Target Immich version

Develop against Immich 3.3. Cluster groups came in 3.2 and people sharing in
3.3. Do not propose workarounds for older versions.

Face detection stays off until every account is in a cluster group, so that no
facial recognition reset is needed. Do not rely on faces or on people sharing.

## Dev instance

Testing happens on a separate dev instance, never on production.
`AGENTS.local.md` describes the instance this project uses: its URL, host,
accounts, where the keys live, and how to deploy to it. That file is
gitignored. If it is missing, ask the user for it before you touch the dev
instance.

What is true of any dev instance for this project:

- It has an admin account, a family account, and the member accounts alice,
  bob, and carol. One shared password works for all of them in the web UI.
  The scripts below read it from `IFL_DEV_PASSWORD`.
- Keys and the password are never in the repo. The service reads the keys from
  `config.toml` in the checkout, which is gitignored. To rebuild it, copy
  `config.example.toml` and fill in the keys.
- A key cannot change its own permissions. When a stage adds permissions, run
  `IFL_DEV_PASSWORD=... IFL_ADMIN_EMAIL=... uv run --group seed python scripts/dev_scenarios.py keys`.
  It logs in with the password and brings every key up to the sets in
  `accounts.py`. The web UI works too, under Account Settings, API Keys.
  `observe` fails at startup until the keys match.
- The webhook token lives only in the two config files, the local one and the
  one on the dev host. They must agree. If they differ, `observe` reports every
  member's workflow as needing a fix.
- `scripts/seed_dev.py` uploads eight deterministic JPEGs with EXIF as the
  members. It also creates the albums the observer expects. Those are the
  family dropbox and "Family Trip", alice's "Beach 2025" shared with the
  family, and alice's "Alice & Bob" shared only with bob. Run it as
  `IFL_DEV_PASSWORD=... uv run --group seed python scripts/seed_dev.py`.
  `--reset` wipes all five accounts' assets and albums first. Workflows and
  partner shares survive, which is fine. Reconcile recreates them anyway.
  Without `--reset`, a second run is a no-op, because Immich dedupes uploads.
  Dedupe also counts trashed assets, so a moved photo is not re-uploaded until
  the member empties the trash. `dev_scenarios.py reupload` does that.
- `scripts/dev_scenarios.py` sets up, as a user does, each situation that
  stages 2 to 7 were verified against. Its docstring lists them:
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
- On the dev host the service runs only as the container. `ifl daemon` is the
  container's default command. For a one-off command next to it, use
  `docker compose run --rm ifl observe` (or `process`, `show-config`, ...).
  The daemon and a one-off share the ledger file safely. Do not run two
  `process`-style commands at once.
  `docker compose logs --no-log-prefix --since 5m` shows the daemon's log.
- The dev config has a `[webhook]` section that points at
  `http://ifl:8080/webhook/asset-created`. The daemon's first pass creates the
  per-member Immich workflows.

## How to work on this

Work on one GitHub issue, or one piece of one, at a time. Do not scaffold
ahead. Confirm verification results with the user before you move on.

CAUTION: The system deletes originals out of members' accounts. Mistakes are
expensive. Anything about Immich's behavior that `docs/immich-notes.md` does
not mark as verified is an assumption until you check it on the dev instance.
The `verify` label on the issues tracks what is still open.

## Looking things up

- The Immich API reference site is a JavaScript app and fetches as an empty
  page. Use the OpenAPI spec instead:
  https://raw.githubusercontent.com/immich-app/immich/main/open-api/immich-openapi-specs.json.
  Each path carries an `x-immich-permission` field that names the API key
  scope it needs.
- For behavior the spec does not describe (job ordering, sidecar handling, what
  extraction overwrites), read the server source. Sidecar read and write live
  in `server/src/services/metadata.service.ts`.
- The GitHub discussions and issues under "Upstream references" in
  `docs/immich-notes.md` record what upstream has said about ownership
  transfer, the re-upload loop, and tags. Read them before you assume that
  something has changed.

## Persistent memory

Before you write to persistent memory, ask whether the guidance belongs in
this file instead. The Claude Code per-project memory store is one example.
`AGENTS.md` is checked in, visible to human contributors, and shared across
every agent session. Per-agent memory is none of those things. If a rule is
repo-wide and durable, rather than personal to one user or session, recommend
adding it to `AGENTS.md`. Wait for approval before you save the memory or edit
this file.

## Commits and branches

Do not add AI agent attribution to commits (no `Co-Authored-By` lines). The
user owns all code in this repository.

Two branches matter.

`dev` is the working branch. Commit and push to it freely, without asking,
whenever there is something worth testing on the dev instance. Keep commits
small and messages plain. After you push, deploy with the command in
`AGENTS.local.md`. That rebuilds the container and restarts the daemon. The
checkout on the dev host uses a read-only deploy key, so it can pull but never
push. Its config and ledger are untracked and survive pulls.

Keep instance-specific values (hostnames, emails, network names) out of
tracked files. This is a public project. Examples use example.com.

`main` is reviewed code. Never merge, rebase, or push to `main` without an
explicit instruction in the current message. Completing a stage is not that
instruction. The user approving a plan is not that instruction either. When
`dev` is ready for review, say so and stop.

Merging to `main` is the user's call, after they have reviewed `dev`. When
told to merge, fast-forward if possible. Otherwise make a merge commit. Unless
the user asks for it, do not squash away the dev history.
