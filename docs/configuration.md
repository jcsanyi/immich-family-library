# Configuration

One TOML file. `config.example.toml` in the repo is the annotated template;
copy it, fill in the keys, keep it out of version control (it's gitignored).
`ifl show-config` prints what the service actually loaded, keys redacted.

Relative paths are resolved against the config file's directory.

## `[immich]`

| Setting | Default | What it is |
|---|---|---|
| `url` | required | Where the service reaches Immich. In the compose stack, the server's container name works: `http://immich_server:2283`. |
| `admin_api_key` | none | Optional. Any admin user's key with `queue.read` (plus `user.read` and `apiKey.read`). With it, no move starts while the sidecar or metadata queues have a backlog. |

## `[service]`

| Setting | Default | What it is |
|---|---|---|
| `readonly` | `true` | While true, every command that writes to Immich refuses to run. Flip it once `observe` reports what you expect. |
| `ledger_path` | `data/ledger.sqlite` | The SQLite ledger. In the container the config mount is read-only, so use the data volume: `/data/ledger.sqlite`. |
| `settle_seconds` | `60` | A photo changed more recently than this isn't moved yet; its jobs may still be running. Re-uploads don't wait for it. |
| `interval_seconds` | `600` | The daemon's pause between passes. |

## `[limits]`

| Setting | Default | What it is |
|---|---|---|
| `albums_per_pass` | `5` | The most albums `convert-album --all` or a pass converts. 0 for no limit. |
| `assets_per_pass` | `20` | The most photos `move --all` or a pass moves. 0 for no limit. |

Commands given a specific id ignore the limits. The limits and the interval
together are the rate; see pacing in [how-it-works.md](how-it-works.md).

## `[tags]`

| Setting | Default | What it is |
|---|---|---|
| `moved` | `"family"` | Goes on a member's original before it's trashed. Adding it is also what makes Immich write the sidecar the move carries. |
| `from_prefix` | `"from"` | The family copy is tagged `<from_prefix>/<member>`, using the member's section name. |

Tag names, no leading or trailing slash.

## `[warnings]`

| Setting | Default | What it is |
|---|---|---|
| `reuploads_per_pass` | `10` | Above this many re-uploads for one member in one pass, the member is named in a warning. |

## `[family]`

| Setting | Default | What it is |
|---|---|---|
| `email` | required | Checked against the account the key belongs to. |
| `api_key` | required | The family account's key. |
| `dropbox_album` | `"Family Dropbox"` | Name of the family-owned album whose photos are moved but not re-added to it. |

## `[members.<name>]`

One section per participating member. The section name is the label used in
logs and in the `from/<name>` tag; Immich identity comes from the key, checked
against `email`. Emails must be unique across the family and members.

| Setting | Default | What it is |
|---|---|---|
| `email` | required | Checked against the key's owner. |
| `api_key` | required | The member's key. |

Members without a section can still use Immich and share albums with the
family user; those albums are reported, not converted.

## `[webhook]`

Optional. With the section present, the daemon runs the receiver and
`reconcile-albums` gives every member an Immich workflow posting their uploads
to it.

| Setting | Default | What it is |
|---|---|---|
| `url` | required | What Immich calls. On the shared docker network, the container name: `http://ifl:8080/webhook/asset-created`. |
| `token` | required | A secret you make up. Immich sends it in the `X-IFL-Token` header; the receiver rejects anything else. |
| `listen` | `"0.0.0.0:8080"` | The receiver's bind address. |
| `path` | `"/webhook/asset-created"` | The receiver's path. Must match the path in `url`. |

## API key permissions

Immich's permission names, as the API key editor shows them. The startup
check requires every one listed; a key with `all` passes.

**Member keys**

`user.read`, `apiKey.read`, `album.read`, `album.delete`, `albumAsset.create`,
`asset.read`, `asset.download`, `asset.delete`, `asset.edit.get`,
`assetFile.read`, `assetFile.download`, `tag.create`, `tag.asset`,
`sharedLink.read`, `partner.read`, `workflow.read`, `workflow.create`,
`workflow.update`.

**Family key**

The member set less the tag, shared-link and workflow permissions, plus
`album.create`, `albumUser.create`, `albumUser.update`, `partner.create`,
`asset.upload`, `asset.update`, `asset.edit.create`.

**Admin key** (optional)

`queue.read`, `user.read`, `apiKey.read`. Any admin user's key, the family
account or a member included; the check is that the owner is an admin.

Which keys an operation uses:

| Operation | Keys |
|---|---|
| Convert an album | the owner's, and every contributor's |
| Move a photo | the owner's to read and trash, the family's to upload and fix up, album owners' to re-link into their albums |
| Re-upload | the owner's to trash, the family's to look up and re-link |
| Reconcile | the family's; each member's for their workflow |
| Queue check | the admin's |

A 403 on any operation is treated like a missing key: the item is blocked,
logged, and tried again next pass.
