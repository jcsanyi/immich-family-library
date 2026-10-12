# Configuration

The config is one TOML file. `config.example.toml` in the repo is the
annotated template. Copy it, fill in the keys, and keep it out of version
control (it is gitignored). `ifl show-config` prints what the service loaded,
with the keys redacted.

Relative paths are resolved against the config file's directory.

The sections and settings below are the only ones the file can hold. A
section or setting with any other name is an error, and the service does not
start. This catches a typo or an old name, which would otherwise take the
default without a message.

## `[immich]`

| Setting | Default | What it is |
|---|---|---|
| `url` | required | Where the service reaches Immich. In the compose stack, the server's container name works: `http://immich_server:2283`. |
| `admin_api_key` | none | Optional. Any admin user's key with `queue.read`, plus `user.read` and `apiKey.read`. With it, no move starts while the sidecar or metadata queues have a backlog. |

## `[service]`

| Setting | Default | What it is |
|---|---|---|
| `readonly` | `true` | While true, every command that writes to Immich refuses to run. Set it to false once `observe` reports what you expect. |
| `state_path` | `data/state.json` | The state file: the re-upload marks and the daily tally. Nothing in it needs a backup. In the container the config mount is read-only, so use the data volume: `/data/state.json`. |
| `settle_seconds` | `60` | A photo changed more recently than this is not moved yet, because its jobs can still be running. Re-uploads do not wait for it. |
| `interval_seconds` | `600` | The daemon's pause between passes. |

## `[limits]`

| Setting | Default | What it is |
|---|---|---|
| `albums_per_pass` | `5` | The most albums that `convert-album --all` or a pass converts. 0 means no limit. |
| `assets_per_pass` | `20` | The most photos that `move --all` or a pass moves. 0 means no limit. |

A command given a specific id ignores the limits. The limits and the interval
together are the rate. See pacing in [how-it-works.md](how-it-works.md).

## `[tags]`

| Setting | Default | What it is |
|---|---|---|
| `moved` | `"family"` | Goes on a member's original before it is trashed. Adding it is also what makes Immich write the sidecar that the move carries. |
| `from_prefix` | `"from"` | The family copy is tagged `<from_prefix>/<member>`, with the member's section name. |

Both are tag names, with no leading or trailing slash.

The `moved` tag is also a marker. When a member's photo has the same bytes as
a family photo, the service reads the tag to tell a move that stopped partway
from a re-upload. Choose a name that members do not use for their own tags,
and that does not occur as a keyword in their files.

## `[warnings]`

| Setting | Default | What it is |
|---|---|---|
| `report_at` | `"22:00"` | The local time of day for the daily re-upload report. One warning per member whose re-uploads were trashed in the last day. The compose file gives the container the host's timezone. |

## `[family]`

| Setting | Default | What it is |
|---|---|---|
| `email` | required | Checked against the account that the key belongs to. |
| `api_key` | required | The family account's key. |
| `dropbox_album` | `"Family Dropbox"` | The family-owned album whose photos are moved but not re-added to it. |

## `[members.<name>]`

One section per participating member. The section name is the label used in
logs and in the `from/<name>` tag. Immich identity comes from the key, which
is checked against `email`. Emails must be unique across the family and the
members. At startup the service also refuses two sections whose keys belong
to the same Immich user. The admin key is the exception: it can belong to the
family account or to a member.

| Setting | Default | What it is |
|---|---|---|
| `email` | required | Checked against the key's owner. |
| `api_key` | required | The member's key. |

A member without a section can still use Immich and share albums with the
family user. The service reports those albums and does not convert them.

## `[webhook]`

Optional. When the section is present, the daemon runs the receiver, and
`reconcile-albums` gives every member an Immich workflow that posts their
uploads to it.

| Setting | Default | What it is |
|---|---|---|
| `url` | required | What Immich calls. On the shared docker network, the container name works: `http://ifl:8080/webhook/asset-created`. |
| `token` | required | A secret you make up. Immich sends it in the `X-IFL-Token` header. The receiver rejects anything else. |
| `listen` | `"0.0.0.0:8080"` | The receiver's bind address. |
| `path` | `"/webhook/asset-created"` | The receiver's path. It must match the path in `url`. |

## API key permissions

These are Immich's permission names, as the API key editor shows them. The
startup check requires every one listed. A key with `all` passes.

**Member keys**

`user.read`, `apiKey.read`, `album.read`, `album.delete`, `albumAsset.create`,
`asset.read`, `asset.download`, `asset.delete`, `asset.edit.get`,
`assetFile.read`, `assetFile.download`, `tag.create`, `tag.asset`,
`sharedLink.read`, `partner.read`, `workflow.read`, `workflow.create`,
`workflow.update`.

**Family key**

The member set without the tag, shared-link and workflow permissions, plus
`album.create`, `albumUser.create`, `albumUser.update`, `partner.create`,
`asset.upload`, `asset.update`, `asset.edit.create`.

**Admin key** (optional)

`queue.read`, `user.read`, `apiKey.read`. Any admin user's key works, the
family account or a member included. The check is that the owner is an admin.

Which keys each operation uses:

| Operation | Keys |
|---|---|
| Convert an album | the owner's, and every contributor's |
| Move a photo | the owner's to read and trash, the family's to upload and fix up, album owners' to re-link into their albums |
| Re-upload | the owner's to trash, the family's to look up and re-link |
| Reconcile | the family's, and each member's for their workflow |
| Queue check | the admin's |

The service treats a 403 on any operation like a missing key. The item is
blocked, logged, and tried again next pass.
