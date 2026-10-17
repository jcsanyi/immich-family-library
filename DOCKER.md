# Immich Family Library

> **Not yet stable.** This project is still in development. Don't use it on photos you care about: it has the potential to delete your photos, and it has only run against a test instance so far. To hear when the first stable release is out, [watch the repository's releases](https://github.com/jcsanyi/immich-family-library) on GitHub.

A small service that runs next to a self-hosted [Immich](https://immich.app) instance. It gives a family a shared photo library while each member keeps their own account and decides what to contribute. When a member shares an album with the family, or drops photos into a family album, the service moves those photos into a dedicated family account, which shares its library back with every member.

The [README on GitHub](https://github.com/jcsanyi/immich-family-library) explains how it works and what it carries over.

## Quick start

The service is one more container in the Immich compose stack. These are the short steps - see [docs/deployment.md](https://github.com/jcsanyi/immich-family-library/blob/main/docs/deployment.md) for the details and alternatives.

1. Create the family user in Immich. Get an API key from the family user and from each member, with the permissions in [docs/configuration.md](https://github.com/jcsanyi/immich-family-library/blob/main/docs/configuration.md).
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

Before you upgrade, read [CHANGELOG.md](https://github.com/jcsanyi/immich-family-library/blob/main/CHANGELOG.md).

## Tags

- `X.Y.Z`: one release, for example `0.1.0`.
- `X.Y`: the newest patch release of `X.Y`, for example `0.1`. The setup above uses this tag.
- `latest`: the newest release.

A pre-release, for example `0.2.0-rc.1`, gets only its own tag. `latest` and `X.Y` never point to a pre-release.
