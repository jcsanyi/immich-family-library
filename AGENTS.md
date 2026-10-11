# Notes for agents

## Orientation

README.md says what the system is and does, in user-facing terms, and defines the vocabulary (member, family account, dropbox, reclaim). Read it first.

`docs/` is the design and the documentation in one. Read `docs/how-it-works.md` fully before doing anything: it covers the flows, the state model, what metadata is carried, the mobile re-upload problem and how we handle it, and pacing. `docs/configuration.md` has the config reference and the API key permissions, `docs/immich-notes.md` what we've verified about Immich's behaviour and the upstream references, `docs/development.md` the test setup and the dev-instance history, `docs/cli.md` and `docs/deployment.md` the operator side, and `docs/for-members.md` is the hand-out for family members. Work still to do lives in the GitHub issues, not in a document.

Any part of the design can still change through conversation with the user. When it does, update the relevant doc in the same change so it never disagrees with what was decided. If code and the docs disagree and there's no record of a decision, ask rather than assume either is right.

Two terms the docs use that README.md doesn't:

- The one rule: a member-owned asset found in a family-owned album gets moved to the family account. Every flow is a way of getting an asset into that position.
- Ledger: the local SQLite record keyed by checksum of what moved, from whom, and where it went. A record, not a controller: the move path derives its state from Immich.

## Decisions

- Language is Python 3.12, managed with `uv`. Tests with `pytest`, lint and format with `ruff`.
- API client is `immichpy` (https://github.com/timonrieger/immichpy), generated from the Immich OpenAPI spec and async on aiohttp. Pin it to the Immich version in use; the repo's COMPATIBILITY.csv maps package versions to Immich versions (9.0.1 for Immich 3.3.1). Use the generated `upload_asset` for moves, not the path-based `assets.upload()` convenience helper, which is a bulk uploader. If the project goes quiet, the fallback is regenerating from the spec with the same generator.
- The service is async throughout. The webhook receiver uses aiohttp's server so there's one HTTP stack.
- Deployment is a standalone container in the Immich compose stack, with a volume for the SQLite ledger. Not an Immich plugin: the plugin sandbox can't hold other users' keys, keep state, or poll. The only Immich-side piece is a workflow with a webhook action pointing at the container.

## Instance state

As of October 2026 the prod Immich instance is on 3.0.x with an upgrade to 3.3 planned shortly. Develop against 3.3: cluster groups came in 3.2, people sharing in 3.3. Don't propose workarounds for older versions.

Face detection is currently off on prod. It gets enabled after all accounts are in a cluster group, so no facial recognition reset is needed. Don't rely on faces or people sharing existing yet.

Testing happens on a separate dev instance, never production.

## Dev instance

- URL: https://dev.photos.csanyi.ca, nginx on the home server (tarsus) proxying to host port 2283.
- Runs under rootless docker as user `immich-dev` on tarsus. `ssh immich-dev` works from this machine (alias in `~/.ssh/config`, key `~/.ssh/id_ed25519_immich_dev`). Rootless docker was installed with `dockerd-rootless-setuptool.sh install`; the user's docker context is `rootless`, and `loginctl enable-linger immich-dev` keeps it running without a login session (`disable-linger` undoes that). If a shell gets "permission denied" on the docker socket, `export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock`.
- Compose files in `/data/immich-dev`: the stock Immich `docker-compose.yml`, an override naming the project `immich-dev` and containers `immich_dev_*`, and `.env` pinning `IMMICH_VERSION=v3.3.1`. Machine learning is behind the `ml` compose profile and disabled until the faces stage. Library and database are plain directories (`library/`, `pgdata/`); a full reset is `docker compose down && rm -rf library pgdata`.
- Accounts: admin, family, and members alice, bob, carol. Emails, user ids, API keys (including `ADMIN_API_KEY`, a key on the admin account with only the queue and identity permissions) and one shared password (`DEV_PASSWORD`, valid for all five accounts in the web UI) are in `/data/immich-dev/accounts.env` on tarsus, never in the repo. The webhook token is only in the two config files (local and tarsus); they must agree, or `observe` reports every member's workflow as needing a fix.
- Key permissions: a key can't change its own, so when a stage adds permissions, update the dev keys with `IFL_DEV_PASSWORD=... IFL_ADMIN_EMAIL=... uv run --group seed python scripts/dev_scenarios.py keys`, which logs in with the password and brings every key up to the sets in `accounts.py` (or do it in the web UI under Account Settings, API Keys). `observe` fails at startup until the keys match.
- Test situations: `scripts/dev_scenarios.py` sets up, as the users would, each situation stages 2 to 7 were verified against: albums to convert (with a description and an outside viewer, a merge into an existing family album, several for the limit), a broken reconcile state, a bob-owned album holding alice's photo, a fully edited photo, re-uploads after a purge, a re-share after the family trashed its copy. Its docstring lists them. Most assume the seed has run and the photos haven't moved yet; after a full cycle, `seed_dev.py --reset` then the seed gets back to the start.
- Local config on this machine: `config.toml` in the repo checkout (gitignored) points at the public dev URL with the real keys. To rebuild it, copy `config.example.toml` and fill in the keys from `accounts.env`.
- Sample data: `scripts/seed_dev.py` uploads eight deterministic JPEGs with EXIF as the members and sets up the albums the observer expects (family dropbox and "Family Trip", alice's "Beach 2025" shared with the family, alice's "Alice & Bob" shared only with bob). It logs in with the shared password, so run it as `IFL_DEV_PASSWORD=... uv run --group seed python scripts/seed_dev.py`; `--reset` wipes all five accounts' assets and albums first (workflows and partner shares survive, which is fine; reconcile would recreate them anyway). Re-running without `--reset` is a no-op thanks to Immich's dedupe, which also counts trashed assets, so a moved photo isn't re-uploaded until the member's trash is emptied; that's what `dev_scenarios.py reupload` does.
- There is no Python on tarsus; the service only runs there as the container (see deploy steps below).

## How to work on this

Work one GitHub issue, or one piece of one, at a time. Don't scaffold ahead. Confirm verification results with the user before moving on.

The system deletes originals out of members' accounts, so mistakes are expensive. Anything about Immich's behaviour that `docs/immich-notes.md` doesn't mark as verified is an assumption until checked on the dev instance; the `verify` label on the issues tracks what's still open.

## Looking things up

- The Immich API reference site is a JavaScript app and fetches as an empty page. Use the OpenAPI spec instead: https://raw.githubusercontent.com/immich-app/immich/main/open-api/immich-openapi-specs.json. Each path carries an `x-immich-permission` field naming the API key scope it needs.
- For behaviour the spec doesn't describe (job ordering, sidecar handling, what extraction overwrites), read the server source directly. server/src/services/metadata.service.ts is where sidecar read and write live.
- The GitHub discussions and issues listed under "Upstream references" in `docs/immich-notes.md` are the record of what upstream has said about ownership transfer, the re-upload loop and tags. Check them before assuming something has changed.

## Persistent memory

Before writing to persistent memory (e.g. the Claude Code per-project memory store), consider whether the guidance belongs in this file instead. AGENTS.md is checked in, visible to human contributors, and shared across every agent session; per-agent memory is none of those things. If a rule is repo-wide and durable rather than personal to one user or session, recommend adding it to AGENTS.md and wait for approval before either saving the memory or editing this file.

## Commits and branches

Do not add AI agent attribution to commits (no Co-Authored-By lines). The user owns all code in this repository.

Two branches matter:

- `dev` is the working branch. Commit and push to it freely, without asking, whenever there's something worth testing on the dev server. Keep commits small and messages plain. This is what gets deployed to `/data/immich-dev/ifl` on tarsus.

  Deploying `dev` to tarsus, after pushing:

  ```
  ssh immich-dev 'cd /data/immich-dev/ifl && git pull -q && \
    export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock && \
    docker compose build -q && docker compose up -d'
  ```

  That restarts the daemon (`ifl daemon` is the container's default command). For a one-off command alongside it, `docker compose run --rm ifl observe` (or `process`, `show-config`, ...); the daemon and a one-off share the ledger file safely, SQLite handles that, but don't run two `process`-style commands at once. `docker compose logs --no-log-prefix --since 5m` shows the daemon's log. The dev config has a `[webhook]` section pointing at `http://ifl:8080/webhook/asset-created` with a token also kept in the local config; the daemon's first pass creates the per-member Immich workflows. The checkout there uses a read-only deploy key, so it can pull but never push. `config/config.toml` and `data/` on tarsus are untracked and survive pulls; the config there uses `http://immich_dev_server:2283` and `ledger_path = "/data/ledger.sqlite"`, and the stack is started with `IMMICH_NETWORK=immich-dev_default` (set in an untracked `.env` next to compose.yml).

  Keep instance-specific values (hostnames, emails, network names) out of tracked files. This is a public project; examples use example.com.
- `main` is reviewed code. Never merge, rebase, or push to `main` without an explicit instruction in the current message. Completing a stage, or the user approving a plan, is not that instruction. When `dev` is ready for review, say so and stop.

Merging to `main` is the user's call after they've reviewed `dev`. When told to merge, fast-forward if possible, otherwise a merge commit; never squash away the dev history unless asked.
