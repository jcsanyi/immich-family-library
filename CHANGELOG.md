# Changelog

What changed in each release of the service. Before you upgrade, read every
section newer than the version you run. Any upgrade notes in those sections
are steps you must take.

## Unreleased

- A config with mistakes now reports all of them at once, one per line, and
  each line names the setting, for example `service.readonly: Input should be
  a valid boolean`.
- An empty `[webhook]` section is now an error. It used to mean no webhook.

### Upgrade notes

- If your config has an empty `[webhook]` section, remove it. Otherwise the
  service does not start.

## v0.1.1 - October 16, 2026

- Added the `ifl example-config` command for printing the annotated example
  config to help bootstrap a new installation.

## v0.1.0 - October 15, 2026

The first release. It isn't ready for use on real photos yet.
