# Notes for agents

## Orientation

Before you do anything, read `README.md` and `docs/how-it-works.md` in full to
ensure you understand what the system does and how it works. Before you make
changes, read `docs/development.md` in full. It says how the code is laid out,
how to test, and which writing style each doc follows.

When any part of the design changes, update the relevant doc at the same
time, so the docs never disagree with the code. If the code and the docs
already disagree, and there is no record of a decision, ask. Do not assume
that either one is right.

## How to work on this

USE CAUTION: The system deletes originals out of members' accounts. Mistakes
are expensive. Anything about Immich's behaviour that `docs/immich-notes.md`
does not mark as verified is an assumption until you check it on a dev
instance. The `verify` label on the issues tracks what is still unconfirmed.

Testing happens on a separate dev instance, never on production.
`AGENTS.local.md`, which is gitignored, describes the instance this project
uses. If it is missing, ask the user for it before you touch the dev instance.
`docs/development.md` says how to use the instance and its scripts.

## Commits and branches

Do not add AI agent attribution to anything. This applies to commits (no
`Co-Authored-By` lines and no session trailers), pull request titles and
descriptions, issues, comments, and docs. The user owns all code in this
repository.

`main` is reviewed code and accepts changes only through a pull request.
Every change happens on its own feature branch, cut from `main`. Name the
branch for the change, for example `webhook-retries`.

When the user says to commit, do all of this:

1. Commit on the feature branch. Keep commits small and messages plain.
2. Push the feature branch.
3. If the change needs a test on the dev instance, deploy the branch to the
   dev host with the command in `AGENTS.local.md`. A change to docs or scripts
   alone does not need a deploy.

Code reaches the dev host only through git. Do not `rsync` or `scp` files to
it.

Keep instance-specific values (hostnames, emails, network names) out of
tracked files. This is a public project. Examples use example.com.

Once a change has been completed and tested, the user will request that you
create a PR for merging the changes to `main`. "Issues and pull requests"
below says what the description holds. For a squash-merged PR, the
title will become the commit message, so keep it short and assume it will be
displayed alongside a link to the PR details themselves. Occasionally, the
user may ask to set up the PR for a rebase merge instead. In that case,
propose a logical order for the final commits, and offer to rebase the branch
to set it up.

## Issues and pull requests

Keep them short. A reader must be able to learn what to do and why it
matters, and no more.

An issue says what is wrong or missing, why it matters, and what a fix looks
like if that is known. Aim for less than 200 words, unless the issue must
explain something that the reader cannot get from the code. Examples are
behaviour of Immich that is not confirmed, and a failure that makes sense
only when its steps are given. Do not explain how the code works now: the
reader will read the code. Do not list the steps of the work or a test plan,
unless a step is one that a person would get wrong.

A pull request description says why the change was made and what a person
will see that is different. Aim for less than 400 words. Do not retell how
the branch got to its final form. That is in the commits. Detail that a later
reader needs belongs in `docs/`, and the description points to it.

A comment that records a decision can be as long as the decision needs.

Use a heading only when the text has parts that a reader would look for
separately.
