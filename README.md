# Immich Family Library

A small service that runs next to a self-hosted [Immich](https://immich.app) instance. It gives a family a shared photo library while each member keeps their own account and decides what to contribute.

Members upload to their own accounts as usual. When a member shares an album with the family, or drops photos into a family album, the service moves those photos into a dedicated family account. The family account shares its library back with every member through partner sharing.

Immich cannot transfer ownership of a photo. So "move" means the service re-uploads the photo under the family account with all of its metadata, then trashes the original. The original sits in the member's trash for 30 days by default. Nothing is ever deleted outright.

## How it works

There is one extra Immich user, the family account, and it owns the family library. The service watches for one thing: a member-owned photo sitting in an album the family account owns. When it finds one, it moves it. Sharing an album, adding to a family album, and adding to the family dropbox are all ways of getting a photo into that position.

[docs/how-it-works.md](docs/how-it-works.md) explains each flow, what metadata travels with a photo, and how the service handles the phone's re-uploads. [docs/for-members.md](docs/for-members.md) explains the same thing for the non-technical family members who will use the system.

## Requirements

- Immich 3.3 or later.
- A family Immich user with an API key, and an API key from each participating member.
- If face detection will be on, all accounts in one cluster group, created before face detection is turned on. [docs/deployment.md](docs/deployment.md) has the order of steps.

## Running it

The service is one container next to Immich, on the same docker network, with a volume for its state file. `compose.yml` is the deployment and `config.example.toml` is the annotated configuration. [docs/deployment.md](docs/deployment.md) has the steps.

## Documentation

- [docs/for-members.md](docs/for-members.md): the page to hand to family members. What they see, the one phone setting, what carries over.
- [docs/how-it-works.md](docs/how-it-works.md): the model. The one rule, the flows, the move step by step, re-uploads, state and pacing.
- [docs/cli.md](docs/cli.md): every command, the exit codes, a first run.
- [docs/configuration.md](docs/configuration.md): every setting with its default, and the API key permissions.
- [docs/deployment.md](docs/deployment.md): compose, the webhook, the state file, upgrades, what to check before production.
- [docs/immich-notes.md](docs/immich-notes.md): what the service relies on in Immich 3.3.1, and how each point was confirmed.
- [docs/development.md](docs/development.md): tests, the fake Immich, the dev scripts, branches.

## Known gaps

The [issues](https://github.com/jcsanyi/immich-family-library/issues) track what is left before a first release and the improvements after it. The gaps a member will notice:

- Reclaim, moving a photo back to its original owner, is planned and not built (issue 3).
- Live photos are not moved. The move blocks them and reports them (issue 14).
- Stacks and manual face corrections do not carry over to the family copy (issues 5 and 4).
- Whether a new upload's faces cluster with people found across accounts is not verified (issue 1).

## Alternatives

Nothing else does this. The closest projects:

- donnchawp/immich-shared-library copies assets and ML data between accounts by writing to Postgres directly. It copies rather than moves, and is pinned to Immich versions.
- Trust1509/immich-family-tools matches people across accounts and builds shared albums. It never moves assets.
- alangrainger/immich-person-to-album and ajb3932/immich-partner-sharing populate albums by face.

Upstream's roadmap lists "better sharing" and "user groups" with no dates. A maintainer-adjacent comment suggests that assets will one day be library-owned with aliases. If that lands, this project becomes unnecessary. Not soon.
