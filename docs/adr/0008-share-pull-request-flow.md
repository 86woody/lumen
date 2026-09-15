# 0008: The share pull-request flow, and what still needs a forge

Accepted 2026-09-13. Closes the buildable half of audit item 6. Additional spend USD 0.

## Built

`lumen share` action `branch` takes a reviewed bundle (owner approval with source
proof, exactly as `stage` and `import` require) and writes its events to
`.lumen/shared/<repo-id>/` or `_monorepo/` on git branches:

- one branch per repo batch, `lumen/share/<folder>/<digest12>` where the digest
  covers the batch's event digests, so the same batch always names the same branch
  and an existing branch is reused, never rewritten;
- batches capped by `share.batch_limit` in `.lumen/config.toml` (ADR 0007; 100 at
  most, lower by policy);
- each branch is created from the checkout's `HEAD` through a temporary worktree
  under `~/.lumen/worktrees/`, so the developer's working tree, index and current
  branch never change and `git status` stays clean; the worktree is removed
  afterwards;
- the commit lists the bundle digest and event ids; the author is the checkout's
  configured identity or a fixed local one; hooks are skipped and every network
  protocol is disabled for the git calls;
- a share record `~/.lumen/shares/<bundle-digest>.json` carries the branches, base
  and `pull_request.status: pending`; `lumen pending` lists it, and `audit --ci`
  counts branches awaiting a pull request.

Nothing is pushed and no pull request is opened. `audit --ci` exits 0 only when
`share.pull_request = "pending"` is committed in `config.toml`: the team then states
that no forge review authority exists yet, the check reports `pending` with
`configured: true` and is accepted, and the response names it under
`accepted_pending`. With the key unconfigured the check stays pending and the
command exits 1 as before; with `"forge"` it fails, because no forge client exists.

## What still needs a forge

The development forge is a self-hosted Gitea 1.26. Closing item 6 needs:

1. **Push and pull request.** Push each share branch and open a pull request
   against the default branch through the Gitea API, batched per repo. The token
   comes from the OS credential store or an environment variable the owner sets
   for the call; it is never written to a file, a URL or a log.
2. **Owner review on the forge.** Branch protection on `.lumen/shared/**` requiring
   CODEOWNERS approval (`lumen codeowners write` already generates the routing),
   so a merge implies review by the folder's owners.
3. **Authority evidence for `audit --ci`.** After a merge, the check reads the pull
   request number, the merge commit, the approving reviewers and the CODEOWNERS
   file at that commit, verifies that the merged files are exactly the branch's
   event files (byte-identical, ids in the reviewed bundle) and that every
   approver is an owner of the folder. That record replaces the local approval
   receipt as the destination authority for recall admission. Until it exists,
   `repository_review_authority` cannot pass; it can only be configured pending.
4. **Review-fatigue metrics.** Proposals per week, acceptance, correction and
   rejection rates and median review time come from pull-request timestamps; the
   pilot report needs them (plan §4 Sharing).

None of these can be built or tested offline against a hosted review authority,
and the forge is not to be pushed to from this work. The branches are real; the
authority is not.
