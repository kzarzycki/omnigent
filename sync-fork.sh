#!/usr/bin/env bash
# Sync this fork with upstream and replay the local patch set.
#
#   main  = pristine mirror of upstream/main (fast-forward only)
#   mine  = main + cherry-picked PRs; rebased onto fresh upstream each run
#
# Usage: ./sync-fork.sh
set -euo pipefail

UPSTREAM=upstream      # remote pointing at omnigent-ai/omnigent
FORK=origin            # remote pointing at your fork
MIRROR=main            # pristine upstream mirror branch
PATCHED=mine           # integration branch carrying your cherry-picks

if [ -n "$(git status --porcelain)" ]; then
  echo "Working tree is dirty — commit or stash first." >&2
  exit 1
fi

start_branch=$(git rev-parse --abbrev-ref HEAD)
restore() { git checkout -q "$start_branch"; }
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
