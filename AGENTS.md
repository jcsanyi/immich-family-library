# Notes for agents

## Orientation

README.md says what the system is and does, in user-facing terms, and defines the vocabulary (member, family account, dropbox, reclaim). Read it first.

DESIGN.md is the working design. Read it fully before doing anything. It covers the flows, the state model, what metadata is carried, the mobile re-upload problem and how we handle it, the API key permissions, the things that still need verifying on a dev instance, and the build stages.

It is a temporary document, not the final documentation. It exists to hold the overall goal and the stages still to come while the system is built. As each stage lands, real documentation (README, config reference, docstrings) takes over from the corresponding part of DESIGN.md, and that part can be trimmed. When nothing is left but history, delete it.

Any part of the design can still change through conversation with the user. When it does, update DESIGN.md in the same change so it never disagrees with what was decided. If code and DESIGN.md disagree and there's no record of a decision, ask rather than assume either is right.

Two terms DESIGN.md uses that README.md doesn't:

- The one rule: a member-owned asset found in a family-owned album gets moved to the family account. Every flow is a way of getting an asset into that position.
- Ledger: the local state DB keyed by checksum recording what moved, from whom, and where it went. Written before anything is deleted.

## Decisions

- Language is Python. No decision yet on the unofficial `immich` PyPI client versus a generated or hand-written wrapper over the OpenAPI spec. Decide when stage 1 needs a client.
- Deployment is a standalone container in the Immich compose stack, with a volume for the SQLite ledger. Not an Immich plugin: the plugin sandbox can't hold other users' keys, keep state, or poll. The only Immich-side piece is a workflow with a webhook action pointing at the container.

## Instance state

As of October 2026 the Immich instance is on 3.0.x with an upgrade to 3.3 planned shortly. Develop against 3.3: cluster groups came in 3.2, people sharing in 3.3. Don't propose workarounds for older versions.

Face detection is currently off on the instance. It gets enabled after all accounts are in a cluster group, so no facial recognition reset is needed. Don't rely on faces or people sharing existing yet.

Testing happens on a separate dev instance, never production.

## How to work on this

Build and test one stage at a time, in the order listed under "Stages" in DESIGN.md. Don't scaffold later stages ahead of time. Confirm verification results with the user before moving on to the next stage.

The system deletes originals out of members' accounts, so mistakes are expensive. Several API behaviours (sidecar precedence, cluster-group recognition on new uploads, the mobile re-upload loop) are assumptions until checked on the dev instance. DESIGN.md has the list.

## Looking things up

- The Immich API reference site is a JavaScript app and fetches as an empty page. Use the OpenAPI spec instead: https://raw.githubusercontent.com/immich-app/immich/main/open-api/immich-openapi-specs.json. Each path carries an `x-immich-permission` field naming the API key scope it needs.
- For behaviour the spec doesn't describe (job ordering, sidecar handling, what extraction overwrites), read the server source directly. server/src/services/metadata.service.ts is where sidecar read and write live.
- GitHub discussions and issues referenced in DESIGN.md are the record of what upstream has said about ownership transfer, the re-upload loop and tags. Check them before assuming something has changed.

## Persistent memory

Before writing to persistent memory (e.g. the Claude Code per-project memory store), consider whether the guidance belongs in this file instead. AGENTS.md is checked in, visible to human contributors, and shared across every agent session; per-agent memory is none of those things. If a rule is repo-wide and durable rather than personal to one user or session, recommend adding it to AGENTS.md and wait for approval before either saving the memory or editing this file.

## Commits

Do not add AI agent attribution to commits (no Co-Authored-By lines). The user owns all code in this repository.

Never run git commit or git push without an explicit instruction in the current message to do so. Completing a task does not constitute permission to commit — each commit requires a fresh explicit request. If changes are ready, say so and stop.
