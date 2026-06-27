#!/usr/bin/env bash
# Sync this fork with upstream and replay the local patch set.
#
#   main  = pristine mirror of upstream/main (fast-forward only)
#   mine  = main + cherry-picked PRs; rebased onto fresh upstream each run
#
# A dirty working tree is auto-stashed before the sync and restored after,
# so in-progress edits survive. Runs against the repo this script lives in,
# regardless of the current directory.
#
# Usage: .claude/skills/sync-fork/sync-fork.sh
set -euo pipefail

UPSTREAM=upstream      # remote -> omnigent-ai/omnigent
FORK=origin            # remote -> your fork
MIRROR=main            # pristine upstream mirror branch
PATCHED=mine           # integration branch carrying your cherry-picks

cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"

start_branch=$(git rev-parse --abbrev-ref HEAD)

stashed=0
if [ -n "$(git status --porcelain)" ]; then
  echo "==> stashing dirty working tree"
  git stash push -u -m "sync-fork autostash" >/dev/null
  stashed=1
fi

restore() {
  git checkout -q "$start_branch" 2>/dev/null || true
  if [ "$stashed" = 1 ]; then
    echo "==> restoring stashed working tree"
    git stash pop || echo "!! stash pop conflicted — your WIP is safe in 'git stash list'; resolve manually" >&2
  fi
}
trap restore EXIT

echo "==> fetching $UPSTREAM"
git fetch "$UPSTREAM"

echo "==> fast-forwarding $MIRROR to $UPSTREAM/$MIRROR"
git branch -f "$MIRROR" "$UPSTREAM/$MIRROR"
git push "$FORK" "$MIRROR"

echo "==> rebasing $PATCHED onto $UPSTREAM/$MIRROR"
git checkout -q "$PATCHED"
git rebase "$UPSTREAM/$MIRROR"
git push --force-with-lease "$FORK" "$PATCHED"

echo "==> done. $MIRROR and $PATCHED are up to date."
