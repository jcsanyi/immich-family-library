# Notes for agents

## Orientation

Read `README.md` first. It says what the system does, in user-facing terms, and
defines the vocabulary: member, family account, dropbox, reclaim.

`docs/` is the design and the documentation in one. Read `docs/how-it-works.md`
in full before you do anything. It covers the flows, the state model, the
metadata that travels with an asset, the mobile re-upload problem and how we
handle it, and pacing. The other docs:

- `docs/configuration.md`: the config reference and the API key permissions.
- `docs/immich-notes.md`: what we have verified about Immich, where to look
  things up, and the upstream references.
- `docs/development.md`: the tooling, the target Immich version, the tests,
  the dev instance and its scripts, the branch model, and the writing style.
- `docs/cli.md` and `docs/deployment.md`: the operator side.
- `docs/for-members.md`: the hand-out for family members.

Work still to do lives in the GitHub issues, not in a document.

Any part of the design can change through conversation with the user. When it
does, update the relevant doc in the same change, so that the docs never
disagree with the decision. If the code and the docs disagree, and there is no
record of a decision, ask. Do not assume that either one is right.

When you write or change a doc, follow the style that "Writing style" in
`docs/development.md` assigns to it, and invoke the matching skill from the
plain-english plugin if it is installed.

Two terms that the docs use and `README.md` does not:

- The one rule: the service moves a member-owned asset found in a family-owned
  album to the family account. Every flow is a way to get an asset into that
  position.
- Ledger: the local SQLite record, keyed by checksum, of what moved, from whom,
  and where it went. It is a record, not a controller. The move path derives
  its state from Immich.

## How to work on this

Work on one GitHub issue, or one piece of one, at a time. Do not scaffold
ahead. Confirm verification results with the user before you move on.

CAUTION: The system deletes originals out of members' accounts. Mistakes are
expensive. Anything about Immich's behaviour that `docs/immich-notes.md` does
not mark as verified is an assumption until you check it on the dev instance.
The `verify` label on the issues tracks what is still open.

Testing happens on a separate dev instance, never on production.
`AGENTS.local.md`, which is gitignored, describes the instance this project
uses. If it is missing, ask the user for it before you touch the dev instance.
`docs/development.md` says how to use the instance and its scripts.

## Persistent memory

Before you write to persistent memory, ask whether the guidance belongs in
this file instead. The Claude Code per-project memory store is one example.
`AGENTS.md` is checked in, visible to human contributors, and shared across
every agent session. Per-agent memory is none of those things. If a rule is
repo-wide and durable, rather than personal to one user or session, recommend
adding it to `AGENTS.md`. Wait for approval before you save the memory or edit
this file.

## Commits and branches

Do not add AI agent attribution to commits (no `Co-Authored-By` lines). The
user owns all code in this repository.

`main` is reviewed code. Every change happens on its own feature branch, cut
from `main`. Name the branch for the change, for example `webhook-retries`.
The old `dev` branch is retired. Do not commit to it.

Never commit without an explicit instruction in the current message.
Completing a task is not that instruction. The user approving a plan is not
that instruction either. When the changes are ready, say so and stop.

When the user says to commit, do all of this:

1. Commit on the feature branch. Keep commits small and messages plain.
2. Push the feature branch.
3. If the change needs a test on the dev instance, deploy the branch to the
   dev host with the command in `AGENTS.local.md`. It checks out the branch,
   pulls it, rebuilds the container, and restarts the daemon. A change to
   docs or scripts alone does not need a deploy.

Code reaches the dev host only through git. Do not `rsync` or `scp` files to
it. The checkout there uses a read-only deploy key, so it can pull but never
push. Its config and ledger are untracked and survive pulls.

Keep instance-specific values (hostnames, emails, network names) out of
tracked files. This is a public project. Examples use example.com.

Never merge, rebase, or push to `main` without an explicit instruction in the
current message. Merging a feature branch is the user's call, after they have
reviewed it and the dev instance has run it. When told to merge, squash the
branch into one commit on `main`. If the user wants the branch history kept,
first rebase the branch so that its commits tell a clear story. Then merge it
without a squash.
