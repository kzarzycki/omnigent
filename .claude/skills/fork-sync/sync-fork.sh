#!/usr/bin/env bash
# Sync this fork with upstream through a merge PR; `mine` is never rewritten.
# Merges the audited TARGET (--no-ff) into sync/<12 chars of TARGET> from
# origin/mine, pushes it and opens a PR against mine with `gh api`. SKILL.md has
# the flow, the HOLD rules and conflict handling. Nothing is signed: GitHub is the
# trust anchor.
#
# Usage, from the fork checkout: bash sync-fork.sh TARGET PR_BODY_FILE
# PR_BODY_FILE holds the audit summary and a `VERDICT: PASS` line, else exit 2.
# Environment: SYNC_BASE (PR base branch, default mine; a scratch branch for a
# dry run), SYNC_REPO (owner/name for gh api, default read from origin).
set -euo pipefail

UPSTREAM=upstream      # remote -> omnigent-ai/omnigent
FORK=origin            # remote -> your fork
MIRROR=main            # pristine upstream mirror branch
PATCHED=${SYNC_BASE:-mine}   # branch the sync PR targets

cd "$(git rev-parse --show-toplevel)"

for remote in "$UPSTREAM" "$FORK"; do
  git remote get-url "$remote" >/dev/null 2>&1 \
    || { echo "!! no '$remote' remote in $PWD — run this from the omnigent fork checkout" >&2; exit 2; }
done
{ git rev-parse -q --verify "refs/heads/$PATCHED" || git rev-parse -q --verify "refs/remotes/$FORK/$PATCHED"; } >/dev/null \
  || { echo "!! no '$PATCHED' branch in $PWD — run this from the omnigent fork checkout" >&2; exit 2; }
BODY_FILE=${2:-}
grep -qx 'VERDICT: PASS' "$BODY_FILE" 2>/dev/null \
  || { echo "!! PR_BODY_FILE (second argument) must be a file with the audit summary and a 'VERDICT: PASS' line" >&2; exit 2; }
[ ! -f "$(git rev-parse --git-path MERGE_HEAD)" ] \
  || { echo "!! a merge is in progress: resolve it, 'git commit', then rerun" >&2; exit 1; }

start_branch=$(git rev-parse --abbrev-ref HEAD)

stashed=0
if [ -n "$(git status --porcelain)" ]; then
  echo "==> stashing dirty working tree"
  git stash push -u -m "sync-fork autostash" >/dev/null
  stashed=1
fi

restore() {
  if [ -f "$(git rev-parse --git-path MERGE_HEAD)" ]; then
    [ "$stashed" = 0 ] || echo "!! merge stopped: your WIP is in 'git stash list' (sync-fork autostash); pop it after the merge is committed" >&2
    return
  fi
  git checkout -q "$start_branch" 2>/dev/null || true
  if [ "$stashed" = 1 ]; then
    echo "==> restoring stashed working tree"
    git stash pop || echo "!! stash pop conflicted — your WIP is safe in 'git stash list'; resolve manually" >&2
  fi
}
trap restore EXIT

# The mirror push is non-fatal: the next run pushes it.
push() {
  git push "$@" || echo "!! push failed ($*) — fork remote not updated" >&2
}

echo "==> fetching $UPSTREAM and $FORK"
git fetch "$UPSTREAM"
git fetch "$FORK" "$PATCHED"

TARGET=$(git rev-parse --verify "${1:?TARGET required}^{commit}")
SYNC_BRANCH="sync/${TARGET:0:12}"
REPO=${SYNC_REPO:-$(git remote get-url "$FORK" | sed -E 's#^.*github\.com[:/]##; s#\.git$##')}

echo "==> fast-forwarding $MIRROR to $UPSTREAM/$MIRROR"
git branch -f "$MIRROR" "$UPSTREAM/$MIRROR"
push "$FORK" "$MIRROR"

open_sync=$(gh api "repos/$REPO/pulls?state=open&base=$PATCHED&per_page=100" \
  --jq '.[] | select(.head.ref | startswith("sync/")) | .number' | head -n1)
if [ -n "$open_sync" ]; then
  echo "VERDICT: HOLD sync PR #$open_sync not landed yet"
  exit 1
fi

if git merge-base --is-ancestor "$TARGET" "$FORK/$PATCHED"; then
  echo "==> $PATCHED already contains ${TARGET:0:9}; nothing to merge"
  exit 0
fi

# A rerun after a hand-resolved conflict finds the finished merge on the branch.
if git merge-base --is-ancestor "$TARGET" "refs/heads/$SYNC_BRANCH" 2>/dev/null; then
  echo "==> $SYNC_BRANCH already has ${TARGET:0:9} merged"
  git checkout -q "$SYNC_BRANCH"
else
  git checkout -q -B "$SYNC_BRANCH" "$FORK/$PATCHED"
  echo "==> merging ${TARGET:0:9} into $SYNC_BRANCH (from $FORK/$PATCHED)"
  git merge --no-ff -m "Merge upstream ${TARGET:0:12} into $PATCHED" "$TARGET" \
    || { echo "!! merge conflicts: resolve them, 'git commit', then rerun this script" >&2; exit 1; }
fi

echo "==> pushing $SYNC_BRANCH"
git push "$FORK" "$SYNC_BRANCH"

echo "==> opening a PR against $PATCHED"
gh api "repos/$REPO/pulls" -f title="Sync upstream ${TARGET:0:12} into $PATCHED" \
  -f head="$SYNC_BRANCH" -f base="$PATCHED" -F body=@"$BODY_FILE" --jq .html_url

echo "==> done. $SYNC_BRANCH pushed; land the PR as a merge commit, never a squash."
