# Notes for agents

## Orientation

README.md says what the system is and does, in user-facing terms, and defines the vocabulary (member, family account, dropbox, reclaim). Read it first.

DESIGN.md is the working design. Read it fully before doing anything. It covers the flows, the state model, what metadata is carried, the mobile re-upload problem and how we handle it, the API key permissions, the things that still need verifying on a dev instance, and the build stages.

It is a temporary document, not the final documentation. It exists to hold the overall goal and the stages still to come while the system is built. As each stage lands, real documentation (README, config reference, docstrings) takes over from the corresponding part of DESIGN.md, and that part can be trimmed. When nothing is left but history, delete it.

Any part of the design can still change through conversation with the user. When it does, update DESIGN.md in the same change so it never disagrees with what was decided. If code and DESIGN.md disagree and there's no record of a decision, ask rather than assume either is right.

Two terms DESIGN.md uses that README.md doesn't:

- The one rule: a member-owned asset found in a family-owned album gets moved to the family account. Every flow is a way of getting an asset into that position.
- Ledger: the local SQLite record keyed by checksum of what moved, from whom, and where it went. A record, not a controller: the move path derives its state from Immich.

## Decisions

- Language is Python 3.12, managed with `uv`. Tests with `pytest`, lint and format with `ruff`.
- API client is `immichpy` (https://github.com/timonrieger/immichpy), generated from the Immich OpenAPI spec and async on aiohttp. Pin it to the Immich version in use; the repo's COMPATIBILITY.csv maps package versions to Immich versions (9.0.1 for Immich 3.3.1). Use the generated `upload_asset` for moves, not the path-based `assets.upload()` convenience helper, which is a bulk uploader. If the project goes quiet, the fallback is regenerating from the spec with the same generator.
- The service is async throughout. The webhook receiver uses aiohttp's server so there's one HTTP stack.
- Deployment is a standalone container in the Immich compose stack, with a volume for the SQLite ledger. Not an Immich plugin: the plugin sandbox can't hold other users' keys, keep state, or poll. The only Immich-side piece is a workflow with a webhook action pointing at the container.

## Instance state

As of October 2026 the Immich instance is on 3.0.x with an upgrade to 3.3 planned shortly. Develop against 3.3: cluster groups came in 3.2, people sharing in 3.3. Don't propose workarounds for older versions.

Face detection is currently off on the instance. It gets enabled after all accounts are in a cluster group, so no facial recognition reset is needed. Don't rely on faces or people sharing existing yet.

Testing happens on a separate dev instance, never production. The dev instance is https://dev.photos.csanyi.ca, running under rootless docker as user `immich-dev` on the home server (`ssh immich-dev`, compose files in `/data/immich-dev`). Accounts, user IDs and API keys are in `/data/immich-dev/accounts.env` on that host, never in the repo. Dev accounts: admin, family, and members alice, bob, carol.

## How to work on this

Build and test one stage at a time, in the order listed under "Stages" in DESIGN.md. Don't scaffold later stages ahead of time. Confirm verification results with the user before moving on to the next stage.

The system deletes originals out of members' accounts, so mistakes are expensive. Several API behaviours (sidecar precedence, cluster-group recognition on new uploads, the mobile re-upload loop) are assumptions until checked on the dev instance. DESIGN.md has the list.

## Looking things up

- The Immich API reference site is a JavaScript app and fetches as an empty page. Use the OpenAPI spec instead: https://raw.githubusercontent.com/immich-app/immich/main/open-api/immich-openapi-specs.json. Each path carries an `x-immich-permission` field naming the API key scope it needs.
- For behaviour the spec doesn't describe (job ordering, sidecar handling, what extraction overwrites), read the server source directly. server/src/services/metadata.service.ts is where sidecar read and write live.
- GitHub discussions and issues referenced in DESIGN.md are the record of what upstream has said about ownership transfer, the re-upload loop and tags. Check them before assuming something has changed.

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

  For a one-off run instead of the long-running service, replace `up -d` with `run --rm ifl observe --once` (or whatever command is being tested). The checkout there uses a read-only deploy key, so it can pull but never push. `config/config.toml` and `data/` on tarsus are untracked and survive pulls; the config there uses `http://immich_dev_server:2283` and `ledger_path = "/data/ledger.sqlite"`, and the stack is started with `IMMICH_NETWORK=immich-dev_default` (set in an untracked `.env` next to compose.yml).

  Keep instance-specific values (hostnames, emails, network names) out of tracked files. This is a public project; examples use example.com.
- `main` is reviewed code. Never merge, rebase, or push to `main` without an explicit instruction in the current message. Completing a stage, or the user approving a plan, is not that instruction. When `dev` is ready for review, say so and stop.

Merging to `main` is the user's call after they've reviewed `dev`. When told to merge, fast-forward if possible, otherwise a merge commit; never squash away the dev history unless asked.
