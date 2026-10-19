---
name: sync-fork
description: Use when asked to sync this omnigent fork/clone with upstream, pull in upstream changes, update main and the patched branch, or "get up to date with upstream". Fast-forwards the main mirror from omnigent-ai/omnigent and rebases the local patch branch onto it, auto-stashing any work-in-progress.
---

# Sync the omnigent fork with upstream

This clone tracks two remotes and keeps two branches:

| Branch | Tracks | Role |
|--------|--------|------|
| `main` | `upstream/main` (omnigent-ai/omnigent) | pristine mirror, fast-forward only |
| `mine` | `origin/mine` (your fork) | `main` + your cherry-picked patches, rebased onto fresh upstream each sync |

`upstream` = `omnigent-ai/omnigent`, `origin` = your fork (`kzarzycki/omnigent`).

## How to sync

Run the bundled script:

```bash
.claude/skills/sync-fork/sync-fork.sh [TARGET]
```

`TARGET` defaults to the newest upstream mainline commit at least 3 days old;
`audit-sync` passes the exact commit it audited.

It performs, in order:
1. Auto-stash the working tree if dirty (untracked included).
2. `git fetch upstream`, `git fetch origin mine`.
3. Fast-forward `main` to `upstream/main`, push to `origin`.
4. Cherry-pick commits on `origin/mine` that have no patch-equivalent in the
   local `mine`, after checking each is signed by a key in
   `gpg.ssh.allowedSignersFile` (it stops on any that isn't). `origin/mine` is the source of truth: a fix landed there from
   any machine survives the force-push below.
5. Rebase `mine` onto `TARGET` (skipped when `mine` already contains it), force-push (`--force-with-lease`) to `origin`.
6. Return to the starting branch and restore the stash.

It does not deploy. Every machine runs `omnigent-update` (dotagents,
`patches/omnigent/update.sh`) from an Omnigent scheduled task, which installs
the new `origin/mine` head and restarts the host or server when idle.

## When the rebase conflicts

Resolve conflicts as part of the sync job. For each conflict, inspect the fork
commit's purpose and upstream's change. Prefer upstream and drop the fork
change when upstream provides the same result differently. Keep or combine
fork code only when upstream provides less of the intended behaviour; record
one line per conflict with the file, choice, and reason. Preserve every sandbox
and authentication check from either side.

The script stops mid-rebase and its EXIT trap cannot restore the starting
branch or pop the stash until conflicts are resolved. Continue manually:

```bash
# Resolve and stage conflicts, then repeat as needed:
git cherry-pick --continue     # if taking origin/mine commits stopped
git rebase --continue          # or git rebase --skip for a superseded commit
```

Run focused tests for resolved files before pushing. Finish the interrupted
script's remaining steps manually: push `mine` with `--force-with-lease`, return
to the original starting branch, and restore its autostash. Do not rerun before
pushing: conflict resolutions change patch equivalence, so the script can try
to import the old remote commits again. Verify the branch and working files. Report
conflict decisions, test commands and exits, script exit, push result, and
`mine` before and after. Do not treat a zero script exit as proof of a push:
the script deliberately reports push failures without failing the run.

## Notes

- Commits are signed (`commit.gpgsign`, SSH format, set in this checkout's
  git config), so the rebased `mine` head carries this machine's signature.
  `omnigent-update` on every machine refuses to install a head that isn't
  signed by a key in its local allowed-signers file.

- This skill must be committed on `mine` to survive the rebase that the sync itself performs. If it lives only as an uncommitted working-tree file, it gets stashed/restored each run instead of being part of the replayed patch set.
- The script assumes the branch/remote names in the table above. Renaming any of them means editing the variables at the top of `sync-fork.sh`.
