# Immich Family Library

> **Not yet stable.** This project is still in development. Don't use it on photos you care about: it has the potential to delete your photos, and it has only run against a test instance so far. To hear when the first stable release is out, [watch the repository's releases](https://github.com/jcsanyi/immich-family-library) on GitHub.

A small service that runs next to a self-hosted [Immich](https://immich.app) instance. It gives a family a shared photo library while each member keeps their own account and decides what to contribute.

Members upload to their own accounts as usual. When a member shares an album with the family, or drops photos into a family album, the service moves those photos into a dedicated family account. The family account shares its library back with every member through partner sharing.

Immich cannot transfer ownership of a photo. So "move" means the service re-uploads the photo under the family account with all of its metadata, then trashes the original. The original sits in the member's trash for 30 days by default. Nothing is ever deleted outright.

> **Not for external libraries.** The service doesn't support moving a photo that lives in an Immich external library, and never moves one. A member can still share such a photo: in a family album, or in an album they share with the family, it stays in the album for everyone to see, and it stays the member's. The dropbox doesn't work for it. [docs/how-it-works.md](docs/how-it-works.md#external-libraries) has the detail.

## How it works

There is one extra Immich user, the family account, and it owns the family library. The service watches for one thing: a member-owned photo sitting in an album the family account owns. When it finds one, it moves it. Sharing an album, adding to a family album, and adding to the family dropbox are all ways of getting a photo into that position.

[docs/how-it-works.md](docs/how-it-works.md) explains each flow, what metadata travels with a photo, and how the service handles the phone's re-uploads. [docs/for-members.md](docs/for-members.md) explains the same thing for the non-technical family members who will use the system.

## Requirements

- Immich 3.3. A later Immich version needs a matching release of this service. Until then the service starts with a warning.
- A family Immich user with an API key, and an API key from each participating member.
- If face detection will be on, all accounts in one cluster group, created before face detection is turned on. [docs/deployment.md](docs/deployment.md) has the order of steps.

## Running it

The service is one more container in the Immich compose stack. These are the short steps - see [docs/deployment.md](docs/deployment.md) for the details and alternatives.

1. Create the family user in Immich. Get an API key from the family user and from each member, with the permissions in [docs/configuration.md](docs/configuration.md).
2. In the directory of Immich's `docker-compose.yml`, make a subdirectory `ifl`.
3. In the same `docker-compose.yml` directory, add the service to `docker-compose.override.yml`. If the file does not exist, create it:

   ```yaml
   services:
     ifl:
       image: jcsanyi/immich-family-library:0.1
       restart: always
       depends_on:
         - immich-server
       volumes:
         - ./ifl:/config:ro
         - ifl-data:/data
         - /etc/localtime:/etc/localtime:ro

   volumes:
     ifl-data:
   ```

4. Get the annotated example config: `docker compose run --rm -T ifl example-config > ifl/config.toml`.
5. Fill in the keys from step 1. Change any other setting that your setup needs, and keep `readonly = true`.
6. Run `docker compose run --rm ifl observe`. It reports what the service will do, and changes nothing.
7. When the report is what you expect, set `readonly = false` in `ifl/config.toml`.
8. Run `docker compose run --rm ifl reconcile-albums`. Then run `docker compose up -d`.

Before you upgrade, read [CHANGELOG.md](CHANGELOG.md).

## Documentation

- [docs/for-members.md](docs/for-members.md): the page to hand to family members. What they see, the one phone setting, what carries over.
- [docs/how-it-works.md](docs/how-it-works.md): the model. What the service moves, the flows, the move step by step, re-uploads, state and pacing.
- [docs/cli.md](docs/cli.md): every command, the exit codes, a first run.
- [docs/configuration.md](docs/configuration.md): every setting with its default, and the API key permissions.
- [docs/deployment.md](docs/deployment.md): the two ways to run the container, the config, the first run, the webhook, the run lock, upgrades, what to check before production.
- [docs/immich-notes.md](docs/immich-notes.md): what the service relies on in Immich 3.3.1, and how each point was confirmed.
- [docs/development.md](docs/development.md): tests, the fake Immich, the dev scripts, branches.
- [CHANGELOG.md](CHANGELOG.md): what changed in each release, and the steps an upgrade needs.

## Known gaps

The [issues](https://github.com/jcsanyi/immich-family-library/issues) track what is left before a first release and the improvements after it. The gaps a member will notice:

- The service never moves a photo that is in an external library. A member can share one through an album, and not through the dropbox.
- Reclaim, moving a photo back to its original owner, is planned and not built (issue 3).
- Live photos are not moved. The service blocks them and reports them (issue 14).
- Stacks and manual face corrections do not carry over to the family copy (issues 5 and 4).

## Alternatives

Nothing else does this. The closest projects:

- donnchawp/immich-shared-library copies assets and ML data between accounts by writing to Postgres directly. It copies rather than moves, and is pinned to Immich versions.
- Trust1509/immich-family-tools matches people across accounts and builds shared albums. It never moves assets.
- alangrainger/immich-person-to-album and ajb3932/immich-partner-sharing populate albums by face.

Upstream's roadmap lists "better sharing" and "user groups" with no dates. A maintainer-adjacent comment suggests that assets will one day be library-owned with aliases. If that lands, this project becomes unnecessary. Not soon.
