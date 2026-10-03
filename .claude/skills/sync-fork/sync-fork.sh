#!/usr/bin/env bash
# Sync this fork with upstream and replay the patch set.
#
#   main  = pristine mirror of upstream/main (fast-forward only)
#   mine  = main + cherry-picked PRs; rebased onto fresh upstream each run
#
# `origin/mine` is the source of truth: commits landed there from any machine
# are taken into the local `mine` before the rebase, so the force-push cannot
# drop them. Deploying the result is not done here: each machine runs
# omnigent-update (dotagents) from an Omnigent scheduled task.
#
# A dirty working tree is auto-stashed before the sync and restored after, so
# in-progress edits survive. Runs against the repo this script lives in,
# regardless of the current directory.
#
# `mine` is rebased onto TARGET, by default the newest upstream mainline commit
# at least 3 days old: a compromised upstream change usually surfaces within
# days, so a short lag keeps most of them off every machine. audit-sync passes
# the exact commit it audited.
#
# Commits landed in origin/mine are only taken when signed by a key in
# gpg.ssh.allowedSignersFile: this repo signs everything it commits, so taking
# an unsigned one would re-sign whatever a stolen push token put there.
#
# Usage: .claude/skills/sync-fork/sync-fork.sh [TARGET]
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

# push is non-fatal: the rebased branch is still correct locally, and the next
# run pushes it (origin/mine is taken in first, so nothing is lost meanwhile).
push() {
  git push "$@" || echo "!! push failed ($*) — sync applied locally, fork remote not updated" >&2
}

echo "==> fetching $UPSTREAM and $FORK"
git fetch "$UPSTREAM"
git fetch "$FORK" "$PATCHED"

TARGET=$(git rev-parse --verify "${1:-$(git rev-list -1 --first-parent --before='3 days ago' "$UPSTREAM/$MIRROR")}^{commit}")

echo "==> fast-forwarding $MIRROR to $UPSTREAM/$MIRROR"
git branch -f "$MIRROR" "$UPSTREAM/$MIRROR"
push "$FORK" "$MIRROR"

git checkout -q "$PATCHED"
# Commits on origin/mine with no patch-equivalent here were landed from another
# machine. Take them before rebasing, or the force-push below deletes them.
landed=$(git cherry "$PATCHED" "$FORK/$PATCHED" | sed -n 's/^+ //p')
if [ -n "$landed" ]; then
  for c in $landed; do
    git verify-commit "$c" 2>/dev/null \
      || { echo "!! $FORK/$PATCHED has unsigned or unknown-signer commit $c — not taking it" >&2; exit 1; }
  done
  echo "==> taking $(echo "$landed" | wc -l | tr -d ' ') commit(s) landed in $FORK/$PATCHED"
  git cherry-pick $landed
fi

if git merge-base --is-ancestor "$TARGET" "$PATCHED"; then
  echo "==> $PATCHED already contains ${TARGET:0:9}; nothing to rebase"
else
  echo "==> rebasing $PATCHED onto ${TARGET:0:9}"
  git rebase "$TARGET"
fi
push --force-with-lease="$PATCHED:$FORK/$PATCHED" "$FORK" "$PATCHED"

echo "==> done. $PATCHED on ${TARGET:0:9}, pushed."
