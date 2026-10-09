# Notes for agents

## What this is

A service that runs beside a self-hosted Immich instance and moves photos from individual family members' accounts into one shared family account, driven by what members choose to share. Immich has no ownership transfer, so "move" means re-upload under the family account with all metadata carried over, then trash the original.

DESIGN.md is the source of truth. Read it fully before doing anything. It covers the flows, the state model, what metadata is carried, the mobile re-upload problem and how we handle it, the API key permissions, the things that still need verifying on a dev instance, and the build stages.

## Vocabulary

- Member: a family member with their own Immich user and an API key handed to the system.
- Family account: the extra Immich user that owns everything shared. Partner-shares to every member so family photos appear in their timelines.
- The one rule: a member-owned asset found in a family-owned album gets moved to the family account. Every flow is a way of getting an asset into that position.
- Album conversion: a member shares an album with the family user; the system recreates it as a family-owned album, re-adds the originals, deletes the member's album, and the one rule does the rest.
- Dropbox: a family-owned album flagged in config. Photos added to it get moved but the family copy isn't re-added to it. For photos that belong in the timeline and no particular album.
- Reclaim: a per-member private album; adding a family-owned asset there moves it back to the original owner. Later stage.
- Ledger: the local state DB keyed by checksum recording what moved, from whom, and where it went. Written before anything is deleted.
- Re-upload loop: the mobile app re-uploads anything whose hash is no longer on the server, so trashed originals come back after the trash purges. We trash them again from the ledger and ask members to turn on the app's experimental "sync remote deletions" setting.

## Instance state

As of October 2026 the Immich instance is on 3.0.x with an upgrade to 3.3 planned shortly. Develop against 3.3: cluster groups came in 3.2, people sharing in 3.3. Don't propose workarounds for older versions.

Face detection is currently off on the instance. It gets enabled after all accounts are in a cluster group, so no facial recognition reset is needed. Don't rely on faces or people sharing existing yet.

Testing happens on a separate dev instance, never production.

## How to work on this

Build and test one stage at a time, in the order listed under "Stages" in DESIGN.md. Don't scaffold later stages ahead of time. Confirm verification results with me before moving on to the next stage.

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


